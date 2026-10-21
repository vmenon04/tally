"""Push notifications through ntfy, headlines only.

ntfy topics have no password: anyone who knows a topic's name can read it, and the server
sees every message. So the topic is a long random name kept in secrets/, and messages never
carry merchants or amounts, only what kind of thing happened and how many.

tally runs every few minutes, so each event has a key, and an event is announced once (a
mailbox failure again after REPEAT_FAILURE if it's still failing). An event is only recorded
as announced once ntfy has accepted it, so one that fails to send is tried on the next run.

A mailbox failure is only announced once the mailbox has failed for MAILBOX_GRACE in a row:
a single run timing out now and then is ordinary network noise, and the next run catches up.
"""

import datetime
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import requests

from tally import config, store

REPEAT_FAILURE = datetime.timedelta(hours=6)
MAILBOX_GRACE = datetime.timedelta(minutes=15)
MAILBOX_FAILURE = "mailbox-failure"
# When the current run of mailbox failures began, kept alongside the sent notifications.
FAILING_SINCE = "mailbox-failing-since"

_TOPIC = re.compile(r"[A-Za-z0-9_-]{20,64}")


@dataclass(frozen=True, slots=True)
class _Kind:
    one: str
    many: str  # with {n} for the count
    priority: str  # ntfy's: min, low, default, high, urgent


# In the order they're sent.
KINDS = {
    "refused": _Kind(
        "1 alert email refused, check tally", "{n} alert emails refused, check tally", "high"
    ),
    "mailbox": _Kind("can't read the alerts mailbox", "can't read the alerts mailbox", "high"),
    "not_found": _Kind(
        "1 purchase missing from the bank export",
        "{n} purchases missing from the bank export",
        "default",
    ),
    "new": _Kind("1 new card alert", "{n} new card alerts", "low"),
}


class NotifyError(Exception):
    """ntfy refused or couldn't be reached, or the topic is missing."""


@dataclass(frozen=True, slots=True)
class Event:
    kind: str  # a key of KINDS
    key: str  # what makes it the same event on the next run


class Sender(Protocol):
    def send(self, message: str, priority: str) -> None: ...


class NtfySender:
    def __init__(self, server: str, topic: str) -> None:
        self._url = f"{server}/{topic}"

    def send(self, message: str, priority: str) -> None:
        try:
            response = requests.post(
                self._url,
                data=message.encode("utf-8"),
                headers={"Title": "tally", "Priority": priority},
                timeout=15,
            )
        except requests.RequestException as e:
            raise NotifyError(f"couldn't reach ntfy: {type(e).__name__}") from e
        if not response.ok:
            raise NotifyError(f"ntfy refused the message: HTTP {response.status_code}")


def new_topic() -> str:
    """A topic name nobody can guess: 128 random bits."""
    return "tally-" + secrets.token_urlsafe(16)


def load_topic(path: Path) -> str:
    try:
        topic = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise NotifyError(f"no ntfy topic at {path}: run `tally ntfy-setup`") from None
    if not _TOPIC.fullmatch(topic):
        raise NotifyError(f"{path} doesn't hold a usable topic: run `tally ntfy-setup`")
    return topic


def save_topic(path: Path, topic: str) -> None:
    config.write_secret(path, topic + "\n")


def mailbox_failed(conn: sqlite3.Connection, now: datetime.datetime) -> list[Event]:
    """Note a failed mailbox read. The failure event, once it has lasted MAILBOX_GRACE."""
    since = store.notifications(conn).get(FAILING_SINCE)
    if since is None:
        store.record_notifications(conn, [FAILING_SINCE], now)
        since = now
    if now - since < MAILBOX_GRACE:
        return []
    return [Event("mailbox", MAILBOX_FAILURE)]


def mailbox_read(conn: sqlite3.Connection) -> None:
    """The mailbox was read: a later failure starts a new grace period and is announced afresh."""
    store.forget_notification(conn, FAILING_SINCE)
    store.forget_notification(conn, MAILBOX_FAILURE)


def send_new(
    conn: sqlite3.Connection,
    sender: Sender,
    events: list[Event],
    now: datetime.datetime,
    err: TextIO,
) -> list[str]:
    """Announce the events not announced before, one message per kind. Returns the messages.

    A message that fails to send prints a warning and is left for the next run.
    """
    sent = store.notifications(conn)

    def due(e: Event) -> bool:
        last = sent.get(e.key)
        if last is None:
            return True
        return e.kind == "mailbox" and now - last >= REPEAT_FAILURE

    fresh = [e for e in events if due(e)]
    messages = []
    for kind, words in KINDS.items():
        keys = sorted({e.key for e in fresh if e.kind == kind})
        if not keys:
            continue
        message = words.one if len(keys) == 1 else words.many.format(n=len(keys))
        try:
            sender.send(message, words.priority)
        except NotifyError as e:
            print(f"notify: {e}; will try again on the next run", file=err)
            continue
        store.record_notifications(conn, keys, now)
        messages.append(message)
    return messages
