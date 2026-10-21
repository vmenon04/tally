import smtplib
import ssl
from typing import Any

import pytest

from tally import mailer


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL and records what was done with it."""

    instances: list["FakeSMTP"] = []
    fail_login = False

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext, timeout: int) -> None:
        self.host, self.port, self.context = host, port, context
        self.logins: list[tuple[str, str]] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.logins.append((user, password))

    def send_message(self, msg: Any) -> None:
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances, FakeSMTP.fail_login = [], False
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_message() -> None:
    msg = mailer.message("alerts@example.com", "me@example.com", "tally: week", "Body – text")
    assert (msg["From"], msg["To"], msg["Subject"]) == (
        "alerts@example.com",
        "me@example.com",
        "tally: week",
    )
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg["Date"]
    assert msg.get_content() == "Body – text\n"


def test_send_logs_in_over_tls(smtp: type[FakeSMTP]) -> None:
    msg = mailer.message("alerts@example.com", "me@example.com", "s", "b")
    mailer.send("alerts@example.com", "abcdefghijklmnop", msg)
    [conn] = smtp.instances
    assert (conn.host, conn.port) == ("smtp.gmail.com", 465)
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.logins == [("alerts@example.com", "abcdefghijklmnop")]
    assert conn.sent == [msg]


def test_refused_password_is_never_shown(smtp: type[FakeSMTP]) -> None:
    smtp.fail_login = True
    msg = mailer.message("alerts@example.com", "me@example.com", "s", "b")
    with pytest.raises(mailer.MailError, match=r"refused the app password .*\(535\)") as error:
        mailer.send("alerts@example.com", "abcdefghijklmnop", msg)
    assert "abcdefghijklmnop" not in str(error.value)


def test_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    msg = mailer.message("alerts@example.com", "me@example.com", "s", "b")
    with pytest.raises(mailer.MailError, match="ConnectionRefusedError"):
        mailer.send("alerts@example.com", "abcdefghijklmnop", msg)
