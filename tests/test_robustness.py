"""Regression tests for error handling, deadlines and credential lifecycle."""

import http.client
import io
import json
import socket
import time
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hotmail_otp_sdk import (
    ConfigOptions,
    HotmailAccount,
    MailboxProviderError,
    OtpReader,
    Throttled,
    TokenRefreshFailed,
)
from hotmail_otp_sdk.models import MailResult, OtpOptions, OtpQuery
from hotmail_otp_sdk.otp_extractor import extract_otp_candidates
from hotmail_otp_sdk.providers import GraphMailboxProvider, ImapMailboxProvider
from hotmail_otp_sdk.token_manager import TokenManager

GRAPH_READ = "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages"
IMAP_READ = "hotmail_otp_sdk.reader.ImapMailboxProvider.get_latest_messages"
IMAP_SSL = "hotmail_otp_sdk.providers.imaplib.IMAP4_SSL"
ACCOUNT_LINE = "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client"


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return io.BytesIO(self.body)

    def __exit__(self, *_):
        return False


def _json(payload) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"))


def _reader(**config) -> OtpReader:
    reader = OtpReader.from_format(ACCOUNT_LINE, ConfigOptions(**config))
    reader.token_manager.get_access_token = lambda *_args: "synthetic-access"  # type: ignore[method-assign]
    return reader


def _mail(body: str, subject: str = "Hello", received_at=None) -> MailResult:
    return MailResult(
        message_id="m",
        received_at=received_at or datetime(2026, 11, 18, tzinfo=timezone.utc),
        sender="security@example.com",
        subject=subject,
        body_text=body,
    )


# --- #6: throttling does not abandon Graph ------------------------------------


def test_graph_throttle_does_not_trigger_imap_fallback():
    reader = _reader(fallback_to_imap=True)
    with (
        patch(GRAPH_READ, side_effect=Throttled(retry_after=30.0)),
        patch(IMAP_READ) as imap_read,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "TIMEOUT"
    assert result.provider == "GRAPH"
    imap_read.assert_not_called()
    reader.close()


def test_get_latest_messages_surfaces_throttle():
    reader = _reader(fallback_to_imap=True)
    with (
        patch(GRAPH_READ, side_effect=Throttled(retry_after=3.0)),
        patch(IMAP_READ) as imap_read,
        pytest.raises(Throttled),
    ):
        reader.get_latest_messages(1)
    imap_read.assert_not_called()
    reader.close()


# --- #7: network and payload failures become MailboxProviderError -----------


@pytest.mark.parametrize(
    "error",
    [socket.gaierror("synthetic dns failure"), ConnectionRefusedError(), TimeoutError()],
)
def test_imap_connect_failure_returns_error_result(error):
    reader = _reader(provider="IMAP")
    with patch(IMAP_SSL, side_effect=error):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.status == "ERROR"
    assert result.error == "MailboxProviderError"
    reader.close()


def test_imap_token_failure_does_not_open_connection():
    def failing_token():
        raise TokenRefreshFailed("synthetic")

    with patch(IMAP_SSL) as ctor:
        provider = ImapMailboxProvider("reader@example.com", failing_token, ConfigOptions())
        with pytest.raises(TokenRefreshFailed):
            provider.get_latest_messages(1, "Inbox")
    ctor.assert_not_called()


class _BrokenThenHealthyImap:
    def __init__(self):
        self.select_calls = 0
        self.logged_out = 0

    def authenticate(self, *_args):
        return "OK", []

    def select(self, *_args, **_kwargs):
        self.select_calls += 1
        if self.select_calls == 1:
            raise ConnectionResetError("synthetic reset")
        return "OK", [b"0"]

    def uid(self, command, *args):
        # Mirror imaplib: UID SEARCH/FETCH dispatch to search()/fetch().
        return getattr(self, command.lower())(*args)

    def search(self, *_args):
        return "OK", [b""]

    def logout(self):
        self.logged_out += 1
        raise OSError("socket already closed")


def test_imap_socket_error_drops_connection_and_reconnects():
    fake = _BrokenThenHealthyImap()
    with patch(IMAP_SSL, return_value=fake) as ctor:
        provider = ImapMailboxProvider("reader@example.com", "synthetic", ConfigOptions())
        with pytest.raises(MailboxProviderError, match="query failed"):
            provider.get_latest_messages(1, "Inbox")
        assert provider.get_latest_messages(1, "Inbox") == []
        provider.close()
    assert ctor.call_count == 2
    assert fake.logged_out >= 1


def test_imap_skips_message_with_malformed_date():
    raw = (
        b"From: security@example.com\r\nSubject: Code\r\nDate: not a date\r\n"
        b"Content-Type: text/plain\r\n\r\nCode 482910"
    )

    class Fake:
        def authenticate(self, *_args):
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", [b"1"]

        def uid(self, command, *args):
            # Mirror imaplib: UID SEARCH/FETCH dispatch to search()/fetch().
            return getattr(self, command.lower())(*args)

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, message_id, _parts):
            return "OK", [(b"1 (BODY[])", raw)]

        def logout(self):
            return "BYE", []

    with patch(IMAP_SSL, return_value=Fake()):
        provider = ImapMailboxProvider("reader@example.com", "synthetic", ConfigOptions())
        assert provider.get_latest_messages(1, "Inbox") == []
        provider.close()


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[1, 2]"])
def test_graph_invalid_response_raises_provider_error(body):
    with (
        patch("urllib.request.urlopen", return_value=_Response(body)),
        pytest.raises(MailboxProviderError, match="invalid response"),
    ):
        GraphMailboxProvider("synthetic", ConfigOptions()).get_latest_messages(1, "Inbox")


