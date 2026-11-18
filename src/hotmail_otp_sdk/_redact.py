"""Helpers that keep personal data out of log messages."""


def mask_email(email: str) -> str:
    """Return ``u***@example.com`` style masking for log output."""
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"
