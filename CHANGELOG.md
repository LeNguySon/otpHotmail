# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - Unreleased

### Added
- Multi-language OTP context scoring and extraction supporting English,
  Vietnamese, Chinese (Simplified & Traditional), Japanese, Korean, Russian,
  Spanish, French, German, Portuguese, Italian, Indonesian, and Thai.
- Extended credential line support: account parser accepts >= 4 pipe-separated
  fields, extracting the primary 4 fields while ignoring trailing fields.
- `run_live_test.py` interactive smoke test tool for authorized mailbox
  verification.
- `hotmail-otp` command-line tool with `wait` and `list` subcommands. The
  account is read from `HOTMAIL_OTP_ACCOUNT`, `--account-file` or stdin, never
  from arguments. Exit codes: 0 success, 1 error, 2 usage, 3 timeout,
  4 ambiguous.
- `parse_accounts()` and `load_accounts()` for batch input.
- `OtpReader` context manager support and `OtpReader.current_refresh_token`
  for persisting refresh tokens rotated by Microsoft.
- `ConfigOptions.imap_oauth_scope` (IMAP needs an Outlook-scoped token).
- `get_latest_messages(folder="both")` merges Inbox and Junk newest first.
- Redacted library logging under the `hotmail_otp_sdk` logger (silent by
  default). Emails are masked, and tokens, passwords, OTPs and message bodies
  are never logged.
- MIT license (`LICENSE`).
- `py.typed` marker, `__version__`, package metadata, ruff/mypy configuration
  and a GitHub Actions CI workflow.

### Changed
- IMAP reads by UID and dates messages by the server's `INTERNALDATE` rather
  than the sender-controlled `Date` header.
- OTP scoring measures the character gap to verification wording, recognizes
  more phrases (including Vietnamese), weights a bare "code" lower, and treats
  year-like numbers dynamically instead of hard-coding 2025/2026.
- When a code is resent, the newest message's code wins over an equally
  ranked older one.
- Throttling (HTTP 429) no longer triggers the IMAP fallback; `Retry-After` is
  honored within `timeout_ms`.
- Network waits, retries and polling are bounded by `timeout_ms`.
- An invalid custom regex reports `InvalidOtpPattern` (was `OtpNotFound`).
- `close()` no longer mutates the caller's `HotmailAccount`.
- Provider `get_latest_messages()` accepts a keyword-only `deadline`.

### Fixed
- Every Microsoft Graph request failed because the OData query contained an
  unencoded space.
- IMAP XOAUTH2 always failed: the SASL string was base64-encoded twice and
  the token carried the Graph scope.
- Expired access tokens were reused for the reader's lifetime.
- CSS and script content in HTML mail produced false OTP candidates.
- Network, TLS and malformed-payload errors escaped `wait_for_otp` as raw
  exceptions instead of `ERROR` results.
- HTML entities were unescaped twice.

## [0.1.0] - 2026-11-18

- Initial implementation: credential parser, token manager, Microsoft Graph
  and IMAP providers, OTP extractor and `OtpReader`.
