import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from hotmail_otp_sdk import AccountLockedOrDisabled, TokenRefreshFailed
from hotmail_otp_sdk.models import ConfigOptions, HotmailAccount
from hotmail_otp_sdk.token_manager import TokenManager

SYNTHETIC_REFRESH = "synthetic-refresh-value"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))

    def __exit__(self, *_):
        return False


def _manager() -> TokenManager:
    account = HotmailAccount(
        email="reader@example.com",
        password="synthetic-password",
        refresh_token=SYNTHETIC_REFRESH,
        client_id="synthetic-client",
    )
    return TokenManager(account, ConfigOptions())


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://login.test/token", code, "error", {}, None)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (400, TokenRefreshFailed),
        (401, TokenRefreshFailed),
        (403, AccountLockedOrDisabled),
        (500, TokenRefreshFailed),
    ],
)
def test_http_errors_map_to_public_exceptions(code, expected):
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(code)),
        pytest.raises(expected) as info,
    ):
        _manager().get_access_token()
    # Error text must never leak secrets.
    assert SYNTHETIC_REFRESH not in str(info.value)
    assert "synthetic-password" not in str(info.value)


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("unreachable"), TimeoutError("timed out")],
)
def test_unreachable_endpoint_raises_token_refresh_failed(error):
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(TokenRefreshFailed, match="unreachable"),
    ):
        _manager().get_access_token()


def test_missing_access_token_raises():
    with (
        patch("urllib.request.urlopen", return_value=_Response({"token_type": "Bearer"})),
        pytest.raises(TokenRefreshFailed, match="access_token"),
    ):
        _manager().get_access_token()


def test_invalid_expires_in_falls_back_to_default_lifetime():
    payload = {"access_token": "synthetic-access", "expires_in": "not-a-number"}
    with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
        manager = _manager()
        assert manager.get_access_token() == "synthetic-access"
        assert manager.get_access_token() == "synthetic-access"
    assert urlopen.call_count == 1


def test_expired_token_is_refreshed():
    payloads = iter((
        {"access_token": "synthetic-access-1", "expires_in": 3600},
        {"access_token": "synthetic-access-2", "expires_in": 3600},
    ))
    with patch("urllib.request.urlopen", side_effect=lambda *_a, **_k: _Response(next(payloads))):
        manager = _manager()
        scope = ConfigOptions().oauth_scope
        assert manager.get_access_token() == "synthetic-access-1"
        # Force the cached entry to look expired.
        manager._tokens[scope] = (manager._tokens[scope][0], 0.0)
        assert manager.get_access_token() == "synthetic-access-2"


def test_token_request_never_sends_password():
    seen: list[bytes] = []

    def fake_urlopen(request, **_kwargs):
        seen.append(request.data)
        return _Response({"access_token": "synthetic-access"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _manager().get_access_token()

    assert b"synthetic-password" not in seen[0]
    assert b"grant_type=refresh_token" in seen[0]


def test_close_clears_cached_tokens():
    with patch("urllib.request.urlopen", return_value=_Response({"access_token": "synthetic-access"})):
        manager = _manager()
        manager.get_access_token()
    manager.close()
    assert manager._tokens == {}
    assert manager._refresh_token == ""
