from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class HotmailAccount:
    email: str
    password: str = field(repr=False, compare=False)
    refresh_token: str
    client_id: str

    def __repr__(self) -> str:
        return (
            f"HotmailAccount(email={self.email!r}, password='***', "
            f"refresh_token='***', client_id={self.client_id!r})"
        )


@dataclass(frozen=True)
class OtpQuery:
    since: datetime | None = None
    sender_allowlist: tuple[str, ...] = ()
    subject_contains: str | None = None
    otp_pattern: str | None = None
    folder: str = "Inbox"


@dataclass(frozen=True)
class OtpOptions:
    timeout_ms: int = 60_000
    poll_interval_ms: int = 3_000
    max_messages_to_check: int = 10
    include_body_snippet: bool = False


@dataclass(frozen=True)
class ConfigOptions:
    token_endpoint: str = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    imap_host: str = "outlook.office365.com"
    imap_port: int = 993
    provider: str = "GRAPH"
    fallback_to_imap: bool = True
    oauth_scope: str = "https://graph.microsoft.com/Mail.Read offline_access"
    # IMAP XOAUTH2 needs a token issued for the Outlook resource; a Graph-scoped
    # token is rejected by the IMAP server.
    imap_oauth_scope: str = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    http_timeout_seconds: float = 20.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0


@dataclass(frozen=True)
class MailResult:
    message_id: str
    received_at: datetime
    sender: str
    subject: str
    body_text: str
    body_html: str | None = None


@dataclass(frozen=True)
class OtpResult:
    status: str
    email: str
    code: str | None = None
    message_id: str | None = None
    received_at: datetime | None = None
    sender: str | None = None
    subject: str | None = None
    body_snippet: str | None = None
    provider: str = "GRAPH"
    error: str | None = None
    candidates: tuple[str, ...] = ()