@pytest.mark.parametrize(
    "item",
    [
        {"receivedDateTime": "2026-11-18T10:15:23Z"},  # missing id
        {"id": "m", "receivedDateTime": "yesterday"},
        {"id": "m"},
        "not-an-object",
    ],
)
def test_graph_unexpected_message_payload_raises_provider_error(item):
    with (
        patch("urllib.request.urlopen", return_value=_json({"value": [item]})),
        pytest.raises(MailboxProviderError, match="unexpected message payload"),
    ):
        GraphMailboxProvider("synthetic", ConfigOptions()).get_latest_messages(1, "Inbox")


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("synthetic reset"), http.client.IncompleteRead(b"")],
)
def test_graph_connection_errors_are_retried_then_wrapped(error):
    with (
        patch("urllib.request.urlopen", side_effect=[error, error]) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep"),
        pytest.raises(MailboxProviderError, match="unreachable"),
    ):
        GraphMailboxProvider(
            "synthetic", ConfigOptions(max_retries=2, retry_backoff_seconds=0)
        ).get_latest_messages(1, "Inbox")
    assert urlopen.call_count == 2


def test_graph_http_error_without_headers_is_handled():
    error = urllib.error.HTTPError("https://graph.test", 503, "error", None, None)
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(MailboxProviderError, match="HTTP 503"),
    ):
        GraphMailboxProvider("synthetic", ConfigOptions(max_retries=1)).get_latest_messages(1, "Inbox")


# --- #8: year detection and generic "code" context ---------------------------


@pytest.mark.parametrize("year", ["1999", "2024", "2026", "2027"])
def test_year_like_numbers_without_context_are_ignored(year):
    message = _mail(f"Your verification code is 482910.\n\n\n(c) {year} Example Inc.")
    assert extract_otp_candidates(message) == ("482910",)


def test_year_like_number_right_after_verification_wording_is_kept():
    assert extract_otp_candidates(_mail("Your verification code is 2019")) == ("2019",)


def test_future_four_digit_numbers_are_not_treated_as_years():
    assert extract_otp_candidates(_mail("Use 4821 to sign in")) == ("4821",)


def test_generic_code_wording_prefers_nearby_number():
    message = _mail("Your code is 482910. Order number 553311 ships soon.")
    assert extract_otp_candidates(message)[0] == "482910"


def test_strong_context_outranks_generic_code():
    message = _mail("Postal code 10001. Your verification code: 482910")
    assert extract_otp_candidates(message)[0] == "482910"


def test_vietnamese_verification_wording_is_recognized():
    message = _mail("Mã xác minh của bạn là 482910. Đơn hàng 553311.")
    assert extract_otp_candidates(message)[0] == "482910"


# --- #10 / #11: credential lifecycle -----------------------------------------


