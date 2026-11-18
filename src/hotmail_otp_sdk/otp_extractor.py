import html
import re
from html.parser import HTMLParser
from typing import Iterable

from .models import MailResult, OtpQuery


DEFAULT_OTP_PATTERN = re.compile(r"\b\d{4,8}\b")
AUTH_CONTEXT = re.compile(
    r"\b(verification code|otp|passcode|one[- ]time password|security code|mã xác nhận)\b",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


def normalize_html(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
    except Exception:
        return re.sub(r"<[^>]+>", " ", value)
    text = " ".join(parser.parts)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def normalize_sender(value: str) -> str:
    match = re.search(r"<([^<>]+)>", value)
    return (match.group(1) if match else value).strip().lower()


def matches_allowlist(sender: str, allowlist: Iterable[str]) -> bool:
    normalized_sender = normalize_sender(sender)
    entries = tuple(item.strip().lower() for item in allowlist if item.strip())
    if not entries:
        return True
    for entry in entries:
        if entry.startswith("@"):
            if normalized_sender.endswith(entry):
                return True
        elif normalized_sender == entry:
            return True
    return False


def extract_otp_candidates(message: MailResult, pattern: str | None = None) -> tuple[str, ...]:
    ranked = extract_otp_candidates_with_scores(message, pattern)
    return tuple(candidate for candidate, _ in ranked)


def extract_otp_candidates_with_scores(
    message: MailResult,
    pattern: str | None = None,
) -> tuple[tuple[str, int], ...]:
    regex = re.compile(pattern) if pattern else DEFAULT_OTP_PATTERN
    text = f"{message.subject}\n{message.body_text}"
    candidates: dict[str, int] = {}
    for match in regex.finditer(text):
        candidate = match.group(0)
        if candidate.isdigit():
            if candidate in {"2025", "2026"} or len(candidate) == 10:
                continue

        score = 1
        for auth_match in AUTH_CONTEXT.finditer(text):
            distance = abs(auth_match.start() - match.start())
            if distance <= 20:
                score += 8
            elif distance <= 50:
                score += 5
        candidates[candidate] = candidates.get(candidate, 0) + score

    return tuple(
        sorted(
            ((candidate, score) for candidate, score in candidates.items()),
            key=lambda item: (-item[1], item[0]),
        )
    )


def filter_messages(messages: Iterable[MailResult], query: OtpQuery) -> list[MailResult]:
    filtered: list[MailResult] = []
    for message in messages:
        if query.since is not None:
            received_at = message.received_at
            since = query.since
            if received_at.tzinfo is None and since.tzinfo is not None:
                since = since.replace(tzinfo=None)
            elif received_at.tzinfo is not None and since.tzinfo is None:
                received_at = received_at.replace(tzinfo=None)
            if received_at < since:
                continue
        if not matches_allowlist(message.sender, query.sender_allowlist):
            continue
        if query.subject_contains:
            if query.subject_contains.lower() not in message.subject.lower():
                continue
        filtered.append(message)
    return filtered
