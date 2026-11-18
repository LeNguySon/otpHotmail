import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .exceptions import (
    HotmailOtpError,
    MailboxProviderError,
    Throttled,
)
from .models import ConfigOptions, HotmailAccount, MailResult, OtpOptions, OtpQuery, OtpResult
from .otp_extractor import extract_otp_candidates_with_scores, filter_messages
from .parser import parse_account
from .providers import GraphMailboxProvider, ImapMailboxProvider, MailboxProvider
from .token_manager import TokenManager


def _folders(folder_name: str) -> tuple[str, ...]:
    folder = folder_name.strip().lower()
    if folder in ("both", "all", "inbox,junk", "inbox+junk"):
        return ("Inbox", "Junk")
    if folder in ("junk", "spam", "junkemail"):
        return ("Junk",)
    return ("Inbox",)


@dataclass(frozen=True)
class _RankedCandidate:
    code: str
    score: int
    timestamp: float
    message: MailResult


def _timestamp(value: datetime) -> float:
    # IMAP may yield naive datetimes (e.g. "-0000" Date headers); treat them as
    # UTC so they can be compared with timezone-aware Graph timestamps.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _rank_candidates(
    messages: Iterable[MailResult],
    pattern: str | None,
) -> list[_RankedCandidate]:
    """Return one entry per distinct code: best score, then newest message."""
    best: dict[str, _RankedCandidate] = {}
    for message in messages:
        timestamp = _timestamp(message.received_at)
        for code, score in extract_otp_candidates_with_scores(message, pattern):
            current = best.get(code)
            if current is None or (score, timestamp) > (current.score, current.timestamp):
                best[code] = _RankedCandidate(code, score, timestamp, message)
    return sorted(best.values(), key=lambda item: (-item.score, -item.timestamp, item.code))


def _pick_winner(ranked: list[_RankedCandidate]) -> _RankedCandidate | None:
    if not ranked:
        return None
    top = ranked[0]
    if len(ranked) == 1:
        return top
    second = ranked[1]
    if top.score > second.score:
        return top
    # Equal scores: when both codes sit next to verification wording (score > 1)
    # and come from different points in time, the newest message wins. This is
    # the usual "resend code" case, where the older code is already stale.
    # Ties within one message, or between low-context numbers, stay ambiguous.
    if top.score > 1 and top.timestamp > second.timestamp:
        return top
    return None


