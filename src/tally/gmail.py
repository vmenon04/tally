"""Reading the alerts mailbox over IMAP, with an app password.

tally reads a Gmail account that holds nothing but the bank alerts forwarded from your main
Gmail, so its password can only ever expose those. The mailbox is opened read-only (EXAMINE)
and messages are fetched with BODY.PEEK, so nothing is ever marked, moved or deleted.

Gmail's IMAP extensions give each message the same ID the Gmail web app and API use
(X-GM-MSGID, shown here in hex), and accept Gmail search syntax (X-GM-RAW).
"""

import contextlib
import imaplib
import re
import ssl
from pathlib import Path
from typing import Any, Protocol, Self

from tally import config

HOST = "imap.gmail.com"
# Google shows app passwords as four groups of four letters.
_APP_PASSWORD = re.compile(r"[a-z]{16}")
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
_FETCH_IDS = re.compile(rb"X-GM-MSGID (\d+)|UID (\d+)")
_BATCH = 500


class GmailError(Exception):
    """Gmail refused or failed a request, or the app password is missing or wrong."""


class Mailbox(Protocol):
    def message_ids(self, query: str) -> list[str]:
        """The IDs of every message matching a Gmail search, newest first."""
        ...

    def raw(self, message_id: str) -> bytes:
        """The message exactly as received, headers and all."""
        ...


class ImapMailbox:
    """A Gmail account's All Mail folder, opened read-only."""

    def __init__(self, imap: Any) -> None:
        self._imap = imap
        self._uids: dict[str, bytes] = {}
        try:
            folder = _all_mail(_ok(imap.list(), "listing folders"))
            _ok(imap.select(folder, readonly=True), f"opening {folder.decode()}")
        except (imaplib.IMAP4.error, OSError) as e:
            raise GmailError(f"opening the mailbox: {e}") from e

    @classmethod
    def connect(cls, address: str, password: str) -> Self:
        try:
            imap = imaplib.IMAP4_SSL(HOST, ssl_context=ssl.create_default_context(), timeout=30)
        except OSError as e:
            raise GmailError(f"connecting to {HOST}: {e}") from e
        try:
            imap.login(address, password)
        except (imaplib.IMAP4.error, OSError) as e:
            imap.shutdown()
            raise GmailError(f"Gmail refused the app password for {address} ({e})") from e
        return cls(imap)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            self._imap.logout()

    def message_ids(self, query: str) -> list[str]:
        try:
            data = _ok(self._imap.uid("SEARCH", "X-GM-RAW", _quote(query)), "searching")
            uids = b" ".join(data).split()
            ids: list[str] = []
            for start in range(0, len(uids), _BATCH):
                batch = b",".join(uids[start : start + _BATCH])
                fetched = _ok(self._imap.uid("FETCH", batch, "(X-GM-MSGID)"), "listing IDs")
                for line in fetched:
                    message_id, uid = _ids(line)
                    self._uids[message_id] = uid
                    ids.append(message_id)
        except (imaplib.IMAP4.error, OSError) as e:
            raise GmailError(f"searching the mailbox: {e}") from e
        return ids[::-1]

    def raw(self, message_id: str) -> bytes:
        uid = self._uids.get(message_id)
        if uid is None:
            raise GmailError(f"message {message_id} wasn't in the last search")
        try:
            data = _ok(self._imap.uid("FETCH", uid, "(BODY.PEEK[])"), f"reading {message_id}")
        except (imaplib.IMAP4.error, OSError) as e:
            raise GmailError(f"reading {message_id}: {e}") from e
        parts = [item[1] for item in data if isinstance(item, tuple)]
        if len(parts) != 1:
            raise GmailError(f"reading {message_id}: unexpected reply")
        raw: bytes = parts[0]
        return raw


def app_password(text: str) -> str:
    """The app password as Google shows it, spaces optional. Refuses anything else."""
    password = "".join(text.split())
    if not _APP_PASSWORD.fullmatch(password):
        raise GmailError(
            "that isn't an app password (16 letters, shown as four groups of four). "
            "Create one at myaccount.google.com/apppasswords for the alerts account."
        )
    return password


def load_password(path: Path) -> str:
    try:
        return app_password(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GmailError(f"no app password at {path}: run `tally gmail-login`") from None


def save_password(path: Path, password: str) -> None:
    config.write_secret(path, password + "\n")


def _ok(reply: tuple[str, list[Any]], doing: str) -> list[Any]:
    status, data = reply
    if status != "OK":
        raise GmailError(f"{doing}: {status} {data!r}")
    return [d for d in data if d is not None]


def _all_mail(lines: list[bytes]) -> bytes:
    """The folder flagged \\All ("[Gmail]/All Mail", or its translation)."""
    for line in lines:
        match = _LIST_LINE.match(line)
        if match and b"\\All" in match["flags"].split():
            return match["name"]
    raise GmailError("the mailbox has no All Mail folder; is IMAP showing it?")


def _ids(line: bytes) -> tuple[str, bytes]:
    message_id = uid = None
    for gm_id, imap_uid in _FETCH_IDS.findall(line):
        message_id = message_id or gm_id
        uid = uid or imap_uid
    if not message_id or not uid:
        raise GmailError(f"unexpected reply listing IDs: {line!r}")
    return f"{int(message_id):x}", uid


def _quote(text: str) -> str:
    if '"' in text or "\\" in text:
        raise ValueError(f"can't quote {text!r}")
    return f'"{text}"'
