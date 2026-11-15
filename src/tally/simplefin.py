"""SimpleFIN Bridge: read-only access to bank and card accounts (simplefin.org/protocol.html).

You connect your banks at bridge.simplefin.org and make a setup token there, which `tally
simplefin-login` claims once for an access URL: a URL with a username and password in it,
kept in secrets/simplefin-access-url. A claimed token can't be claimed again, so if claiming
fails because it already was, someone else may have it, and you should disable it at the
bridge. The access URL is never printed, logged or put in an error message.

Each request asks for the accounts, their balances, and their transactions since a date,
pending ones included. Amounts are positive for money in.
"""

import base64
import binascii
import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

ACCESS_FILE = "simplefin-access-url"
TIMEOUT = 60  # seconds; the bridge asks each bank as it answers

# (url, params, auth) -> the response; a seam for tests.
Get = Callable[[str, dict[str, str], tuple[str, str]], requests.Response]
Post = Callable[[str], requests.Response]


class SimpleFINError(RuntimeError):
    """The bridge refused a request, or couldn't be reached."""


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    posted: datetime.date | None  # None while pending
    transacted: datetime.date | None  # the purchase, when the bank says
    amount: Decimal  # money in is positive
    description: str
    pending: bool


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    name: str
    bank: str  # the connection's name, like "Discover Card"
    currency: str
    balance: Decimal
    balance_date: datetime.date
    transactions: list[Transaction] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Answer:
    accounts: list[Account]
    problems: list[str]  # what the bridge said went wrong, per connection or account


def claim(token: str, post: Post | None = None) -> str:
    """The access URL a setup token gives. Raises SimpleFINError."""
    try:
        url = base64.b64decode(token.strip(), validate=True).decode("ascii").strip()
    except (binascii.Error, UnicodeDecodeError):
        raise SimpleFINError("that isn't a SimpleFIN setup token") from None
    if not url.startswith("https://"):
        raise SimpleFINError("that setup token doesn't hold an https:// claim URL")
    post = post or (lambda u: requests.post(u, timeout=TIMEOUT))
    try:
        response = post(url)
    except requests.RequestException as e:
        raise SimpleFINError(f"couldn't reach the bridge: {type(e).__name__}") from None
    if response.status_code == 403:
        raise SimpleFINError(
            "the bridge says this token doesn't exist or was claimed already. If you didn't "
            "claim it, someone else may have: disable it at bridge.simplefin.org"
        )
    access = response.text.strip()
    if response.status_code != 200 or not access.startswith("https://"):
        raise SimpleFINError(f"the bridge didn't give an access URL (HTTP {response.status_code})")
    return access


def fetch(
    access_url: str,
    since: datetime.date | None = None,
    *,
    balances_only: bool = False,
    get: Get | None = None,
) -> Answer:
    """The accounts, with their transactions since `since`. Raises SimpleFINError."""
    parts = urlsplit(access_url)
    if parts.scheme != "https" or not parts.username or parts.password is None:
        raise SimpleFINError("the saved access URL isn't one; run `tally simplefin-login`")
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    bare = urlunsplit(("https", host, parts.path, "", ""))
    params = {"version": "2", "pending": "1"}
    if since is not None:
        start = datetime.datetime.combine(since, datetime.time(), datetime.UTC)
        params["start-date"] = str(int(start.timestamp()))
    if balances_only:
        params["balances-only"] = "1"
    get = get or (lambda u, p, a: requests.get(u, params=p, auth=a, timeout=TIMEOUT))
    try:
        response = get(f"{bare.rstrip('/')}/accounts", params, (parts.username, parts.password))
    except requests.RequestException as e:
        raise SimpleFINError(f"couldn't reach the bridge: {type(e).__name__}") from None
    if response.status_code == 402:
        raise SimpleFINError("the bridge wants payment: renew at bridge.simplefin.org")
    if response.status_code == 403:
        raise SimpleFINError(
            "the bridge refused the saved access: it was disabled, or run `tally simplefin-login`"
        )
    if response.status_code != 200:
        raise SimpleFINError(f"the bridge answered HTTP {response.status_code}")
    try:
        return _answer(response.json())
    except (ValueError, KeyError, TypeError, InvalidOperation) as e:
        raise SimpleFINError(f"unexpected answer from the bridge: {e}") from None


def _answer(body: dict[str, Any]) -> Answer:
    banks = {str(c["conn_id"]): str(c["name"]) for c in body.get("connections", [])}
    accounts = [
        Account(
            id=str(a["id"]),
            name=str(a["name"]),
            bank=banks.get(str(a.get("conn_id")), str(a.get("conn_name", ""))),
            currency=str(a["currency"]),
            balance=Decimal(str(a["balance"])),
            balance_date=_day(a["balance-date"]) or datetime.date.min,
            transactions=[_transaction(t) for t in a.get("transactions", [])],
        )
        for a in body["accounts"]
    ]
    problems = [str(e.get("msg", e)) for e in body.get("errlist", [])]
    return Answer(accounts, problems)


def _transaction(t: dict[str, Any]) -> Transaction:
    pending = bool(t.get("pending", False))
    return Transaction(
        id=str(t["id"]),
        posted=None if pending else _day(t["posted"]),
        transacted=_day(t.get("transacted_at")),
        amount=Decimal(str(t["amount"])),
        description=str(t["description"]).strip(),
        pending=pending,
    )


def _day(epoch: Any) -> datetime.date | None:
    """The day of a Unix time, in UTC as the bridge gives it; None for 0 or none."""
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(int(epoch), datetime.UTC).date()


def overview(answer: Answer, today: datetime.date) -> list[str]:
    """What the bridge sees, one line per account, then any problems it reported."""
    lines = []
    for a in sorted(answer.accounts, key=lambda a: (a.bank, a.name)):
        pending = sum(t.pending for t in a.transactions)
        days = [d for t in a.transactions if (d := t.posted or t.transacted) is not None]
        span = f", {min(days)} to {max(days)}" if days else ""
        age = (today - a.balance_date).days
        lines.append(
            f"{a.bank} / {a.name}: balance {a.balance:,.2f} {a.currency} "
            f"({'today' if age <= 0 else f'{age} day(s) old'}); "
            f"{len(a.transactions)} transaction(s), {pending} pending{span}"
        )
    lines += [f"The bridge says: {p}" for p in answer.problems]
    return lines or ["The bridge has no accounts yet: connect your banks at bridge.simplefin.org."]


def transactions(answer: Answer, last4: str) -> list[str] | None:
    """Every transaction of the account whose name ends in last4, newest first, as the
    bridge gives it; None if no account does."""
    found = [a for a in answer.accounts if a.name.rstrip(")").endswith(last4)]
    if len(found) != 1:
        return None
    rows = sorted(
        found[0].transactions,
        key=lambda t: t.posted or t.transacted or datetime.date.max,
        reverse=True,
    )
    return [
        f"{'pending' if t.pending else t.posted}  bought {t.transacted or '?'}  "
        f"{t.amount:>9}  {t.description}"
        for t in rows
    ]
