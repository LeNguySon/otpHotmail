import pytest

from hotmail_otp_sdk import InvalidCredentialFormat, OtpReader


def test_parse_valid_account_masks_sensitive_values():
    reader = OtpReader.from_format(
        "  user@example.com | secret | synthetic-refresh | synthetic-client "
    )
    assert reader.account.email == "user@example.com"
    assert reader.account.password == "secret"
    assert reader.account.refresh_token == "synthetic-refresh"
    assert "secret" not in repr(reader.account)
    assert "synthetic-refresh" not in repr(reader.account)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "user@example.com|secret|refresh",
        "not-an-email|secret|refresh|client",
        "user@example.com||refresh|client",
    ],
)
def test_parse_rejects_invalid_account_formats(line):
    with pytest.raises(InvalidCredentialFormat):
        OtpReader.from_format(line)
