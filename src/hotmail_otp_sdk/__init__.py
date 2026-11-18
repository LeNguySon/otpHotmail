"""Read OTP codes and messages from authorized Hotmail/Outlook mailboxes."""

import logging

from .exceptions import (
    AccountLockedOrDisabled,
    AmbiguousOtp,
    HotmailOtpError,
    InvalidCredentialFormat,
    MailboxProviderError,
    OtpNotFound,
    Throttled,
    TokenRefreshFailed,
)
from .models import ConfigOptions, HotmailAccount, MailResult, OtpOptions, OtpQuery, OtpResult
from .parser import load_accounts, parse_account, parse_accounts
from .reader import OtpReader

__version__ = "0.2.0"

# Library logging is silent unless the application configures a handler.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "AccountLockedOrDisabled",
    "AmbiguousOtp",
    "ConfigOptions",
    "HotmailAccount",
    "HotmailOtpError",
    "InvalidCredentialFormat",
    "MailResult",
    "MailboxProviderError",
    "OtpNotFound",
    "OtpOptions",
    "OtpQuery",
    "OtpReader",
    "OtpResult",
    "Throttled",
    "TokenRefreshFailed",
    "__version__",
    "load_accounts",
    "parse_account",
    "parse_accounts",
]
