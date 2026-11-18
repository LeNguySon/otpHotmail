"""Security audit: no secret ever reaches logs, errors or reprs (spec 12.3)."""

import io
import json
import logging
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

from hotmail_otp_sdk import ConfigOptions, OtpReader
from hotmail_otp_sdk.cli import ACCOUNT_ENV, main
from hotmail_otp_sdk.models import OtpOptions, OtpQuery

EMAIL = "audited.reader@example.com"
PASSWORD = "synthetic-password-Zq81"
REFRESH = "synthetic-refresh-Kp27"
ROTATED = "synthetic-refresh-rotated-Hn55"
CLIENT = "synthetic-client-Vb13"
ACCESS = "synthetic-access-Mx64"
OTP = "735184"
ACCOUNT_LINE = f"{EMAIL}|{PASSWORD}|{REFRESH}|{CLIENT}"
CREDENTIALS = (PASSWORD, REFRESH, ROTATED, ACCESS)


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return io.BytesIO(self.body)

    def __exit__(self, *_):
        return False


def _graph_message():
    return {
        "value": [
            {
                "id": "audit-message",
                "receivedDateTime": "2099-01-01T00:00:00Z",
                "from": {"emailAddress": {"address": "security@example.com"}},
                "subject": "Your verification code",
                "body": {"contentType": "text", "content": f"Your verification code is {OTP}"},
            }
        ]
    }


def _fake_network(graph_failures: int = 0):
    """Token endpoint plus Graph; Graph fails ``graph_failures`` times with 503."""
    state = {"graph_calls": 0, "sent": []}

    def fake_urlopen(request, **_kwargs):
        state["sent"].append(request)
        if "token" in request.full_url:
            return _Response({"access_token": ACCESS, "refresh_token": ROTATED, "expires_in": 3600})
        state["graph_calls"] += 1
        if state["graph_calls"] <= graph_failures:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return _Response(_graph_message())

    return fake_urlopen, state


def _assert_clean(text: str, *, allow_email: bool = False):
    """Credentials and the OTP must never appear; logs must also mask the email."""
    for secret in CREDENTIALS:
        assert secret not in text, f"secret leaked: {secret[:16]}..."
    assert OTP not in text, "OTP leaked"
    if not allow_email:
        assert EMAIL not in text, "unmasked email leaked"


def test_full_flow_logs_no_secrets(caplog):
    caplog.set_level(logging.DEBUG, logger="hotmail_otp_sdk")
    fake_urlopen, state = _fake_network(graph_failures=1)
    options = ConfigOptions(
        token_endpoint="https://login.test/token",
        graph_base_url="https://graph.test/v1.0",
        max_retries=2,
        retry_backoff_seconds=0,
    )
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("hotmail_otp_sdk.providers.time.sleep"),
        OtpReader.from_format(ACCOUNT_LINE, options) as reader,
    ):
        # Leave room in the deadline for one Graph retry (sleep is patched).
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=60_000))
        rotated = reader.current_refresh_token

    assert result.status == "SUCCESS"
    assert result.code == OTP
    assert rotated == ROTATED
    assert caplog.records, "expected the flow to log something"
    assert "a***@example.com" in caplog.text
    _assert_clean(caplog.text)

    # The password is never sent anywhere.
    token_request = next(r for r in state["sent"] if "token" in r.full_url)
    body = urllib.parse.parse_qs(token_request.data.decode("utf-8"))
    assert PASSWORD not in token_request.data.decode("utf-8")
    assert body["refresh_token"] == [REFRESH]


def test_failure_paths_log_no_secrets(caplog):
    caplog.set_level(logging.DEBUG, logger="hotmail_otp_sdk")

    def failing_urlopen(request, **_kwargs):
        if "token" in request.full_url:
            return _Response({"access_token": ACCESS})
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

    options = ConfigOptions(
        token_endpoint="https://login.test/token",
        graph_base_url="https://graph.test/v1.0",
        max_retries=2,
        retry_backoff_seconds=0,
        fallback_to_imap=True,
    )
    with (
        patch("urllib.request.urlopen", side_effect=failing_urlopen),
        patch("hotmail_otp_sdk.providers.time.sleep"),
        patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", side_effect=OSError("synthetic")),
        OtpReader.from_format(ACCOUNT_LINE, options) as reader,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))

    assert result.status == "ERROR"
    assert "falling back to IMAP" in caplog.text
    _assert_clean(caplog.text)
    # OtpResult carries the mailbox address by contract, but no credentials.
    _assert_clean(str(result), allow_email=True)


def test_token_rejection_logs_no_secrets(caplog):
    caplog.set_level(logging.DEBUG, logger="hotmail_otp_sdk")
    error = urllib.error.HTTPError("https://login.test/token", 400, "invalid_grant", {}, None)
    with (
        patch("urllib.request.urlopen", side_effect=error),
        OtpReader.from_format(ACCOUNT_LINE) as reader,
    ):
        result = reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))
    assert result.error == "TokenRefreshFailed"
    _assert_clean(caplog.text)
    # OtpResult carries the mailbox address by contract, but no credentials.
    _assert_clean(str(result), allow_email=True)


@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_cli_verbose_output_prints_only_the_otp(monkeypatch, verbosity):
    monkeypatch.setenv(ACCOUNT_ENV, ACCOUNT_LINE)
    fake_urlopen, _ = _fake_network()
    out, err = io.StringIO(), io.StringIO()
    package_logger = logging.getLogger("hotmail_otp_sdk")
    handlers_before = list(package_logger.handlers)
    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code = main(
                ["wait", verbosity, "--timeout", "0"],
                stdin=io.StringIO(), stdout=out, stderr=err,
            )
    finally:
        for handler in package_logger.handlers[:]:
            if handler not in handlers_before:
                package_logger.removeHandler(handler)
        package_logger.setLevel(logging.NOTSET)

    assert code == 0
    assert out.getvalue() == f"{OTP}\n"
    assert "status=SUCCESS" in err.getvalue()
    _assert_clean(err.getvalue())


def test_account_repr_and_closed_reader_hold_no_secrets():
    reader = OtpReader.from_format(ACCOUNT_LINE)
    _assert_clean(repr(reader.account), allow_email=True)
    reader.close()
    assert PASSWORD not in repr(vars(reader.account))
    assert REFRESH not in repr(vars(reader.account))
