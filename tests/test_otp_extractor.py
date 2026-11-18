from datetime import datetime, timedelta, timezone

from hotmail_otp_sdk.models import MailResult, OtpQuery
from hotmail_otp_sdk.otp_extractor import (
    extract_otp_candidates,
    filter_messages,
    normalize_html,
)


def _message(
    body: str,
    *,
    sender: str = "security@example.com",
    subject: str = "Your verification code",
    received_at: datetime | None = None,
) -> MailResult:
    return MailResult(
        message_id="test-message",
        received_at=received_at or datetime.now(timezone.utc),
        sender=sender,
        subject=subject,
        body_text=body,
        body_html=None,
    )


def test_normalize_html():
    assert normalize_html("<p>Your code: <b>482910</b></p>") == "Your code: 482910"


def test_extract_prefers_otp_near_verification_context():
    message = _message("Your verification code is 482910. Reference 12345.")
    assert extract_otp_candidates(message)[0] == "482910"


def test_rejects_phone_and_year_numbers():
    message = _message("Phone 2026-555-1234")
    candidates = extract_otp_candidates(message)
    assert "2026" not in candidates


def test_filter_uses_since_sender_and_subject():
    now = datetime.now(timezone.utc)
    old = _message("Your code is 111111", received_at=now - timedelta(hours=1))
    wrong_sender = _message(
        "Your code is 222222",
        sender="unknown@example.com",
        received_at=now,
    )
    valid = _message(
        "Your code is 333333",
        sender="No-Reply@Security.Example.com",
        received_at=now,
    )

    query = OtpQuery(
        since=now - timedelta(minutes=5),
        sender_allowlist=("@security.example.com", "no-reply@security.example.com"),
        subject_contains="verification",
    )
    assert filter_messages([old, wrong_sender, valid], query) == [valid]


def test_normalize_html_ignores_style_and_script_content():
    html = (
        "<html><head><style>.a{color:#000000}</style>"
        "<script>var n = 135790;</script></head>"
        '<body><p style="color:#333333">Your verification code is 482910</p></body></html>'
    )
    text = normalize_html(html)
    assert text == "Your verification code is 482910"
    assert extract_otp_candidates(_message(text, subject="Hello")) == ("482910",)
