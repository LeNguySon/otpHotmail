# Hotmail / Outlook OTP SDK

Python SDK for reading OTP and mailbox messages from authorized Microsoft 365,
Hotmail, and Outlook accounts.

## Install

```powershell
python -m pip install -e ".[test]"
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

reader = OtpReader.from_format(
    "user@example.com|password|refresh-token|client-id",
    ConfigOptions(provider="GRAPH", fallback_to_imap=True),
)

try:
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
finally:
    reader.close()
```

To read recent messages without polling:

```python
messages = reader.get_latest_messages(top=10, folder="Inbox")
for message in messages:
    print(message.received_at, message.sender, message.subject)
```

Use synthetic credentials in tests. Never write real passwords, refresh tokens,
access tokens, OTP values, or mailbox content into source, logs, fixtures, or
telemetry.

## Configuration

`ConfigOptions` supports:

- `token_endpoint`: Microsoft OAuth2 token endpoint
- `graph_base_url`: Microsoft Graph base URL
- `imap_host` and `imap_port`: IMAP XOAUTH2 fallback server
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
python -m compileall -q src tests
```

## Result behavior

- `SUCCESS`: exactly one OTP candidate was found.
- `AMBIGUOUS`: multiple candidates remained equally likely.
- `TIMEOUT`: no matching OTP arrived before the overall timeout.
- `ERROR`: invalid options/regex or a provider/token failure.
- `GRAPH` or `IMAP`: identifies the provider that returned the result.

Graph provider failures can fall back to IMAP when `fallback_to_imap=True`.
Close the reader when finished so cached access and refresh tokens are cleared
from memory.
