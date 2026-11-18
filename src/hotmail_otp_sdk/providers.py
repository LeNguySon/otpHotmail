import imaplib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email import policy
from email.parser import BytesParser

from .exceptions import MailboxProviderError, Throttled
from .models import ConfigOptions, MailResult
from .otp_extractor import normalize_html

# A provider accepts either a fixed access token or a callable that returns a
# current one. The callable form lets long-lived readers pick up refreshed
# tokens instead of reusing an expired one.
TokenSource = str | Callable[[], str]


def _resolve_token(source: TokenSource) -> str:
    return source() if callable(source) else source


class MailboxProvider:
    def get_latest_messages(self, top: int, folder: str) -> list[MailResult]:
        raise NotImplementedError

    def close(self) -> None:
        return None


def _parse_datetime(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphMailboxProvider(MailboxProvider):
    def __init__(self, access_token: TokenSource, options: ConfigOptions):
        self._token_source: TokenSource = access_token
        self._options = options
        self._closed = False

    def get_latest_messages(self, top: int, folder: str) -> list[MailResult]:
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
            payload = self._request_page(url)
            for item in payload.get("value", []):
                if len(results) >= top:
                    break
                from_value = item.get("from") or {}
                sender_data = from_value.get("emailAddress") or {}
                sender = sender_data.get("address") or sender_data.get("name") or ""
                body_data = item.get("body") or {}
                body_html = body_data.get("content") if body_data.get("contentType") == "html" else None
                body_text = normalize_html(body_html) if body_html else body_data.get("content", "")
                received_at = _parse_datetime(item["receivedDateTime"])
                results.append(MailResult(
                    message_id=item["id"],
                    received_at=received_at,
                    sender=sender,
                    subject=item.get("subject") or "",
                    body_text=body_text,
                    body_html=body_html,
                ))
            url = payload.get("@odata.nextLink") if len(results) < top else None
        return results

    def _request_page(self, url: str) -> dict:
        for attempt in range(max(1, self._options.max_retries)):
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {_resolve_token(self._token_source)}",
                    "Accept": "application/json",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._options.http_timeout_seconds,
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After")
                retry_seconds = self._parse_retry_after(retry_after)
                if exc.code == 429:
                    if attempt + 1 >= self._options.max_retries:
                        raise Throttled(
                            "Microsoft Graph was throttled.",
                            retry_after=retry_seconds,
                        ) from exc
                elif not 500 <= exc.code <= 599:
                    raise MailboxProviderError(
                        f"Microsoft Graph returned HTTP {exc.code}."
                    ) from exc
                elif attempt + 1 >= self._options.max_retries:
                    raise MailboxProviderError(
                        f"Microsoft Graph returned HTTP {exc.code}."
                    ) from exc

                delay = retry_seconds
                if delay is None:
                    delay = self._options.retry_backoff_seconds * (2 ** attempt)
                    delay += random.uniform(0, delay * 0.25)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 >= self._options.max_retries:
                    raise MailboxProviderError("Microsoft Graph is unreachable.") from exc
                delay = self._options.retry_backoff_seconds * (2 ** attempt)
                time.sleep(delay + random.uniform(0, delay * 0.25))
        raise MailboxProviderError("Microsoft Graph request failed.")

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


class ImapMailboxProvider(MailboxProvider):
    def __init__(self, email: str, access_token: TokenSource, options: ConfigOptions):
        self._email = email
        self._token_source: TokenSource = access_token
        self._options = options
        self._client: imaplib.IMAP4_SSL | None = None
        self._closed = False

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self._client is not None:
            return self._client
        client = imaplib.IMAP4_SSL(
            self._options.imap_host,
            self._options.imap_port,
            timeout=self._options.http_timeout_seconds,
        )
        access_token = _resolve_token(self._token_source)
        auth_string = f"user={self._email}\x01auth=Bearer {access_token}\x01\x01"
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
        except (imaplib.IMAP4.error, MailboxProviderError) as exc:
            client.logout()
            if isinstance(exc, MailboxProviderError):
                raise
            raise MailboxProviderError("IMAP XOAUTH2 authentication failed.") from exc
        self._client = client
        return client

    def get_latest_messages(self, top: int, folder: str) -> list[MailResult]:
        if self._closed:
            raise MailboxProviderError("Mailbox provider is closed.")
        client = self._connect()
        mailbox = "Junk" if folder.lower() in ("junk", "spam", "junkemail") else "INBOX"
        try:
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise MailboxProviderError(f"Could not select IMAP mailbox {mailbox}.")
            status, data = client.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()
            selected = list(reversed(ids[-max(1, top):]))
        except imaplib.IMAP4.error as exc:
            raise MailboxProviderError("IMAP mailbox query failed.") from exc

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
            message = BytesParser(policy=policy.default).parsebytes(raw)
            body_html: str | None = None
            body_text = ""
            if message.is_multipart():
                for part in message.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    disposition = part.get_content_disposition()
                    if disposition == "attachment":
                        continue
                    if part.get_content_type() == "text/html" and body_html is None:
                        body_html = part.get_content()
                    elif part.get_content_type() == "text/plain" and not body_text:
                        body_text = part.get_content()
            else:
                content_type = message.get_content_type()
                content = message.get_content()
                if content_type == "text/html":
                    body_html = content
                else:
                    body_text = content
            if body_html and not body_text:
                body_text = normalize_html(body_html)
            received = message.get("Date")
            from email.utils import parsedate_to_datetime
            received_at = parsedate_to_datetime(received) if received else None
            if received_at is None:
                continue
            sender = message.get("From", "")
            results.append(MailResult(
                message_id=message_id.decode("ascii"),
                received_at=received_at,
                sender=sender,
                subject=str(message.get("Subject", "")),
                body_text=body_text,
                body_html=body_html,
            ))
        return results

    def close(self) -> None:
        self._closed = True
        if self._client is not None:
            try:
                self._client.logout()
            except imaplib.IMAP4.error:
                pass
            self._client = None
        self._token_source = ""
