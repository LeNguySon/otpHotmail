"""Wait for a verification code in an authorized mailbox.

Set HOTMAIL_OTP_ACCOUNT to "email|password|refresh_token|client_id" for an
account you own or are authorized to read, then run:

    python examples/wait_for_otp.py
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from hotmail_otp_sdk import ConfigOptions, OtpOptions, OtpQuery, OtpReader


def main() -> int:
    account_line = os.environ.get("HOTMAIL_OTP_ACCOUNT")
    if not account_line:
        print("Set HOTMAIL_OTP_ACCOUNT first.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO)  # SDK logs are redacted

    with OtpReader.from_format(account_line, ConfigOptions(fallback_to_imap=True)) as reader:
        result = reader.wait_for_otp(
            OtpQuery(
                since=datetime.now(timezone.utc) - timedelta(minutes=2),
                sender_allowlist=("@example.com",),
                folder="both",
            ),
            OtpOptions(timeout_ms=90_000, poll_interval_ms=3_000),
        )
        rotated = reader.current_refresh_token

    if result.status != "SUCCESS":
        print(f"{result.status}: {result.error or ', '.join(result.candidates)}", file=sys.stderr)
        return 1
    print(result.code)
    if rotated != account_line.split("|")[2].strip():
        print("Refresh token was rotated; store the new one securely.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
