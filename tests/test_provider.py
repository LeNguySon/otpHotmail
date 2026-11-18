import io
import json
import urllib.error
from unittest.mock import patch

from hotmail_otp_sdk.models import ConfigOptions, HotmailAccount
from hotmail_otp_sdk.providers import GraphMailboxProvider, ImapMailboxProvider
from hotmail_otp_sdk.token_manager import TokenManager


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))

    def __exit__(self, *_):
        return False


def test_graph_provider_normalizes_message():
    payload = {
        "value": [
            {
                "id": "message-1",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "subject": "Verification code",
                "body": {"contentType": "html", "content": "<p>Your code is <b>482910</b></p>"},
            }
        ]
    }

    with patch("urllib.request.urlopen", return_value=_Response(payload)):
        provider = GraphMailboxProvider("synthetic-access-token", ConfigOptions())
        messages = provider.get_latest_messages(10, "Inbox")

    assert len(messages) == 1
    assert messages[0].message_id == "message-1"
    assert messages[0].sender == "sender@example.com"
    assert "482910" in messages[0].body_text


def test_graph_provider_follows_next_link_until_top():
    first_payload = {
        "value": [
            {
                "id": "message-1",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "subject": "First",
                "body": {"contentType": "text", "content": "First body"},
            }
        ],
        "@odata.nextLink": "https://graph.test/page-2",
    }
    second_payload = {
        "value": [
            {
                "id": "message-2",
                "receivedDateTime": "2026-11-18T10:14:23Z",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "subject": "Second",
                "body": {"contentType": "text", "content": "Second body"},
            }
        ]
    }

    responses = iter((_Response(first_payload), _Response(second_payload)))
    with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: next(responses)):
        provider = GraphMailboxProvider("synthetic-access-token", ConfigOptions())
        messages = provider.get_latest_messages(2, "Inbox")

    assert [message.message_id for message in messages] == ["message-1", "message-2"]


def test_token_manager_refreshes_and_caches_synthetic_token():
    account = HotmailAccount(
        email="reader@example.com",
        password="synthetic-password",
        refresh_token="synthetic-refresh",
        client_id="synthetic-client",
    )
    payload = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh-2",
        "expires_in": 3600,
    }

    with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
        manager = TokenManager(account, ConfigOptions())
        first = manager.get_access_token()
        second = manager.get_access_token()

    assert first == "synthetic-access"
    assert second == "synthetic-access"
    assert urlopen.call_count == 1
    manager.close()


def test_graph_provider_retries_throttle_then_succeeds():
    throttle = urllib.error.HTTPError(
        "https://graph.test/messages",
        429,
        "Throttled",
        {"Retry-After": "0"},
        None,
    )
    payload = {
        "value": [
            {
                "id": "message-after-retry",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "subject": "Verification",
                "body": {"contentType": "text", "content": "Code 482910"},
            }
        ]
    }

    with (
        patch("urllib.request.urlopen", side_effect=[throttle, _Response(payload)]),
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
    ):
        provider = GraphMailboxProvider(
            "synthetic-access-token",
            ConfigOptions(max_retries=2),
        )
        messages = provider.get_latest_messages(1, "Inbox")

    assert messages[0].message_id == "message-after-retry"
    sleep.assert_called_once_with(0.0)


def test_graph_provider_retries_transient_server_error():
    server_error = urllib.error.HTTPError(
        "https://graph.test/messages",
        503,
        "Unavailable",
        {},
        None,
    )
    payload = {
        "value": [
            {
                "id": "message-after-server-retry",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "subject": "Verification",
                "body": {"contentType": "text", "content": "Code 482910"},
            }
        ]
    }

    with (
        patch("urllib.request.urlopen", side_effect=[server_error, _Response(payload)]),
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
    ):
        provider = GraphMailboxProvider(
            "synthetic-access-token",
            ConfigOptions(max_retries=2, retry_backoff_seconds=0),
        )
        messages = provider.get_latest_messages(1, "Inbox")

    assert messages[0].message_id == "message-after-server-retry"
    sleep.assert_called_once()


def test_imap_provider_authenticates_fetches_and_closes():
    raw_message = (
        b"From: sender@example.com\r\n"
        b"Subject: Verification code\r\n"
        b"Date: Wed, 18 Nov 2026 10:15:23 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Your verification code is 482910"
    )

    class FakeImap:
        def authenticate(self, _mechanism, _callback):
            return "OK", []

        def select(self, mailbox, readonly=False):
            assert mailbox == "INBOX"
            assert readonly is True
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, message_id, _parts):
            assert message_id == b"1"
            return "OK", [(b"1 (RFC822)", raw_message)]

        def logout(self):
            return "BYE", []

    fake = FakeImap()
    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake):
        provider = ImapMailboxProvider(
            "reader@example.com",
            "synthetic-access-token",
            ConfigOptions(),
        )
        messages = provider.get_latest_messages(1, "Inbox")
        provider.close()

    assert len(messages) == 1
    assert messages[0].sender == "sender@example.com"
    assert "482910" in messages[0].body_text
