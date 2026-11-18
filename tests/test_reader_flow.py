from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, call, patch

import pytest

from hotmail_otp_sdk import (
    ConfigOptions,
    MailboxProviderError,
    OtpReader,
    Throttled,
    TokenRefreshFailed,
)
from hotmail_otp_sdk.models import MailResult, OtpOptions, OtpQuery

GRAPH_READ = "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages"
IMAP_READ = "hotmail_otp_sdk.reader.ImapMailboxProvider.get_latest_messages"
SLEEP = "hotmail_otp_sdk.reader.time.sleep"


def _reader(**config) -> OtpReader:
    config.setdefault("fallback_to_imap", False)
    reader = OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
        ConfigOptions(**config),
    )
    patcher = patch.object(reader.token_manager, "get_access_token", return_value="synthetic-access")
    patcher.start()
    reader._token_patcher = patcher  # stopped by the fixture below
    return reader


@pytest.fixture
def reader():
    instance = _reader()
    yield instance
    instance._token_patcher.stop()
    instance.close()


def _otp_mail(code="482910", received_at=None, message_id="message-1") -> MailResult:
    return MailResult(
        message_id=message_id,
        received_at=received_at or datetime.now(timezone.utc),
        sender="security@example.com",
        subject="Your verification code",
        body_text=f"Your verification code is {code}. " + "x" * 300,
    )


def test_timeout_when_no_otp_arrives(reader):
    with patch(GRAPH_READ, return_value=[]), patch(SLEEP) as sleep:
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "TIMEOUT"
    assert result.error == "Timeout"
    sleep.assert_not_called()


def test_polls_until_otp_arrives(reader):
    with (
        patch(GRAPH_READ, side_effect=[[], [], [_otp_mail()]]) as read,
        patch(SLEEP) as sleep,
    ):
        result = reader.wait_for_otp(
            OtpQuery(), OtpOptions(timeout_ms=60_000, poll_interval_ms=2_500)
        )
    assert result.status == "SUCCESS"
    assert read.call_count == 3
    assert sleep.call_args_list == [call(2.5), call(2.5)]


def test_throttle_with_retry_after_waits_then_succeeds(reader):
    with (
        patch(GRAPH_READ, side_effect=[Throttled(retry_after=4.0), [_otp_mail()]]),
        patch(SLEEP) as sleep,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=60_000))
    assert result.status == "SUCCESS"
    sleep.assert_called_once_with(4.0)


def test_throttle_retry_after_beyond_deadline_times_out_without_waiting(reader):
    with (
        patch(GRAPH_READ, side_effect=[Throttled(retry_after=600.0), [_otp_mail()]]),
        patch(SLEEP) as sleep,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=5_000))
    assert result.status == "TIMEOUT"
    sleep.assert_not_called()


def test_throttle_after_deadline_returns_timeout(reader):
    with patch(GRAPH_READ, side_effect=Throttled(retry_after=5.0)), patch(SLEEP) as sleep:
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "TIMEOUT"
    sleep.assert_not_called()


def test_throttle_without_retry_after_uses_poll_interval(reader):
    with (
        patch(GRAPH_READ, side_effect=[Throttled(), [_otp_mail()]]),
        patch(SLEEP) as sleep,
    ):
        result = reader.wait_for_otp(
            OtpQuery(), OtpOptions(timeout_ms=60_000, poll_interval_ms=1_000)
        )
    assert result.status == "SUCCESS"
    sleep.assert_called_once_with(1.0)


def test_provider_error_returns_error_result(reader):
    with patch(GRAPH_READ, side_effect=MailboxProviderError("synthetic failure")):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "ERROR"
    assert result.error == "MailboxProviderError"


