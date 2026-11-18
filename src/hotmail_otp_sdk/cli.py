"""Command-line interface: ``hotmail-otp wait`` and ``hotmail-otp list``.

The account line (``email|password|refresh_token|client_id``) is read from the
``HOTMAIL_OTP_ACCOUNT`` environment variable, from ``--account-file`` or from
standard input (``--account-file -``). It is deliberately not accepted as a
command-line argument, because arguments leak into shell history and process
listings.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, TextIO

from . import __version__
from .exceptions import HotmailOtpError, InvalidCredentialFormat
from .models import ConfigOptions, HotmailAccount, MailResult, OtpOptions, OtpQuery, OtpResult
from .parser import parse_accounts
from .reader import OtpReader

ACCOUNT_ENV = "HOTMAIL_OTP_ACCOUNT"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_AMBIGUOUS = 4

_STATUS_EXIT = {
    "SUCCESS": EXIT_OK,
    "TIMEOUT": EXIT_TIMEOUT,
    "AMBIGUOUS": EXIT_AMBIGUOUS,
}


class _UsageError(Exception):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hotmail-otp",
        description="Read OTP codes and messages from an authorized Hotmail/Outlook mailbox.",
        epilog=f"The account line is read from ${ACCOUNT_ENV}, --account-file, or stdin (--account-file -).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--account-file",
        metavar="PATH",
        help="file holding the account line ('-' for stdin); the first account line is used",
    )
    common.add_argument("--provider", choices=["graph", "imap"], default="graph")
    common.add_argument("--no-fallback", action="store_true", help="do not fall back from Graph to IMAP")
    common.add_argument("--json", action="store_true", help="print machine-readable JSON")
    common.add_argument("-v", "--verbose", action="count", default=0, help="log to stderr (-vv for debug)")

    commands = parser.add_subparsers(dest="command", required=True)

    wait = commands.add_parser("wait", parents=[common], help="wait for an OTP and print it")
    wait.add_argument("--sender", action="append", default=[], help="allowed sender or @domain (repeatable)")
    wait.add_argument("--subject", help="subject must contain this text")
    wait.add_argument("--pattern", help="custom OTP regular expression")
    wait.add_argument("--folder", default="both", help="Inbox, Junk or both (default: both)")
    wait.add_argument("--since-minutes", type=float, default=5.0, help="ignore older mail (default: 5)")
    wait.add_argument("--timeout", type=float, default=60.0, help="overall timeout in seconds (default: 60)")
    wait.add_argument("--poll", type=float, default=3.0, help="poll interval in seconds (default: 3)")
    wait.add_argument("--max-messages", type=int, default=10, help="messages checked per folder (default: 10)")

    listing = commands.add_parser("list", parents=[common], help="list recent messages")
    listing.add_argument("--top", type=int, default=10, help="number of messages (default: 10)")
    listing.add_argument("--folder", default="Inbox", help="Inbox, Junk or both (default: Inbox)")
    listing.add_argument("--body", action="store_true", help="include the plain-text body")
    return parser


def _read_account(args: argparse.Namespace, stdin: TextIO) -> HotmailAccount:
    if args.account_file == "-":
        source = stdin.read()
    elif args.account_file:
        try:
            with open(args.account_file, encoding="utf-8-sig") as handle:
                source = handle.read()
        except OSError as exc:
            raise _UsageError(f"Cannot read account file: {exc.strerror or exc}.") from None
    else:
        source = os.environ.get(ACCOUNT_ENV, "")
    if not source.strip():
        raise _UsageError(f"No account given. Set ${ACCOUNT_ENV} or use --account-file.")
    accounts = parse_accounts(source)
    if not accounts:
        raise _UsageError("The account source contains no account line.")
    return accounts[0]


def _config(args: argparse.Namespace) -> ConfigOptions:
    return ConfigOptions(provider=args.provider.upper(), fallback_to_imap=not args.no_fallback)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def _result_dict(result: OtpResult) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in asdict(result).items()}


def _message_dict(message: MailResult, include_body: bool) -> dict[str, Any]:
    data = {
        "message_id": message.message_id,
        "received_at": message.received_at.isoformat(),
        "sender": message.sender,
        "subject": message.subject,
    }
    if include_body:
        data["body_text"] = message.body_text
    return data


def _run_wait(reader: OtpReader, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.timeout < 0 or args.poll < 0 or args.max_messages < 1 or args.since_minutes < 0:
        raise _UsageError("--timeout, --poll and --since-minutes must be >= 0 and --max-messages >= 1.")
    result = reader.wait_for_otp(
        OtpQuery(
            since=datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes),
            sender_allowlist=tuple(args.sender),
            subject_contains=args.subject,
            otp_pattern=args.pattern,
            folder=args.folder,
        ),
        OtpOptions(
            timeout_ms=int(args.timeout * 1000),
            poll_interval_ms=int(args.poll * 1000),
            max_messages_to_check=args.max_messages,
        ),
    )
    if args.json:
        print(json.dumps(_result_dict(result), ensure_ascii=False), file=out)
    elif result.status == "SUCCESS":
        print(result.code, file=out)
    elif result.status == "AMBIGUOUS":
        print(f"Ambiguous OTP; candidates: {', '.join(result.candidates)}", file=err)
    else:
        print(f"{result.status}: {result.error}", file=err)
    return _STATUS_EXIT.get(result.status, EXIT_ERROR)


def _run_list(reader: OtpReader, args: argparse.Namespace, out: TextIO) -> int:
    if args.top < 1:
        raise _UsageError("--top must be at least 1.")
    messages = reader.get_latest_messages(top=args.top, folder=args.folder)
    if args.json:
        payload = [_message_dict(message, args.body) for message in messages]
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return EXIT_OK
    for message in messages:
        print(f"{message.received_at.isoformat()}\t{message.sender}\t{message.subject}", file=out)
        if args.body:
            print(message.body_text, file=out)
            print(file=out)
    return EXIT_OK


def _configure_logging(verbosity: int, err: TextIO) -> None:
    if verbosity <= 0:
        return
    handler = logging.StreamHandler(err)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    package_logger = logging.getLogger("hotmail_otp_sdk")
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.DEBUG if verbosity > 1 else logging.INFO)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose, err)
    try:
        account = _read_account(args, stdin)
        with OtpReader(account, _config(args)) as reader:
            if args.command == "wait":
                return _run_wait(reader, args, out, err)
            return _run_list(reader, args, out)
    except (_UsageError, InvalidCredentialFormat) as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE
    except HotmailOtpError as exc:
        print(f"error: {exc.code}: {exc}", file=err)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
