import io
import json
import urllib.error
import urllib.parse
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


def test_graph_provider_sends_valid_url_to_real_http_server():
    # Uses a real local HTTP server (no mocking of urlopen) so http.client's
    # URL validation runs; raw spaces in the OData query used to fail here.
    import http.server
    import threading

    seen_paths: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen_paths.append(self.path)
            body = json.dumps({"value": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = GraphMailboxProvider(
            "synthetic-access-token",
            ConfigOptions(
                graph_base_url=f"http://127.0.0.1:{server.server_port}/v1.0",
                max_retries=1,
            ),
        )
        assert provider.get_latest_messages(5, "Junk") == []
    finally:
        server.shutdown()
        server.server_close()

    assert len(seen_paths) == 1
    path = seen_paths[0]
    assert path.startswith("/v1.0/me/mailFolders/junkemail/messages?")
    assert " " not in path
    assert "$orderby=receivedDateTime%20desc" in path
    assert "$top=5" in path


def test_graph_provider_resolves_token_callable_per_request():
    tokens = iter(("synthetic-token-1", "synthetic-token-2"))
    seen_auth: list[str] = []

    def fake_urlopen(request, **_kwargs):
        seen_auth.append(request.get_header("Authorization"))
        return _Response({"value": []})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        provider = GraphMailboxProvider(lambda: next(tokens), ConfigOptions())
        provider.get_latest_messages(1, "Inbox")
        provider.get_latest_messages(1, "Inbox")

    assert seen_auth == ["Bearer synthetic-token-1", "Bearer synthetic-token-2"]


def test_token_manager_caches_per_scope_and_uses_rolled_refresh_token():
    account = HotmailAccount(
        email="reader@example.com",
        password="synthetic-password",
        refresh_token="synthetic-refresh-1",
        client_id="synthetic-client",
    )
    sent_bodies: list[dict[str, list[str]]] = []
    payloads = iter((
        {"access_token": "synthetic-graph", "refresh_token": "synthetic-refresh-2"},
        {"access_token": "synthetic-imap"},
    ))

    def fake_urlopen(request, **_kwargs):
        sent_bodies.append(urllib.parse.parse_qs(request.data.decode("utf-8")))
        return _Response(next(payloads))

    options = ConfigOptions()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        manager = TokenManager(account, options)
        assert manager.get_access_token() == "synthetic-graph"
        assert manager.get_access_token(options.imap_oauth_scope) == "synthetic-imap"
        # Both scopes are now cached; no further token requests.
        assert manager.get_access_token(options.oauth_scope) == "synthetic-graph"
        assert manager.get_access_token(options.imap_oauth_scope) == "synthetic-imap"

    assert len(sent_bodies) == 2
    assert sent_bodies[0]["scope"] == [options.oauth_scope]
    assert sent_bodies[1]["scope"] == [options.imap_oauth_scope]
    assert sent_bodies[1]["refresh_token"] == ["synthetic-refresh-2"]
    assert "password" not in sent_bodies[0]
    manager.close()


def test_imap_provider_returns_raw_xoauth2_string_to_imaplib():
    # imaplib base64-encodes the authenticator's return value itself.
    captured: list[bytes] = []

    class FakeImap:
        def authenticate(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            captured.append(callback(b""))
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", [b"0"]

        def search(self, *_args):
            return "OK", [b""]

        def logout(self):
            return "BYE", []

    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=FakeImap()):
        provider = ImapMailboxProvider(
            "reader@example.com",
            lambda: "synthetic-imap-token",
            ConfigOptions(),
        )
        assert provider.get_latest_messages(1, "Inbox") == []
        provider.close()

    assert captured == [
        b"user=reader@example.com\x01auth=Bearer synthetic-imap-token\x01\x01"
    ]
