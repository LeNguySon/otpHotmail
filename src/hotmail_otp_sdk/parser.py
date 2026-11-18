import os
import re
from collections.abc import Iterable

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


def parse_accounts(lines: Iterable[str]) -> list[HotmailAccount]:
    """Parse several ``email|password|refresh_token|client_id`` lines.

    Blank lines and lines starting with ``#`` are skipped. A malformed line
    raises ``InvalidCredentialFormat`` naming its 1-based line number; the line
    content is never included because it holds secrets.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    accounts: list[HotmailAccount] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        try:
            accounts.append(parse_account(line))
        except InvalidCredentialFormat as exc:
            raise InvalidCredentialFormat(f"Line {number}: {exc}") from None
    return accounts


def load_accounts(path: str | os.PathLike[str], encoding: str = "utf-8-sig") -> list[HotmailAccount]:
    """Read accounts from a text file with one account per line."""
    with open(path, encoding=encoding) as handle:
        return parse_accounts(handle)
