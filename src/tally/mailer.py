"""Sending email from the alerts mailbox, through Gmail's SMTP server.

It signs in with the same app password tally reads the mailbox with, so sending needs no new
account or secret. The weekly summary goes from there to the address in config.toml.

Nothing sent this way is read back as an alert: tally only reads mail from the banks' domains.
"""

import email.utils
import smtplib
import ssl
from email.message import EmailMessage

HOST = "smtp.gmail.com"
PORT = 465


class MailError(Exception):
    """Gmail refused or couldn't be reached. The message never includes the password."""


def message(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.rpartition("@")[2])
    msg.set_content(body)
    return msg


def send(address: str, password: str, msg: EmailMessage) -> None:
    try:
        with smtplib.SMTP_SSL(HOST, PORT, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"Gmail refused the app password for {address} ({e.smtp_code})") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"sending through {HOST}: {type(e).__name__}: {e}") from e
