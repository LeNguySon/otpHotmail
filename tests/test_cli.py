import io
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hotmail_otp_sdk import MailboxProviderError, __version__
from hotmail_otp_sdk.cli import (
    ACCOUNT_ENV,
    EXIT_AMBIGUOUS,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    main,
)
from hotmail_otp_sdk.models import MailResult, OtpResult

ACCOUNT_LINE = "reader@example.com|synthetic-password|synthetic-refresh|synthetic-client"
WAIT = "hotmail_otp_sdk.cli.OtpReader.wait_for_otp"
LIST = "hotmail_otp_sdk.cli.OtpReader.get_latest_messages"


def _run(argv, *, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def account_env(monkeypatch):
    monkeypatch.setenv(ACCOUNT_ENV, ACCOUNT_LINE)


def _success(**extra):
    return OtpResult(status="SUCCESS", email="reader@example.com", code="482910", **extra)


def test_wait_prints_code_and_passes_options(account_env):
    with patch(WAIT, return_value=_success()) as wait:
        code, out, err = _run([
            "wait", "--sender", "@example.com", "--sender", "a@b.test", "--subject", "verify",
            "--folder", "Junk", "--timeout", "12.5", "--poll", "0.5", "--max-messages", "4",
            "--pattern", r"\d{6}",
        ])
    assert (code, out, err) == (EXIT_OK, "482910\n", "")
    query, options = wait.call_args.args
    assert query.sender_allowlist == ("@example.com", "a@b.test")
    assert query.subject_contains == "verify"
    assert query.folder == "Junk"
    assert query.otp_pattern == r"\d{6}"
    assert query.since is not None
    assert (options.timeout_ms, options.poll_interval_ms, options.max_messages_to_check) == (12_500, 500, 4)


def test_wait_json_output(account_env):
    received = datetime(2026, 11, 18, 10, 15, tzinfo=timezone.utc)
    with patch(WAIT, return_value=_success(received_at=received, provider="IMAP")):
        code, out, _ = _run(["wait", "--json"])
    payload = json.loads(out)
    assert code == EXIT_OK
    assert payload["code"] == "482910"
    assert payload["received_at"] == "2026-11-18T10:15:00+00:00"
    assert payload["provider"] == "IMAP"
    assert payload["candidates"] == []


@pytest.mark.parametrize(
    ("result", "exit_code", "stderr_text"),
    [
        (OtpResult(status="TIMEOUT", email="r@example.com", error="Timeout"), EXIT_TIMEOUT, "TIMEOUT"),
        (
            OtpResult(status="AMBIGUOUS", email="r@example.com", candidates=("111111", "222222")),
            EXIT_AMBIGUOUS,
            "111111, 222222",
        ),
        (OtpResult(status="ERROR", email="r@example.com", error="TokenRefreshFailed"), EXIT_ERROR, "TokenRefresh"),
    ],
)
def test_wait_non_success_exit_codes(account_env, result, exit_code, stderr_text):
    with patch(WAIT, return_value=result):
        code, out, err = _run(["wait"])
    assert code == exit_code
    assert out == ""
    assert stderr_text in err


def test_account_from_file_and_stdin(tmp_path, monkeypatch):
    monkeypatch.delenv(ACCOUNT_ENV, raising=False)
    path = tmp_path / "account.txt"
    path.write_text(f"# synthetic\n{ACCOUNT_LINE}\n", encoding="utf-8")
    with patch(WAIT, return_value=_success()) as wait:
        assert _run(["wait", "--account-file", str(path)])[0] == EXIT_OK
        assert _run(["wait", "--account-file", "-"], stdin=ACCOUNT_LINE + "\n")[0] == EXIT_OK
    assert wait.call_count == 2


def test_provider_options_are_applied(account_env):
    seen = []

    def fake_wait(self, *_args):
        seen.append(self.options)
        return _success()

    with patch(WAIT, fake_wait):
        _run(["wait", "--provider", "imap", "--no-fallback"])
    assert seen[0].provider == "IMAP"
    assert seen[0].fallback_to_imap is False


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["wait"], "", "No account given"),
        (["wait"], "not-an-account", "Line 1"),
        (["wait", "--account-file", "Z:/definitely/missing.txt"], ACCOUNT_LINE, "Cannot read account file"),
        (["wait", "--timeout", "-1"], ACCOUNT_LINE, "--timeout"),
        (["list", "--top", "0"], ACCOUNT_LINE, "--top"),
    ],
)
def test_usage_errors(monkeypatch, argv, env, message):
    monkeypatch.setenv(ACCOUNT_ENV, env)
    code, out, err = _run(argv)
    assert code == EXIT_USAGE
    assert out == ""
    assert message in err
    assert "synthetic-password" not in err


def test_provider_exception_is_reported_without_traceback(account_env):
    with patch(LIST, side_effect=MailboxProviderError("synthetic outage")):
        code, out, err = _run(["list"])
    assert code == EXIT_ERROR
    assert err.strip() == "error: MailboxProviderError: synthetic outage"
    assert out == ""


def _messages():
    return [
        MailResult(
            message_id="m1",
            received_at=datetime(2026, 11, 18, 10, 15, tzinfo=timezone.utc),
            sender="security@example.com",
            subject="Welcome",
            body_text="Synthetic body",
        )
    ]


def test_list_text_and_json(account_env):
    with patch(LIST, return_value=_messages()) as listing:
        code, out, _ = _run(["list", "--top", "3", "--folder", "both"])
        assert code == EXIT_OK
        assert out == "2026-11-18T10:15:00+00:00\tsecurity@example.com\tWelcome\n"
        assert listing.call_args.kwargs == {"top": 3, "folder": "both"}

        _, out, _ = _run(["list", "--json"])
        assert json.loads(out) == [{
            "message_id": "m1",
            "received_at": "2026-11-18T10:15:00+00:00",
            "sender": "security@example.com",
            "subject": "Welcome",
        }]

        _, out, _ = _run(["list", "--json", "--body"])
        assert json.loads(out)[0]["body_text"] == "Synthetic body"

        _, out, _ = _run(["list", "--body"])
        assert "Synthetic body" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_account_is_not_accepted_as_argument(account_env, capsys):
    with pytest.raises(SystemExit) as info:
        main(["wait", ACCOUNT_LINE])
    assert info.value.code == EXIT_USAGE
    assert "synthetic-password" not in capsys.readouterr().out
