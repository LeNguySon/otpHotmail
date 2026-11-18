import html
import re
from html.parser import HTMLParser
from typing import Iterable

from .models import MailResult, OtpQuery


DEFAULT_OTP_PATTERN = re.compile(r"\b\d{4,8}\b")
AUTH_CONTEXT = re.compile(
    r"\b(verification code|otp|passcode|one[- ]time password|security code|login code"
    r"|mã xác nhận|mã xác minh|mã xác thực)\b",
    re.IGNORECASE,
)
# Bare "code" is common in OTP mails ("Your code is ...") but also appears in
# "postal code" or "promo code", so it earns a smaller boost and is ignored
# after such qualifiers. Spans already matched by AUTH_CONTEXT are not counted
# twice.
GENERIC_CONTEXT = re.compile(r"\b(code|mã)\b", re.IGNORECASE)
NON_OTP_CODE_QUALIFIER = re.compile(
    r"\b(postal|zip|post|promo|promotion|discount|coupon|voucher|area|country"
    r"|product|tracking|order|bưu chính|giảm giá|khuyến mãi|đơn hàng)\s*$",
    re.IGNORECASE,
)
# Distances are gaps in characters between the keyword and the candidate.
_ADJACENT_GAP = 5
_STRONG_GAP = 20
_NEAR_GAP = 50
_EARLIEST_YEAR = 1900


def _gap(span: tuple[int, int], other: tuple[int, int]) -> int:
    if other[0] >= span[1]:
        return other[0] - span[1]
    if other[1] <= span[0]:
        return span[0] - other[1]
    return 0


# Content of these elements is never shown to the reader; it holds CSS/JS such
# as "color:#000000" whose digit runs would otherwise look like OTP codes.
_HIDDEN_TAGS = frozenset({"script", "style", "template"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
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
    strong = [m.span() for m in AUTH_CONTEXT.finditer(text)]
    generic = [
        m.span()
        for m in GENERIC_CONTEXT.finditer(text)
        if not any(start <= m.start() < end for start, end in strong)
        and not NON_OTP_CODE_QUALIFIER.search(text[: m.start()])
    ]
    latest_year = message.received_at.year + 1
    candidates: dict[str, int] = {}
    for match in regex.finditer(text):
        candidate = match.group(0)
        span = match.span()
        if candidate.isdigit():
            if len(candidate) == 10:  # phone-number length
                continue
            # A year-like number (e.g. "(c) 2026", dates) is only accepted
            # when verification wording sits immediately next to it, as in
            # "verification code: 2019".
            looks_like_year = (
                len(candidate) == 4 and _EARLIEST_YEAR <= int(candidate) <= latest_year
            )
            if looks_like_year and not any(
                _gap(keyword, span) <= _ADJACENT_GAP for keyword in strong
            ):
                continue

        score = 1
        for keyword in strong:
            gap = _gap(keyword, span)
            if gap <= _ADJACENT_GAP:
                score += 10
            elif gap <= _STRONG_GAP:
                score += 8
            elif gap <= _NEAR_GAP:
                score += 5
        for keyword in generic:
            gap = _gap(keyword, span)
            if gap <= _STRONG_GAP:
                score += 3
            elif gap <= _NEAR_GAP:
                score += 1
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
