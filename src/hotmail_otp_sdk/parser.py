import re

from .exceptions import InvalidCredentialFormat
from .models import HotmailAccount


EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")


def parse_account(line: str) -> HotmailAccount:
    if not isinstance(line, str):
        raise InvalidCredentialFormat("Account line must be a string.")

    fields = [field.strip() for field in line.split("|")]
    if len(fields) != 4:
        raise InvalidCredentialFormat("Expected exactly 4 pipe-separated fields.")

    email, password, refresh_token, client_id = fields
    if not email or not password or not refresh_token or not client_id:
        raise InvalidCredentialFormat("All four account fields are required.")
    if not EMAIL_RE.fullmatch(email):
        raise InvalidCredentialFormat("Email address is invalid.")

    return HotmailAccount(
        email=email,
        password=password,
        refresh_token=refresh_token,
        client_id=client_id,
    )
