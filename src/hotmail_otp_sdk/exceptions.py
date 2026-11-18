class HotmailOtpError(Exception):
    code = "MailboxProviderError"


class InvalidCredentialFormat(HotmailOtpError):
    code = "InvalidCredentialFormat"


class TokenRefreshFailed(HotmailOtpError):
    code = "TokenRefreshFailed"


class AccountLockedOrDisabled(HotmailOtpError):
    code = "AccountLockedOrDisabled"


class MailboxProviderError(HotmailOtpError):
    code = "MailboxProviderError"


class Throttled(MailboxProviderError):
    code = "Throttled"

    def __init__(self, message: str = "Microsoft throttled the request.", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AmbiguousOtp(HotmailOtpError):
    code = "AmbiguousOtp"


class OtpNotFound(HotmailOtpError):
    code = "OtpNotFound"
