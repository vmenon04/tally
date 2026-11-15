"""Sending the weekly summary through Gmail's SMTP server.

It signs in to gmail_account (config.toml) with an app password, saved by `tally
gmail-login` in secrets/gmail-app-password, and sends to summary_to.
"""

import email.utils
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from tally import config

HOST = "smtp.gmail.com"
PORT = 465


_APP_PASSWORD = re.compile(r"[a-z]{16}")


class MailError(Exception):
    """Gmail refused or couldn't be reached. The message never includes the password."""


def app_password(text: str) -> str:
    """The app password as Google shows it, spaces optional. Refuses anything else."""
    password = "".join(text.split())
    if not _APP_PASSWORD.fullmatch(password):
        raise MailError(
            "that isn't an app password (16 letters, shown as four groups of four). "
            "Create one at myaccount.google.com/apppasswords for the sending account."
        )
    return password


def load_password(path: Path) -> str:
    try:
        return app_password(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MailError(f"no app password at {path}: run `tally gmail-login`") from None


def save_password(path: Path, password: str) -> None:
    config.write_secret(path, password + "\n")


def check(address: str, password: str) -> None:
    """Sign in to Gmail and out again, sending nothing. Raises MailError."""
    _session(address, password, None)


def message(sender: str, to: str, subject: str, body: str, html: str | None = None) -> EmailMessage:
    """A plain text email, with an HTML version for mail apps that show it."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.rpartition("@")[2])
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


def send(address: str, password: str, msg: EmailMessage) -> None:
    _session(address, password, msg)


def _session(address: str, password: str, msg: EmailMessage | None) -> None:
    try:
        with smtplib.SMTP_SSL(HOST, PORT, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(address, password)
            if msg is not None:
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"Gmail refused the app password for {address} ({e.smtp_code})") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"sending through {HOST}: {type(e).__name__}: {e}") from e
