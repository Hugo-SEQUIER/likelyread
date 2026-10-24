import pytest

MAIL_SETTINGS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM")


@pytest.fixture(autouse=True)
def no_real_email(monkeypatch):
    """Mail settings from the shell never reach a test, so no test can send an email."""
    for k in MAIL_SETTINGS:
        monkeypatch.delenv(k, raising=False)
