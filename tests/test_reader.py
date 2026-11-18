from unittest.mock import patch

from hotmail_otp_sdk import ConfigOptions, OtpReader
from hotmail_otp_sdk.models import OtpOptions, OtpQuery


def test_wait_for_otp_returns_success_with_mock_provider():
    account = "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client"
    reader = OtpReader.from_format(account, ConfigOptions(provider="GRAPH", fallback_to_imap=False))
    messages = [
        {
            "message_id": "message-1",
            "received_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            "sender": "security@example.com",
            "subject": "Your verification code",
            "body_text": "Your verification code is 482910",
        }
    ]

    from hotmail_otp_sdk.models import MailResult
    mail_results = [MailResult(**item) for item in messages]

    with (
        patch.object(reader.token_manager, "get_access_token", return_value="synthetic-access"),
        patch(
            "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages",
            return_value=mail_results,
        ),
    ):
        result = reader.wait_for_otp(
            OtpQuery(subject_contains="verification"),
            OtpOptions(timeout_ms=500, poll_interval_ms=10, max_messages_to_check=5),
        )

    assert result.status == "SUCCESS"
    assert result.code == "482910"
    assert result.provider == "GRAPH"
    reader.close()


def test_wait_for_otp_rejects_invalid_custom_regex():
    reader = OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
        ConfigOptions(provider="GRAPH", fallback_to_imap=False),
    )
    result = reader.wait_for_otp(
        __import__("hotmail_otp_sdk").OtpQuery(otp_pattern="["),
        OtpOptions(timeout_ms=100, poll_interval_ms=10),
    )
    assert result.status == "ERROR"
    assert result.error == "InvalidOtpPattern"
    reader.close()


def test_close_clears_sensitive_reader_state():
    reader = OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
    )
    reader.close()
    assert reader.account.password == ""
    assert reader.account.refresh_token == ""
    assert reader.account.client_id == ""


def _reader(**config):
    return OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
        ConfigOptions(**config),
    )


def _mail(message_id, body, received_at, subject="Your verification code"):
    from hotmail_otp_sdk.models import MailResult

    return MailResult(
        message_id=message_id,
        received_at=received_at,
        sender="security@example.com",
        subject=subject,
        body_text=body,
    )


def _wait_with_messages(reader, messages):
    with (
        patch.object(reader.token_manager, "get_access_token", return_value="synthetic-access"),
        patch(
            "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages",
            return_value=messages,
        ),
    ):
        return reader.wait_for_otp(OtpQuery(), OtpOptions(timeout_ms=0))


def test_wait_for_otp_prefers_newest_code_after_resend():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    reader = _reader(fallback_to_imap=False)
    result = _wait_with_messages(reader, [
        _mail("old", "Your verification code is 111111", now - timedelta(minutes=1)),
        _mail("new", "Your verification code is 222222", now),
    ])
    assert result.status == "SUCCESS"
    assert result.code == "222222"
    assert result.message_id == "new"
    reader.close()


def test_wait_for_otp_keeps_same_message_tie_ambiguous():
    from datetime import datetime, timezone

    reader = _reader(fallback_to_imap=False)
    result = _wait_with_messages(reader, [
        _mail(
            "one",
            "111111 verification code 222222",
            datetime.now(timezone.utc),
            subject="Hello",
        ),
    ])
    assert result.status == "AMBIGUOUS"
    assert set(result.candidates) == {"111111", "222222"}
    reader.close()


def test_wait_for_otp_keeps_low_context_numbers_ambiguous():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    reader = _reader(fallback_to_imap=False)
    result = _wait_with_messages(reader, [
        _mail("a", "Order 111111 shipped", now - timedelta(minutes=1), subject="Order"),
        _mail("b", "Order 222222 shipped", now, subject="Order"),
    ])
    assert result.status == "AMBIGUOUS"
    reader.close()


def test_graph_provider_gets_fresh_token_after_expiry():
    import io
    import json

    reader = _reader(fallback_to_imap=False)
    tokens = iter(("synthetic-token-1", "synthetic-token-2"))
    seen_auth = []

    class _Response:
        def __enter__(self):
            return io.BytesIO(json.dumps({"value": []}).encode("utf-8"))

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, **_kwargs):
        seen_auth.append(request.get_header("Authorization"))
        return _Response()

    with (
        patch.object(reader.token_manager, "get_access_token", side_effect=lambda *_: next(tokens)),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        reader.get_latest_messages(1)
        reader.get_latest_messages(1)

    assert seen_auth == ["Bearer synthetic-token-1", "Bearer synthetic-token-2"]
    reader.close()


def test_imap_fallback_requests_imap_scoped_token():
    from hotmail_otp_sdk import MailboxProviderError

    reader = _reader(provider="GRAPH", fallback_to_imap=True)
    requested_scopes = []

    def fake_token(scope=None):
        requested_scopes.append(scope)
        return "synthetic-access"

    class FakeImap:
        def authenticate(self, _mechanism, callback):
            callback(b"")
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", [b"0"]

        def uid(self, command, *args):
            # Mirror imaplib: UID SEARCH/FETCH dispatch to search()/fetch().
            return getattr(self, command.lower())(*args)

        def search(self, *_args):
            return "OK", [b""]

        def logout(self):
            return "BYE", []

    with (
        patch.object(reader.token_manager, "get_access_token", side_effect=fake_token),
        patch(
            "hotmail_otp_sdk.reader.GraphMailboxProvider.get_latest_messages",
            side_effect=MailboxProviderError("synthetic graph failure"),
        ),
        patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=FakeImap()),
    ):
        assert reader.get_latest_messages(1) == []

    assert requested_scopes == [reader.options.imap_oauth_scope]
    reader.close()