def test_token_failure_returns_error_result():
    reader = OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
        ConfigOptions(fallback_to_imap=False),
    )
    with (
        patch.object(reader.token_manager, "get_access_token", side_effect=TokenRefreshFailed("x")),
        patch("urllib.request.urlopen") as urlopen,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "ERROR"
    assert result.error == "TokenRefreshFailed"
    urlopen.assert_not_called()
    reader.close()


@pytest.mark.parametrize(
    "options",
    [
        OtpOptions(timeout_ms=-1),
        OtpOptions(poll_interval_ms=-1),
        OtpOptions(max_messages_to_check=0),
    ],
)
def test_invalid_options_return_error_without_network(reader, options):
    with patch(GRAPH_READ) as read:
        result = reader.wait_for_otp(OtpQuery(), options)
    assert result.status == "ERROR"
    read.assert_not_called()


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("Inbox", ["Inbox"]),
        ("junk", ["Junk"]),
        ("spam", ["Junk"]),
        ("both", ["Inbox", "Junk"]),
        ("inbox+junk", ["Inbox", "Junk"]),
    ],
)
def test_query_folder_selects_mailboxes(reader, folder, expected):
    with patch(GRAPH_READ, return_value=[]) as read:
        reader.wait_for_otp(OtpQuery(folder=folder), OtpOptions(timeout_ms=0, max_messages_to_check=7))
    assert read.call_args_list == [call(7, name, deadline=ANY) for name in expected]
    assert all(c.kwargs["deadline"] is not None for c in read.call_args_list)


def test_default_since_ignores_old_messages(reader):
    old = _otp_mail(received_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    with patch(GRAPH_READ, return_value=[old]):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "TIMEOUT"


def test_body_snippet_only_when_requested(reader):
    with patch(GRAPH_READ, return_value=[_otp_mail()]):
        without = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
        with_snippet = reader.wait_for_otp(
            OtpQuery(), OtpOptions(timeout_ms=0, include_body_snippet=True)
        )
    assert without.body_snippet is None
    assert with_snippet.body_snippet.startswith("Your verification code is 482910")
    assert len(with_snippet.body_snippet) == 240


def test_custom_otp_pattern_is_used(reader):
    mail = MailResult(
        message_id="m",
        received_at=datetime.now(timezone.utc),
        sender="security@example.com",
        subject="Code",
        body_text="Your passcode is AB12CD, ref 123456",
    )
    with patch(GRAPH_READ, return_value=[mail]):
        result = reader.wait_for_otp(
            OtpQuery(otp_pattern=r"\b[A-Z]{2}\d{2}[A-Z]{2}\b"), OtpOptions(timeout_ms=0)
        )
    assert result.status == "SUCCESS"
    assert result.code == "AB12CD"


def test_graph_failure_without_fallback_is_raised(reader):
    with (
        patch(GRAPH_READ, side_effect=MailboxProviderError("synthetic failure")),
        patch(IMAP_READ) as imap_read,
        pytest.raises(MailboxProviderError),
    ):
        reader.get_latest_messages(1)
    imap_read.assert_not_called()


def test_fallback_switches_provider_name():
    reader = _reader(fallback_to_imap=True)
    try:
        with (
            patch(GRAPH_READ, side_effect=MailboxProviderError("synthetic failure")),
            patch(IMAP_READ, return_value=[_otp_mail()]),
        ):
            result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
        assert result.status == "SUCCESS"
        assert result.provider == "IMAP"
    finally:
        reader._token_patcher.stop()
        reader.close()


def test_imap_provider_option_skips_graph():
    reader = _reader(provider="imap")
    try:
        with patch(GRAPH_READ) as graph_read, patch(IMAP_READ, return_value=[]) as imap_read:
            assert reader.get_latest_messages(3, "Junk") == []
        graph_read.assert_not_called()
        imap_read.assert_called_once_with(3, "Junk", deadline=None)
    finally:
        reader._token_patcher.stop()
        reader.close()


def test_get_latest_messages_trims_to_top(reader):
    base = datetime.now(timezone.utc)
    mails = [
        _otp_mail(message_id=str(i), received_at=base - timedelta(seconds=i)) for i in range(5)
    ]
    with patch(GRAPH_READ, return_value=mails):
        assert [m.message_id for m in reader.get_latest_messages(2)] == ["0", "1"]


def test_get_latest_messages_rejects_non_positive_top(reader):
    with pytest.raises(ValueError):
        reader.get_latest_messages(0)


def test_closed_reader_rejects_reads():
    reader = _reader()
    reader._token_patcher.stop()
    reader.close()
    with pytest.raises(MailboxProviderError, match="closed"):
        reader.get_latest_messages(1)
    result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "ERROR"
