import imaplib
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from hotmail_otp_sdk import MailboxProviderError, Throttled
from hotmail_otp_sdk.models import ConfigOptions
from hotmail_otp_sdk.providers import GraphMailboxProvider, ImapMailboxProvider

SYNTHETIC_TOKEN = "synthetic-access-token"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))

    def __exit__(self, *_):
        return False


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://graph.test/messages", code, "error", headers or {}, None)


def _graph(**config) -> GraphMailboxProvider:
    return GraphMailboxProvider(SYNTHETIC_TOKEN, ConfigOptions(**config))


# --- Graph -----------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_graph_client_errors_fail_without_retry(code):
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(code)) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(MailboxProviderError, match=f"HTTP {code}") as info,
    ):
        _graph(max_retries=3).get_latest_messages(1, "Inbox")
    assert urlopen.call_count == 1
    sleep.assert_not_called()
    assert SYNTHETIC_TOKEN not in str(info.value)


def test_graph_throttle_exhausts_retries_and_reports_retry_after():
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[_http_error(429, {"Retry-After": "7"})] * 2,
        ) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(Throttled) as info,
    ):
        _graph(max_retries=2).get_latest_messages(1, "Inbox")
    assert info.value.retry_after == 7.0
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(7.0)


def test_graph_server_error_exhausts_retries():
    with (
        patch("urllib.request.urlopen", side_effect=[_http_error(503)] * 3) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(MailboxProviderError, match="HTTP 503"),
    ):
        _graph(max_retries=3, retry_backoff_seconds=0).get_latest_messages(1, "Inbox")
    assert urlopen.call_count == 3
    assert sleep.call_count == 2


def test_graph_invalid_retry_after_uses_exponential_backoff():
    payload = {"value": []}
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=[_http_error(429, {"Retry-After": "soon"}), _Response(payload)],
        ),
        patch("hotmail_otp_sdk.providers.random.uniform", return_value=0.0),
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
    ):
        _graph(max_retries=2, retry_backoff_seconds=1.5).get_latest_messages(1, "Inbox")
    sleep.assert_called_once_with(1.5)


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("unreachable"), TimeoutError("timed out")],
)
def test_graph_network_errors_retry_then_fail(error):
    with (
        patch("urllib.request.urlopen", side_effect=[error, error]) as urlopen,
        patch("hotmail_otp_sdk.providers.time.sleep") as sleep,
        pytest.raises(MailboxProviderError, match="unreachable"),
    ):
        _graph(max_retries=2, retry_backoff_seconds=0).get_latest_messages(1, "Inbox")
    assert urlopen.call_count == 2
    sleep.assert_called_once()


def test_graph_closed_provider_rejects_reads():
    provider = _graph()
    provider.close()
    with pytest.raises(MailboxProviderError, match="closed"):
        provider.get_latest_messages(1, "Inbox")


