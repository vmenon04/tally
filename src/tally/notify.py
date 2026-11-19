"""Push notifications through ntfy, headlines only.

ntfy topics have no password: anyone who knows a topic's name can read it, and the server
sees every message. So the topic is a long random name kept in secrets/, and messages never
carry merchants or amounts, only what kind of thing happened and how many.

On a self-hosted ntfy server, an account guards the topic instead: TALLY_NTFY_SERVER,
TALLY_NTFY_TOPIC and TALLY_NTFY_TOKEN (an access token that may publish to the topic) override
config.toml's ntfy_server and secrets/ntfy-topic. A topic shorter than a random one is only
accepted with a token, so a guessable name never goes to a public server unguarded.

Each `tally sync` (every two hours on the server) announces what's new since the last: new
pending purchases, a bank that needs signing in to again at SimpleFIN, an account whose
balance differs from the bank's, and a week with nothing new. Each event has a key and is
announced once (SimpleFIN failing again after REPEAT_FAILURE if it still is). An event is
only recorded as announced once ntfy has accepted it, so one that fails to send is tried on
the next run.

SimpleFIN failing is only announced once it has failed for BANK_GRACE in a row: one run
timing out now and then is ordinary network noise, and the next run catches up.
"""

import datetime
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import requests

from tally import config, store

REPEAT_FAILURE = datetime.timedelta(hours=6)
# Nothing new for this long means something upstream stopped: SimpleFIN, or a bank in it.
STALE_AFTER = datetime.timedelta(days=7)
BANK_GRACE = datetime.timedelta(hours=3)  # longer than one sync to the next
BANK_FAILURE = "bank-failure"
# When the current run of SimpleFIN failures began, kept alongside the sent notifications.
FAILING_SINCE = "bank-failing-since"

_TOPIC = re.compile(r"[A-Za-z0-9_-]{20,64}")
# With a token the topic needn't be secret, so any name ntfy accepts.
_GUARDED_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
# ntfy's access tokens: tk_ and 29 lowercase letters or digits.
_TOKEN = re.compile(r"tk_[a-z0-9]{29}")


@dataclass(frozen=True, slots=True)
class _Kind:
    one: str
    many: str  # with {n} for the count
    priority: str  # ntfy's: min, low, default, high, urgent


# In the order they're sent.
KINDS = {
    "bank": _Kind(
        "can't read your accounts through SimpleFIN",
        "can't read your accounts through SimpleFIN",
        "high",
    ),
    "signin": _Kind(
        "a bank needs you to sign in again at SimpleFIN",
        "{n} banks need you to sign in again at SimpleFIN",
        "high",
    ),
    "balance": _Kind(
        "1 account's balance differs from the bank's, check tally",
        "{n} accounts' balances differ from the bank's, check tally",
        "default",
    ),
    "stale": _Kind(
        "no new transactions for a week, check SimpleFIN",
        "no new transactions for a week, check SimpleFIN",
        "default",
    ),
    "new": _Kind("1 new pending purchase", "{n} new pending purchases", "low"),
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
    def __init__(self, server: str, topic: str, token: str | None = None) -> None:
        self._url = f"{server}/{topic}"
        self._token = token

    def send(self, message: str, priority: str) -> None:
        headers = {"Title": "tally", "Priority": priority}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = requests.post(
                self._url,
                data=message.encode("utf-8"),
                headers=headers,
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


def sender(server: str, topic_file: Path, env: Mapping[str, str]) -> NtfySender:
    """The sender for config.toml's server and the saved topic, or what env overrides.

    Never puts the topic or the token in an error.
    """
    server = env.get("TALLY_NTFY_SERVER") or server
    if not server.startswith("https://"):
        raise NotifyError("TALLY_NTFY_SERVER must be an https:// address")
    token = env.get("TALLY_NTFY_TOKEN") or None
    if token is not None and not _TOKEN.fullmatch(token):
        raise NotifyError("TALLY_NTFY_TOKEN isn't an ntfy access token (tk_ and 29 more)")
    topic = env.get("TALLY_NTFY_TOPIC") or None
    if topic is None:
        topic = load_topic(topic_file)
    elif not (_TOPIC if token is None else _GUARDED_TOPIC).fullmatch(topic):
        raise NotifyError(
            "TALLY_NTFY_TOPIC isn't usable: without TALLY_NTFY_TOKEN it must be a random"
            " name of 20 to 64 letters, digits, - or _"
        )
    return NtfySender(server.rstrip("/"), topic, token)


def bank_failed(conn: sqlite3.Connection, now: datetime.datetime) -> list[Event]:
    """Note that SimpleFIN couldn't be asked. The failure event, once it has lasted BANK_GRACE."""
    since = store.notifications(conn).get(FAILING_SINCE)
    if since is None:
        store.record_notifications(conn, [FAILING_SINCE], now)
        since = now
    if now - since < BANK_GRACE:
        return []
    return [Event("bank", BANK_FAILURE)]


def stale(conn: sqlite3.Connection, now: datetime.datetime) -> list[Event]:
    """The stale event if no new transaction has come in for STALE_AFTER.

    Its key is when the last one came in, so it's announced once per quiet spell.
    """
    last = store.last_new_transaction(conn)
    if last is None or now - last < STALE_AFTER:
        return []
    return [Event("stale", f"stale:{last.isoformat()}")]


def bank_read(conn: sqlite3.Connection) -> None:
    """SimpleFIN answered: a later failure starts a new grace period and is announced afresh."""
    store.forget_notification(conn, FAILING_SINCE)
    store.forget_notification(conn, BANK_FAILURE)


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
        return e.kind == "bank" and now - last >= REPEAT_FAILURE

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
