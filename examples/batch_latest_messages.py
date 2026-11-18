"""List recent messages for every account in a file.

The file holds one "email|password|refresh_token|client_id" line per account
(blank lines and # comments are ignored). Use only authorized mailboxes.

    python examples/batch_latest_messages.py accounts.txt
"""

import sys

from hotmail_otp_sdk import HotmailOtpError, OtpReader, load_accounts


def main(path: str) -> int:
    failures = 0
    for account in load_accounts(path):
        try:
            with OtpReader(account) as reader:
                messages = reader.get_latest_messages(top=5, folder="both")
        except HotmailOtpError as exc:
            failures += 1
            print(f"{account.email}: {exc.code}", file=sys.stderr)
            continue
        print(f"{account.email}: {len(messages)} message(s)")
        for message in messages:
            print(f"  {message.received_at:%Y-%m-%d %H:%M} {message.sender} | {message.subject}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
