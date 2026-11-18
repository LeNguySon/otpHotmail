import http.client
import imaplib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

from .exceptions import MailboxProviderError, Throttled
from .models import ConfigOptions, MailResult
from .otp_extractor import normalize_html

# A provider accepts either a fixed access token or a callable that returns a
# current one. The callable form lets long-lived readers pick up refreshed
# tokens instead of reusing an expired one.
TokenSource = str | Callable[[], str]

# Lower bound for a single network operation when a deadline is close, so a
# final attempt still has a realistic chance to complete.
_MIN_OPERATION_TIMEOUT = 1.0


def _resolve_token(source: TokenSource) -> str:
    return source() if callable(source) else source


def _operation_timeout(configured: float, deadline: float | None) -> float:
    if deadline is None:
        return configured
    remaining = deadline - time.monotonic()
    return max(_MIN_OPERATION_TIMEOUT, min(configured, remaining))


class MailboxProvider:
    def get_latest_messages(
        self,
        top: int,
        folder: str,
        *,
        deadline: float | None = None,
    ) -> list[MailResult]:
        """Return up to ``top`` newest messages from ``folder``.

        ``deadline`` is an optional ``time.monotonic()`` value; providers avoid
        retry waits that would pass it and bound network timeouts by it.
        """
        raise NotImplementedError

    def close(self) -> None:
        return None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphMailboxProvider(MailboxProvider):
    def __init__(self, access_token: TokenSource, options: ConfigOptions):
        self._token_source: TokenSource = access_token
        self._options = options
        self._closed = False

    def get_latest_messages(
        self,
        top: int,
        folder: str,
        *,
        deadline: float | None = None,
    ) -> list[MailResult]:
        if self._closed:
            raise MailboxProviderError("Mailbox provider is closed.")

        folder_name = "junkemail" if folder.lower() in ("junk", "spam", "junkemail") else "inbox"
        # Percent-encode the OData query: http.client rejects URLs that contain
        # raw spaces (e.g. "receivedDateTime desc").
        query = urllib.parse.urlencode(
            {
                "$select": "id,receivedDateTime,from,subject,bodyPreview,body",
                "$orderby": "receivedDateTime desc",
                "$top": max(1, min(top, 50)),
            },
            quote_via=urllib.parse.quote,
            safe="$,",
        )
        url: str | None = (
            f"{self._options.graph_base_url.rstrip('/')}/me/mailFolders/{folder_name}/messages"
            f"?{query}"
        )
        results: list[MailResult] = []
        while url:
            payload = self._request_page(url, deadline)
            for item in payload.get("value") or []:
                if len(results) >= top:
                    break
                results.append(self._to_mail_result(item))
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and len(results) < top else None
        return results

    @staticmethod
    def _to_mail_result(item: Any) -> MailResult:
        try:
            from_value = item.get("from") or {}
            sender_data = from_value.get("emailAddress") or {}
            sender = sender_data.get("address") or sender_data.get("name") or ""
            body_data = item.get("body") or {}
            content = body_data.get("content") or ""
            body_html = content if body_data.get("contentType") == "html" else None
            body_text = normalize_html(body_html) if body_html else content
            return MailResult(
                message_id=str(item["id"]),
                received_at=_parse_datetime(item["receivedDateTime"]),
                sender=sender,
                subject=item.get("subject") or "",
                body_text=body_text,
                body_html=body_html,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise MailboxProviderError(
                "Microsoft Graph returned an unexpected message payload."
            ) from exc

    def _backoff(self, attempt: int) -> float:
        delay = self._options.retry_backoff_seconds * (2 ** attempt)
        return delay + random.uniform(0, delay * 0.25)

    def _request_page(self, url: str, deadline: float | None) -> dict[str, Any]:
        attempts = max(1, self._options.max_retries)
        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {_resolve_token(self._token_source)}",
                    "Accept": "application/json",
                },
                method="GET",
            )
            error: MailboxProviderError
            cause: BaseException
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=_operation_timeout(self._options.http_timeout_seconds, deadline),
                ) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                retry_header = exc.headers.get("Retry-After") if exc.headers is not None else None
                retry_seconds = self._parse_retry_after(retry_header)
                if exc.code == 429:
                    error = Throttled("Microsoft Graph was throttled.", retry_after=retry_seconds)
                else:
                    error = MailboxProviderError(f"Microsoft Graph returned HTTP {exc.code}.")
                    if not 500 <= exc.code <= 599:
                        raise error from exc
                delay = retry_seconds if retry_seconds is not None else self._backoff(attempt)
                cause = exc
            except (OSError, http.client.HTTPException) as exc:
                # URLError and TimeoutError are OSError subclasses; this also
                # covers TLS errors and connections reset while reading.
                error = MailboxProviderError("Microsoft Graph is unreachable.")
                delay = self._backoff(attempt)
                cause = exc
            else:
                return self._decode(raw)

            out_of_time = deadline is not None and time.monotonic() + delay >= deadline
            if attempt + 1 >= attempts or out_of_time:
                raise error from cause
            time.sleep(delay)
        raise MailboxProviderError("Microsoft Graph request failed.")

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MailboxProviderError("Microsoft Graph returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise MailboxProviderError("Microsoft Graph returned an invalid response.")
        return payload

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def close(self) -> None:
        self._closed = True
        self._token_source = ""


def _safe_logout(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


def _message_body(message: EmailMessage) -> tuple[str, str | None]:
    body_html: str | None = None
    body_text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/html" and body_html is None:
                body_html = part.get_content()
            elif part.get_content_type() == "text/plain" and not body_text:
                body_text = part.get_content()
    else:
        content = message.get_content()
        if message.get_content_type() == "text/html":
            body_html = content
        else:
            body_text = content
    if body_html and not body_text:
        body_text = normalize_html(body_html)
    return body_text, body_html


class ImapMailboxProvider(MailboxProvider):
    def __init__(self, email: str, access_token: TokenSource, options: ConfigOptions):
        self._email = email
        self._token_source: TokenSource = access_token
        self._options = options
        self._client: imaplib.IMAP4_SSL | None = None
        self._closed = False

    def _connect(self, deadline: float | None) -> imaplib.IMAP4_SSL:
        if self._client is not None:
            return self._client
        # Resolve the token first so a token failure never leaves an open socket.
        access_token = _resolve_token(self._token_source)
        auth_string = f"user={self._email}\x01auth=Bearer {access_token}\x01\x01"
        try:
            client = imaplib.IMAP4_SSL(
                self._options.imap_host,
                self._options.imap_port,
                timeout=_operation_timeout(self._options.http_timeout_seconds, deadline),
            )
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxProviderError("IMAP server is unreachable.") from exc
        try:
            # imaplib base64-encodes the callback's return value itself, so the
            # raw SASL string must be returned here (encoding it again breaks
            # XOAUTH2).
            status, _ = client.authenticate(
                "XOAUTH2",
                lambda _: auth_string.encode("utf-8"),
            )
            if status != "OK":
                raise MailboxProviderError("IMAP XOAUTH2 authentication failed.")
        except MailboxProviderError:
            _safe_logout(client)
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            _safe_logout(client)
            raise MailboxProviderError("IMAP XOAUTH2 authentication failed.") from exc
        self._client = client
        return client

    def _drop_connection(self) -> None:
        if self._client is not None:
            _safe_logout(self._client)
            self._client = None

    def get_latest_messages(
        self,
        top: int,
        folder: str,
        *,
        deadline: float | None = None,
    ) -> list[MailResult]:
        if self._closed:
            raise MailboxProviderError("Mailbox provider is closed.")
        client = self._connect(deadline)
        sock = getattr(client, "sock", None)
        if sock is not None and deadline is not None:
            sock.settimeout(_operation_timeout(self._options.http_timeout_seconds, deadline))
        mailbox = "Junk" if folder.lower() in ("junk", "spam", "junkemail") else "INBOX"
        try:
            return self._read_mailbox(client, mailbox, top)
        except MailboxProviderError:
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            # The session may be broken (timeout, reset, server abort);
            # reconnect with a fresh token on the next read.
            self._drop_connection()
            raise MailboxProviderError("IMAP mailbox query failed.") from exc

    def _read_mailbox(self, client: imaplib.IMAP4_SSL, mailbox: str, top: int) -> list[MailResult]:
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise MailboxProviderError(f"Could not select IMAP mailbox {mailbox}.")
        status, data = client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        selected = list(reversed(ids[-max(1, top):]))

        results: list[MailResult] = []
        for message_id in selected:
            try:
                status, parts = client.fetch(message_id, "(BODY.PEEK[])")
            except imaplib.IMAP4.error as exc:
                raise MailboxProviderError("IMAP message fetch failed.") from exc
            if status != "OK" or not parts:
                continue
            raw = next(
                (part[1] for part in parts if isinstance(part, tuple) and len(part) == 2),
                None,
            )
            if not raw:
                continue
            result = self._to_mail_result(message_id, raw)
            if result is not None:
                results.append(result)
        return results

    @staticmethod
    def _to_mail_result(message_id: bytes, raw: bytes) -> MailResult | None:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        received = message.get("Date")
        if not received:
            return None
        try:
            received_at = parsedate_to_datetime(str(received))
            body_text, body_html = _message_body(message)  # type: ignore[arg-type]
        except (LookupError, TypeError, ValueError):
            # Malformed Date header or undecodable body: the message cannot be
            # evaluated safely, so it is skipped rather than guessed at.
            return None
        return MailResult(
            message_id=message_id.decode("ascii"),
            received_at=received_at,
            sender=str(message.get("From", "")),
            subject=str(message.get("Subject", "")),
            body_text=body_text,
            body_html=body_html,
        )

    def close(self) -> None:
        self._closed = True
        self._drop_connection()
        self._token_source = ""
