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
    assert result.error == "OtpNotFound"
    reader.close()


def test_close_clears_sensitive_reader_state():
    reader = OtpReader.from_format(
        "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client",
    )
    reader.close()
    assert reader.account.password == ""
    assert reader.account.refresh_token == ""
    assert reader.account.client_id == ""