def test_close_does_not_mutate_callers_account():
    account = HotmailAccount(
        email="reader@example.com",
        password="synthetic-password",
        refresh_token="synthetic-refresh",
        client_id="synthetic-client",
    )
    reader = OtpReader(account)
    reader.close()
    assert account.refresh_token == "synthetic-refresh"
    assert account.password == "synthetic-password"
    assert reader.account.refresh_token == ""
    assert reader.account.email == "reader@example.com"
    assert "synthetic" not in repr(reader.account)


def test_token_manager_refuses_use_after_close():
    reader = OtpReader.from_format(ACCOUNT_LINE)
    reader.close()
    with (
        patch("urllib.request.urlopen") as urlopen,
        pytest.raises(TokenRefreshFailed, match="closed"),
    ):
        reader.token_manager.get_access_token()
    urlopen.assert_not_called()


def test_current_refresh_token_reflects_rotation():
    reader = OtpReader.from_format(ACCOUNT_LINE)
    assert reader.current_refresh_token == "synthetic-refresh"
    payload = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh-rotated"}
    with patch("urllib.request.urlopen", return_value=_json(payload)):
        reader.token_manager.get_access_token()
    assert reader.current_refresh_token == "synthetic-refresh-rotated"
    reader.close()
    assert reader.current_refresh_token == ""


def test_token_manager_exposes_refresh_token_property():
    account = HotmailAccount("reader@example.com", "p", "synthetic-refresh", "c")
    assert TokenManager(account, ConfigOptions()).current_refresh_token == "synthetic-refresh"


# --- #12: deadlines are respected --------------------------------------------


def test_graph_skips_retry_wait_that_would_pass_deadline():
    error = urllib.error.HTTPError(
        "https://graph.test", 503, "error", {"Retry-After": "30"}, None
    )
    with (
        patch("urllib.request.urlopen", side_effect=[error, _json({"value": []})]) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(MailboxProviderError, match="HTTP 503"),
    ):
        GraphMailboxProvider("synthetic", ConfigOptions(max_retries=3)).get_latest_messages(
            1, "Inbox", deadline=time.monotonic() + 5
        )
    assert urlopen.call_count == 1
    sleep.assert_not_called()


def test_graph_throttle_near_deadline_raises_with_retry_after():
    error = urllib.error.HTTPError(
        "https://graph.test", 429, "error", {"Retry-After": "30"}, None
    )
    with (
        patch("urllib.request.urlopen", side_effect=error),
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(Throttled) as info,
    ):
        GraphMailboxProvider("synthetic", ConfigOptions(max_retries=3)).get_latest_messages(
            1, "Inbox", deadline=time.monotonic() + 5
        )
    assert info.value.retry_after == 30.0
    sleep.assert_not_called()


def test_graph_request_timeout_is_bounded_by_deadline():
    timeouts = []

    def fake_urlopen(_request, timeout):
        timeouts.append(timeout)
        return _json({"value": []})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        provider = GraphMailboxProvider("synthetic", ConfigOptions(http_timeout_seconds=20))
        provider.get_latest_messages(1, "Inbox", deadline=time.monotonic() + 3)
        provider.get_latest_messages(1, "Inbox", deadline=time.monotonic() - 10)
        provider.get_latest_messages(1, "Inbox")

    assert 1.0 <= timeouts[0] <= 3.0
    assert timeouts[1] == 1.0  # minimum per-operation timeout
    assert timeouts[2] == 20


def test_poll_sleep_never_exceeds_remaining_time():
    reader = _reader(fallback_to_imap=False)
    with (
        patch(GRAPH_READ, return_value=[]),
        patch("hotmail_otp_sdk.reader.time.sleep") as sleep,
    ):
        # The patched sleep returns immediately; stop after the first wait by
        # letting the real clock pass the short deadline.
        sleep.side_effect = lambda seconds: time_sleep(seconds)
        result = reader.wait_for_otp(
            OtpQuery(), OtpOptions(timeout_ms=200, poll_interval_ms=60_000)
        )
    assert result.status == "TIMEOUT"
    assert sleep.call_count >= 1
    assert all(call.args[0] <= 0.2 + 1e-6 for call in sleep.call_args_list)
    reader.close()


time_sleep = time.sleep