def test_graph_parses_text_body_and_missing_fields():
    payload = {
        "value": [
            {
                "id": "message-1",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": None,
                "subject": None,
                "body": {"contentType": "text", "content": "Code 482910"},
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=_Response(payload)):
        [message] = _graph().get_latest_messages(5, "spam")

    assert message.sender == ""
    assert message.subject == ""
    assert message.body_text == "Code 482910"
    assert message.body_html is None
    assert message.received_at.utcoffset().total_seconds() == 0


def test_graph_falls_back_to_sender_name_without_address():
    payload = {
        "value": [
            {
                "id": "message-1",
                "receivedDateTime": "2026-11-18T10:15:23Z",
                "from": {"emailAddress": {"name": "Example Security"}},
                "subject": "Hello",
                "body": {"contentType": "text", "content": ""},
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=_Response(payload)):
        [message] = _graph().get_latest_messages(1, "Inbox")
    assert message.sender == "Example Security"


# --- IMAP ------------------------------------------------------------------


class FakeImap:
    """In-memory stand-in for imaplib.IMAP4_SSL with synthetic messages."""

    def __init__(self, messages=None, *, auth_status="OK", auth_error=None,
                 select_status="OK", fetch_error=None, logout_error=None):
        self.messages = messages or {}
        self.auth_status = auth_status
        self.auth_error = auth_error
        self.select_status = select_status
        self.fetch_error = fetch_error
        self.logout_error = logout_error
        self.selected = None
        self.fetched: list[bytes] = []
        self.logged_out = False

    def authenticate(self, _mechanism, callback):
        callback(b"")
        if self.auth_error:
            raise self.auth_error
        return self.auth_status, []

    def select(self, mailbox, readonly=False):
        assert readonly is True
        self.selected = mailbox
        return self.select_status, [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        # Mirror imaplib: UID SEARCH/FETCH dispatch to search()/fetch().
        return getattr(self, command.lower())(*args)

    def search(self, *_args):
        return "OK", [b" ".join(self.messages)]

    def fetch(self, message_id, _parts):
        if self.fetch_error:
            raise self.fetch_error
        self.fetched.append(message_id)
        return "OK", [(message_id + b" (BODY[])", self.messages[message_id])]

    def logout(self):
        self.logged_out = True
        if self.logout_error:
            raise self.logout_error
        return "BYE", []


def _imap_read(fake, top=10, folder="Inbox"):
    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake):
        provider = ImapMailboxProvider("reader@example.com", SYNTHETIC_TOKEN, ConfigOptions())
        try:
            return provider.get_latest_messages(top, folder)
        finally:
            provider.close()


def _raw(headers: str, body: str) -> bytes:
    return (headers.strip().replace("\n", "\r\n") + "\r\n\r\n" + body).encode("utf-8")


MULTIPART = _raw(
    """
From: Example Security <security@example.com>
Subject: Your verification code
Date: Wed, 18 Nov 2026 10:15:23 +0000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="outer"
""",
    "--outer\r\n"
    'Content-Type: multipart/alternative; boundary="inner"\r\n\r\n'
    "--inner\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    "Your verification code is 482910\r\n"
    "--inner\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n"
    "<p>Your verification code is <b>482910</b></p>\r\n"
    "--inner--\r\n"
    "--outer\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    'Content-Disposition: attachment; filename="note.txt"\r\n\r\n'
    "Attachment 999999\r\n"
    "--outer--\r\n",
)

HTML_ONLY = _raw(
    """
From: security@example.com
Subject: Code
Date: Wed, 18 Nov 2026 10:16:23 +0000
Content-Type: text/html; charset=utf-8
""",
    "<style>p{color:#000000}</style><p>Code <b>135790</b></p>",
)

NO_DATE = _raw(
    """
From: security@example.com
Subject: No date
Content-Type: text/plain; charset=utf-8
""",
    "Code 246810",
)


def test_imap_parses_multipart_and_skips_attachments():
    [message] = _imap_read(FakeImap({b"1": MULTIPART}))
    assert message.sender == "Example Security <security@example.com>"
    assert message.body_text.strip() == "Your verification code is 482910"
    assert "<b>482910</b>" in message.body_html
    assert "999999" not in message.body_text
    assert message.message_id == "1"


def test_imap_html_only_message_is_normalized():
    [message] = _imap_read(FakeImap({b"1": HTML_ONLY}))
    assert message.body_text == "Code 135790"
    assert message.body_html is not None


def test_imap_skips_message_without_date():
    assert _imap_read(FakeImap({b"1": NO_DATE})) == []


def test_imap_returns_newest_first_limited_to_top():
    fake = FakeImap({b"1": HTML_ONLY, b"2": HTML_ONLY, b"3": MULTIPART})
    messages = _imap_read(fake, top=2)
    assert fake.fetched == [b"3", b"2"]
    assert [m.message_id for m in messages] == ["3", "2"]


@pytest.mark.parametrize(("folder", "mailbox"), [("Junk", "Junk"), ("spam", "Junk"), ("Inbox", "INBOX")])
def test_imap_maps_folder_names(folder, mailbox):
    fake = FakeImap()
    assert _imap_read(fake, folder=folder) == []
    assert fake.selected == mailbox


@pytest.mark.parametrize(
    "fake",
    [FakeImap(auth_status="NO"), FakeImap(auth_error=imaplib.IMAP4.error("denied"))],
)
def test_imap_auth_failure_raises_and_logs_out(fake):
    with (
        patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake),
        pytest.raises(MailboxProviderError, match="authentication failed") as info,
    ):
        ImapMailboxProvider("reader@example.com", SYNTHETIC_TOKEN, ConfigOptions()).get_latest_messages(1, "Inbox")
    assert fake.logged_out
    assert SYNTHETIC_TOKEN not in str(info.value)


def test_imap_select_failure_raises():
    with pytest.raises(MailboxProviderError, match="select"):
        _imap_read(FakeImap(select_status="NO"))


def test_imap_fetch_error_raises():
    fake = FakeImap({b"1": MULTIPART}, fetch_error=imaplib.IMAP4.error("fetch broke"))
    with pytest.raises(MailboxProviderError, match="fetch failed"):
        _imap_read(fake)


def test_imap_close_swallows_logout_error_and_rejects_reads():
    fake = FakeImap(logout_error=imaplib.IMAP4.error("bye failed"))
    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake):
        provider = ImapMailboxProvider("reader@example.com", SYNTHETIC_TOKEN, ConfigOptions())
        provider.get_latest_messages(1, "Inbox")
        provider.close()
    assert fake.logged_out
    with pytest.raises(MailboxProviderError, match="closed"):
        provider.get_latest_messages(1, "Inbox")


def test_imap_reuses_authenticated_connection():
    fake = FakeImap({b"1": MULTIPART})
    with patch("hotmail_otp_sdk.providers.imaplib.IMAP4_SSL", return_value=fake) as ctor:
        provider = ImapMailboxProvider("reader@example.com", SYNTHETIC_TOKEN, ConfigOptions())
        provider.get_latest_messages(1, "Inbox")
        provider.get_latest_messages(1, "Inbox")
        provider.close()
    assert ctor.call_count == 1
