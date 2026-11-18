# Hotmail / Outlook OTP SDK

Python SDK for reading OTP and mailbox messages from authorized Microsoft 365,
Hotmail, and Outlook accounts.

## Install

```powershell
python -m pip install -e ".[test]"   # runtime + pytest
python -m pip install -e ".[dev]"    # adds pytest-cov, ruff and mypy
```

## Usage

Use only mailboxes you own or are explicitly authorized to access.

```python
from datetime import datetime, timedelta, timezone

from hotmail_otp_sdk import (
    ConfigOptions,
    OtpOptions,
    OtpQuery,
    OtpReader,
)

with OtpReader.from_format(
    "user@example.com|password|refresh-token|client-id",
    ConfigOptions(provider="GRAPH", fallback_to_imap=True),
) as reader:
    result = reader.wait_for_otp(
        OtpQuery(
            since=datetime.now(timezone.utc) - timedelta(minutes=5),
            sender_allowlist=("noreply@example.com", "@example.com"),
            subject_contains="verification",
            folder="both",
        ),
        OtpOptions(
            timeout_ms=60_000,
            poll_interval_ms=3_000,
            max_messages_to_check=10,
            include_body_snippet=False,
        ),
    )
    print(result.status, result.code)
```

`OtpReader` is a context manager; leaving the `with` block calls `close()`.
Calling `close()` explicitly is equivalent.

To read recent messages without polling:

```python
messages = reader.get_latest_messages(top=10, folder="both")  # Inbox + Junk, newest first
for message in messages:
    print(message.received_at, message.sender, message.subject)
```

To load several accounts (one `email|password|refresh_token|client_id` per
line; blank lines and `#` comments are ignored):

```python
from hotmail_otp_sdk import OtpReader, load_accounts, parse_accounts

accounts = load_accounts("accounts.txt")          # or parse_accounts(list_of_lines)
for account in accounts:
    with OtpReader(account) as reader:
        ...
```

A malformed line raises `InvalidCredentialFormat` with its line number; the
line content is never included in the error.

Use synthetic credentials in tests. Never write real passwords, refresh tokens,
access tokens, OTP values, or mailbox content into source, logs, fixtures, or
telemetry.

## Configuration

`ConfigOptions` supports:

- `token_endpoint`: Microsoft OAuth2 token endpoint
- `graph_base_url`: Microsoft Graph base URL
- `imap_host` and `imap_port`: IMAP XOAUTH2 fallback server. IMAP messages
  are identified by UID and dated by the server's `INTERNALDATE` (the sender's
  `Date` header is only used when `INTERNALDATE` is unavailable)
- `provider`: `GRAPH` or `IMAP`
- `fallback_to_imap`: allow Graph failures to fall back to IMAP
- `oauth_scope`: OAuth scope requested for Microsoft Graph tokens
- `imap_oauth_scope`: OAuth scope requested for IMAP XOAUTH2 tokens
  (default `https://outlook.office.com/IMAP.AccessAsUser.All offline_access`);
  the `client_id` must be consented for this scope for IMAP to work
- `http_timeout_seconds`: network timeout
- `max_retries`: maximum Graph attempts for transient failures
- `retry_backoff_seconds`: base delay for exponential retry backoff

## Tests

```powershell
python -m pytest
python -m ruff check
python -m mypy
```

CI (`.github/workflows/ci.yml`) runs the same checks on Ubuntu and Windows for
Python 3.10–3.13.

## Result behavior

- `SUCCESS`: one OTP candidate ranked clearly highest. When a code was
  resent, the code from the newest message wins.
- `AMBIGUOUS`: multiple candidates remained equally likely (`candidates` lists
  them).
- `TIMEOUT`: no matching OTP arrived before the overall timeout, or a
  throttling `Retry-After` would pass the timeout.
- `ERROR`: a provider or token failure (`error` holds the exception code, such
  as `MailboxProviderError` or `TokenRefreshFailed`), an invalid regex
  (`InvalidOtpPattern`), or invalid options.
- `GRAPH` or `IMAP`: identifies the provider that returned the result.

Graph provider failures can fall back to IMAP when `fallback_to_imap=True`.
Throttling (HTTP 429) does not trigger the fallback; the reader waits for
`Retry-After` within the timeout instead. Network waits, retries and polling are
bounded by `timeout_ms`; a single network operation may still take up to one
second past it.

Microsoft may rotate the refresh token. Read `reader.current_refresh_token`
before `close()` and store it securely if you persist credentials.

Close the reader when finished so cached access and refresh tokens are dropped.
`close()` does not modify a `HotmailAccount` you passed in.
