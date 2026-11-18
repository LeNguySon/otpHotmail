import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

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


def _folders(query: OtpQuery) -> tuple[str, ...]:
    folder = query.folder.strip().lower()
    if folder in ("both", "all", "inbox,junk", "inbox+junk"):
        return ("Inbox", "Junk")
    if folder in ("junk", "spam", "junkemail"):
        return ("Junk",)
    return ("Inbox",)


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

    def _create_provider(self) -> MailboxProvider:
        access_token = self.token_manager.get_access_token()
        if self.options.provider.upper() == "IMAP":
            self._provider_name = "IMAP"
            return ImapMailboxProvider(
                self.account.email,
                access_token,
                self.options,
            )
        self._provider_name = "GRAPH"
        return GraphMailboxProvider(access_token, self.options)

    def _read_messages(self, top: int, folders: Iterable[str]) -> tuple[list[MailResult], str]:
        if self._closed:
            raise MailboxProviderError("OtpReader is closed.")

        if self._provider is None:
            self._provider = self._create_provider()

        try:
            results: list[MailResult] = []
            for folder in folders:
                results.extend(self._provider.get_latest_messages(top, folder))
            return results, self._provider_name
        except MailboxProviderError:
            if (
                self._provider_name == "GRAPH"
                and self.options.fallback_to_imap
                and self.options.provider.upper() != "IMAP"
            ):
                self._provider.close()
                access_token = self.token_manager.get_access_token()
                self._provider = ImapMailboxProvider(
                    self.account.email,
                    access_token,
                    self.options,
                )
                self._provider_name = "IMAP"
                results = []
                for folder in folders:
                    results.extend(self._provider.get_latest_messages(top, folder))
                return results, self._provider_name
            raise

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
                    error="OtpNotFound",
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
                    _folders(effective_query),
                )
                messages = filter_messages(raw_messages, effective_query)
                ranked: list[tuple[str, int, MailResult]] = []
                for message in messages:
                    for candidate, score in extract_otp_candidates_with_scores(
                        message,
                        effective_query.otp_pattern,
                    ):
                        ranked.append((candidate, score, message))

                unique = tuple(dict.fromkeys(candidate for candidate, _, _ in ranked))
                if len(unique) == 1:
                    candidate, message = next(
                        (candidate, mail)
                        for candidate, _, mail in ranked
                        if candidate == unique[0]
                    )
                    snippet = message.body_text[:240] if opts.include_body_snippet else None
                    return OtpResult(
                        status="SUCCESS",
                        email=self.account.email,
                        code=candidate,
                        message_id=message.message_id,
                        received_at=message.received_at,
                        sender=message.sender,
                        subject=message.subject,
                        body_snippet=snippet,
                        provider=provider_name,
                    )
                if len(unique) > 1:
                    best_candidates = {}
                    for candidate, score, _ in ranked:
                        best_candidates[candidate] = max(
                            best_candidates.get(candidate, 0),
                            score,
                        )
                    sorted_candidates = sorted(
                        best_candidates.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                    if len(sorted_candidates) > 1:
                        top_candidate, top_score = sorted_candidates[0]
                        _, second_score = sorted_candidates[1]
                        if top_score > second_score:
                            message = next(
                                mail
                                for candidate, _, mail in ranked
                                if candidate == top_candidate
                            )
                            snippet = message.body_text[:240] if opts.include_body_snippet else None
                            return OtpResult(
                                status="SUCCESS",
                                email=self.account.email,
                                code=top_candidate,
                                message_id=message.message_id,
                                received_at=message.received_at,
                                sender=message.sender,
                                subject=message.subject,
                                body_snippet=snippet,
                                provider=provider_name,
                            )
                    return OtpResult(
                        status="AMBIGUOUS",
                        email=self.account.email,
                        candidates=unique,
                        provider=provider_name,
                        error="AmbiguousOtp",
                    )
            except HotmailOtpError as exc:
                if isinstance(exc, Throttled):
                    retry_delay = exc.retry_after
                    if retry_delay is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return OtpResult(
                                status="TIMEOUT",
                                email=self.account.email,
                                provider=self._provider_name,
                                error="Timeout",
                            )
                        time.sleep(min(retry_delay, remaining))
                        continue
                else:
                    return OtpResult(
                        status="ERROR",
                        email=self.account.email,
                        provider=self._provider_name,
                        error=exc.code,
                    )

            if time.monotonic() >= deadline:
                return OtpResult(
                    status="TIMEOUT",
                    email=self.account.email,
                    provider=self._provider_name,
                    error="Timeout",
                )
            time.sleep(max(0.0, opts.poll_interval_ms / 1000))

    def get_latest_messages(self, top: int = 10, folder: str = "Inbox") -> list[MailResult]:
        if top < 1:
            raise ValueError("top must be at least 1.")
        messages, _ = self._read_messages((top), (folder,))
        return messages[:top]

    def close(self) -> None:
        if self._provider is not None:
            self._provider.close()
            self._provider = None
        self.token_manager.close()
        object.__setattr__(self.account, "password", "")
        object.__setattr__(self.account, "refresh_token", "")
        object.__setattr__(self.account, "client_id", "")
        self._closed = True
