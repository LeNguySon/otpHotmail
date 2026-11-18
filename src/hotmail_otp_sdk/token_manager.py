import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .exceptions import AccountLockedOrDisabled, TokenRefreshFailed
from .models import ConfigOptions, HotmailAccount


class TokenManager:
    def __init__(self, account: HotmailAccount, options: ConfigOptions):
        self._account = account
        self._options = options
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._refresh_token = account.refresh_token
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token

            body = urllib.parse.urlencode({
                "client_id": self._account.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": self._options.oauth_scope,
            }).encode("utf-8")
            request = urllib.request.Request(
                self._options.token_endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._options.http_timeout_seconds,
                ) as response:
                    import json
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401):
                    raise TokenRefreshFailed(
                        "Microsoft rejected the refresh token or client id."
                    ) from exc
                if exc.code in (403,):
                    raise AccountLockedOrDisabled(
                        "The Microsoft account is locked or disabled."
                    ) from exc
                raise TokenRefreshFailed(
                    f"Token refresh failed with HTTP {exc.code}."
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise TokenRefreshFailed("Token endpoint is unreachable.") from exc

            access_token = payload.get("access_token")
            if not access_token:
                raise TokenRefreshFailed("Token response did not contain access_token.")

            expires_in = payload.get("expires_in", 3600)
            try:
                lifetime = max(60, int(expires_in) - 60)
            except (TypeError, ValueError):
                lifetime = 3_540

            self._access_token = access_token
            self._expires_at = time.monotonic() + lifetime
            if payload.get("refresh_token"):
                self._refresh_token = payload["refresh_token"]
            return access_token

    def close(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0
            self._refresh_token = ""