class OtpReader:
    def __init__(self, account: HotmailAccount, options: ConfigOptions | None = None):
        self.account = account
        self.options = options or ConfigOptions()
        self.token_manager = TokenManager(account, self.options)
        self._provider: MailboxProvider | None = None
        self._provider_name = "GRAPH"
        self._closed = False

    @staticmethod
    def from_format(account_line: str, options: ConfigOptions | None = None) -> "OtpReader":
        return OtpReader(parse_account(account_line), options)

    def __enter__(self) -> "OtpReader":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _graph_token(self) -> str:
        return self.token_manager.get_access_token(self.options.oauth_scope)

    def _imap_token(self) -> str:
        return self.token_manager.get_access_token(self.options.imap_oauth_scope)

    def _new_imap_provider(self) -> ImapMailboxProvider:
        return ImapMailboxProvider(self.account.email, self._imap_token, self.options)

    def _create_provider(self) -> MailboxProvider:
        # Providers receive token callables (not a token snapshot) so cached
        # tokens are refreshed by TokenManager once they expire.
        if self.options.provider.upper() == "IMAP":
            self._provider_name = "IMAP"
            return self._new_imap_provider()
        self._provider_name = "GRAPH"
        return GraphMailboxProvider(self._graph_token, self.options)

    @property
    def current_refresh_token(self) -> str:
        """The refresh token in use, including one rotated by Microsoft.

        Persist this value (securely) after reading if the caller stores
        credentials; Microsoft may replace the original refresh token.
        """
        return self.token_manager.current_refresh_token

    def _read_from_provider(
        self,
        provider: MailboxProvider,
        top: int,
        folders: Iterable[str],
        deadline: float | None,
    ) -> list[MailResult]:
        results: list[MailResult] = []
        for folder in folders:
            results.extend(provider.get_latest_messages(top, folder, deadline=deadline))
        return results

    def _read_messages(
        self,
        top: int,
        folders: Iterable[str],
        deadline: float | None = None,
    ) -> tuple[list[MailResult], str]:
        if self._closed:
            raise MailboxProviderError("OtpReader is closed.")

        if self._provider is None:
            self._provider = self._create_provider()

        folders = tuple(folders)
        try:
            return (
                self._read_from_provider(self._provider, top, folders, deadline),
                self._provider_name,
            )
        except Throttled:
            # Throttling is transient and Graph is healthy: let the caller
            # honor Retry-After instead of abandoning Graph for IMAP.
            raise
        except MailboxProviderError:
            if (
                self._provider_name == "GRAPH"
                and self.options.fallback_to_imap
                and self.options.provider.upper() != "IMAP"
            ):
                self._provider.close()
                self._provider = self._new_imap_provider()
                self._provider_name = "IMAP"
                return (
                    self._read_from_provider(self._provider, top, folders, deadline),
                    self._provider_name,
                )
            raise

    def _timeout(self) -> OtpResult:
        return OtpResult(
            status="TIMEOUT",
            email=self.account.email,
            provider=self._provider_name,
            error="Timeout",
        )

    def wait_for_otp(self, query: OtpQuery, options: OtpOptions | None = None) -> OtpResult:
        opts = options or OtpOptions()
        if opts.timeout_ms < 0 or opts.poll_interval_ms < 0 or opts.max_messages_to_check < 1:
            return OtpResult(
                status="ERROR",
                email=self.account.email,
                provider=self._provider_name,
                error="Otp options contain invalid values.",
            )
        if query.otp_pattern:
            try:
                re.compile(query.otp_pattern)
            except re.error:
                return OtpResult(
                    status="ERROR",
                    email=self.account.email,
                    provider=self._provider_name,
                    error="InvalidOtpPattern",
                )

        effective_query = query
        if effective_query.since is None:
            effective_query = OtpQuery(
                since=datetime.now(timezone.utc) - timedelta(minutes=5),
                sender_allowlist=effective_query.sender_allowlist,
                subject_contains=effective_query.subject_contains,
                otp_pattern=effective_query.otp_pattern,
                folder=effective_query.folder,
            )

        deadline = time.monotonic() + (opts.timeout_ms / 1000)
        while True:
            try:
                raw_messages, provider_name = self._read_messages(
                    opts.max_messages_to_check,
                    _folders(effective_query.folder),
                    deadline,
                )
                messages = filter_messages(raw_messages, effective_query)
                ranked = _rank_candidates(messages, effective_query.otp_pattern)
                winner = _pick_winner(ranked)
                if winner is not None:
                    message = winner.message
                    snippet = message.body_text[:240] if opts.include_body_snippet else None
                    return OtpResult(
                        status="SUCCESS",
                        email=self.account.email,
                        code=winner.code,
                        message_id=message.message_id,
                        received_at=message.received_at,
                        sender=message.sender,
                        subject=message.subject,
                        body_snippet=snippet,
                        provider=provider_name,
                    )
                if ranked:
                    return OtpResult(
                        status="AMBIGUOUS",
                        email=self.account.email,
                        candidates=tuple(item.code for item in ranked),
                        provider=provider_name,
                        error="AmbiguousOtp",
                    )
            except HotmailOtpError as exc:
                if isinstance(exc, Throttled):
                    retry_delay = exc.retry_after
                    if retry_delay is not None:
                        remaining = deadline - time.monotonic()
                        # Waiting out Retry-After would pass the deadline.
                        if remaining <= 0 or retry_delay >= remaining:
                            return self._timeout()
                        time.sleep(retry_delay)
                        continue
                else:
                    return OtpResult(
                        status="ERROR",
                        email=self.account.email,
                        provider=self._provider_name,
                        error=exc.code,
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._timeout()
            # Never sleep past the overall deadline.
            time.sleep(min(max(0.0, opts.poll_interval_ms / 1000), remaining))

    def get_latest_messages(self, top: int = 10, folder: str = "Inbox") -> list[MailResult]:
        if top < 1:
            raise ValueError("top must be at least 1.")
        messages, _ = self._read_messages(top, _folders(folder))
        # Folders are read separately; merge them newest first before trimming.
        messages.sort(key=lambda message: _timestamp(message.received_at), reverse=True)
        return messages[:top]

    def close(self) -> None:
        if self._provider is not None:
            self._provider.close()
            self._provider = None
        self.token_manager.close()
        # Drop this reader's reference to the secrets without mutating the
        # caller's (frozen) HotmailAccount, which may still be in use.
        self.account = HotmailAccount(
            email=self.account.email,
            password="",
            refresh_token="",
            client_id="",
        )
        self._closed = True
