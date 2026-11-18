"""Tests for batch loading, context manager, folder merging and IMAP UIDs."""

from datetime import datetime, timedelta, timezone
from unittest.mock import call, patch

import pytest

from hotmail_otp_sdk import (
    ConfigOptions,
    InvalidCredentialFormat,
    OtpReader,
    load_accounts,
    parse_accounts,
)
from hotmail_otp_sdk.models import MailResult
from hotmail_otp_sdk.otp_extractor import normalize_html
from hotmail_otp_sdk.providers import ImapMailboxProvider, _parse_internaldate

GRAPH_READ = "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages"
LINE_A = "a@example.com|synthetic-pass-a|synthetic-refresh-a|synthetic-client"
LINE_B = "b@example.com|synthetic-pass-b|synthetic-refresh-b|synthetic-client"


# --- batch loading ------------------------------------------------------------


def test_parse_accounts_skips_blank_and_comment_lines():
    accounts = parse_accounts(["", "# synthetic accounts", LINE_A, "   ", LINE_B])
    assert [account.email for account in accounts] == ["a@example.com", "b@example.com"]


def test_parse_accounts_accepts_multiline_string():
    assert len(parse_accounts(f"{LINE_A}\r\n{LINE_B}\n")) == 2


def test_parse_accounts_reports_line_number_without_secrets():
    with pytest.raises(InvalidCredentialFormat, match="Line 3") as info:
        parse_accounts([LINE_A, "", "c@example.com|synthetic-secret-c|only-three"])
    assert "synthetic-secret-c" not in str(info.value)
    assert info.value.__cause__ is None


def test_load_accounts_reads_utf8_file_with_bom(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text(f"{LINE_A}\n# comment\n{LINE_B}\n", encoding="utf-8-sig")
    accounts = load_accounts(path)
    assert [account.email for account in accounts] == ["a@example.com", "b@example.com"]
    assert accounts[0].refresh_token == "synthetic-refresh-a"


def test_parse_accounts_strips_bom_from_first_line():
    assert parse_accounts(["﻿" + LINE_A])[0].email == "a@example.com"


# --- context manager ----------------------------------------------------------


def test_reader_context_manager_closes_on_exit():
    with OtpReader.from_format(LINE_A) as reader:
        assert reader.current_refresh_token == "synthetic-refresh-a"
    assert reader.account.refresh_token == ""
    assert reader.current_refresh_token == ""


def test_reader_context_manager_closes_on_error():
    with pytest.raises(RuntimeError), OtpReader.from_format(LINE_A) as reader:
        raise RuntimeError("synthetic")
    assert reader.account.password == ""


# --- get_latest_messages with several folders --------------------------------


def _mail(message_id: str, minutes_ago: int) -> MailResult:
    return MailResult(
        message_id=message_id,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        sender="security@example.com",
        subject="Hello",
        body_text="",
    )


def test_get_latest_messages_both_merges_folders_newest_first():
    reader = OtpReader.from_format(LINE_A, ConfigOptions(fallback_to_imap=False))
    by_folder = {
        "Inbox": [_mail("inbox-1", 1), _mail("inbox-2", 10)],
        "Junk": [_mail("junk-1", 5), _mail("junk-2", 20)],
    }
    with (
        patch.object(reader.token_manager, "get_access_token", return_value="synthetic"),
        patch(GRAPH_READ, side_effect=lambda top, folder, **_: by_folder[folder]) as read,
    ):
        messages = reader.get_latest_messages(top=3, folder="both")

    assert [m.message_id for m in messages] == ["inbox-1", "junk-1", "inbox-2"]
    assert read.call_args_list == [
        call(3, "Inbox", deadline=None),
        call(3, "Junk", deadline=None),
    ]
    reader.close()


# --- HTML entity handling -----------------------------------------------------


def test_normalize_html_decodes_entities_once():
    assert normalize_html("<p>Use &amp;lt;b&amp;gt; for bold</p>") == "Use &lt;b&gt; for bold"


# --- IMAP UID and INTERNALDATE ------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (
            b'7 (UID 42 INTERNALDATE "18-Nov-2026 10:15:23 +0700" BODY[] {10}',
            datetime(2026, 11, 18, 10, 15, 23, tzinfo=timezone(timedelta(hours=7))),
        ),
        (
            b'1 (INTERNALDATE " 8-Nov-2026 01:02:03 -0130")',
            datetime(2026, 11, 8, 1, 2, 3, tzinfo=timezone(-timedelta(hours=1, minutes=30))),
        ),
        (b"1 (RFC822 {10}", None),
        (b'1 (INTERNALDATE "18-Foo-2026 10:15:23 +0000")', None),
        (b'1 (INTERNALDATE "garbage")', None),
    ],
)
def test_parse_internaldate(header, expected):
    assert _parse_internaldate(header) == expected


class _UidImap:
    """Fake server whose Date header lies; INTERNALDATE is authoritative."""

    def __init__(self):
        self.commands: list[tuple] = []

    def authenticate(self, *_args):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"101 205"]
        raw = (
            b"From: security@example.com\r\nSubject: Code\r\n"
            b"Date: Mon, 01 Jan 2001 00:00:00 +0000\r\n"
            b"Content-Type: text/plain\r\n\r\nYour verification code is 482910"
        )
        header = b'1 (UID ' + args[0] + b' INTERNALDATE "18-Nov-2026 10:15:23 +0000" BODY[] {99}'
        return "OK", [(header, raw), b")"]

    def logout(self):
        return "BYE", []


def test_imap_uses_uids_and_internaldate():
    fake = _UidImap()
    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake):
        provider = ImapMailboxProvider("reader@example.com", "synthetic", ConfigOptions())
        messages = provider.get_latest_messages(5, "Inbox")
        provider.close()

    assert [m.message_id for m in messages] == ["205", "101"]
    assert all(
        m.received_at == datetime(2026, 11, 18, 10, 15, 23, tzinfo=timezone.utc) for m in messages
    )
    assert fake.commands[0] == ("SEARCH", "ALL")
    assert fake.commands[1] == ("FETCH", b"205", "(INTERNALDATE BODY.PEEK[])")
