"""SimpleFIN Bridge: read-only access to bank and card accounts (simplefin.org/protocol.html).

You connect your banks at bridge.simplefin.org and make a setup token there, which `tally
simplefin-login` claims once for an access URL: a URL with a username and password in it,
kept in secrets/simplefin-access-url. A claimed token can't be claimed again, so if claiming
fails because it already was, someone else may have it, and you should disable it at the
bridge. The access URL is never printed, logged or put in an error message.

Each request asks for the accounts, their balances, and their transactions since a date,
pending ones included. Amounts are positive for money in, as in a Transaction.

An account comes into tally when the ledger links it (`simplefin-account:` with its last
four digits, on an account with a `tally-account:`): its posted rows become Transactions as
that bank's export would give them (see rows), and are stored and added to the ledger by
`tally sync` in place of CSV imports. An account with `interest-from:` and no tally-account
brings in only its interest (see interest).
"""

import base64
import binascii
import datetime
import functools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from tally.alerts import BANK_PENDING, Alert
from tally.importers import bofa, discover, fidelity
from tally.transaction import Transaction as Row

ACCESS_FILE = "simplefin-access-url"
TIMEOUT = 180  # seconds; the bridge asks each bank while we wait, which can take over a minute

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
    # What else the bridge sent for the account (holdings, say), as it sent it.
    unread: dict[str, Any] = field(default_factory=dict)


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


_READ = {
    "id",
    "name",
    "conn_id",
    "conn_name",
    "currency",
    "balance",
    "balance-date",
    "transactions",
}


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
            unread={k: v for k, v in a.items() if k not in _READ},
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
    account = account_ending(answer, last4)
    if account is None:
        return None
    ordered = sorted(
        account.transactions,
        key=lambda t: t.posted or t.transacted or datetime.date.max,
        reverse=True,
    )
    return [
        f"{'pending' if t.pending else t.posted}  bought {t.transacted or '?'}  "
        f"{t.amount:>9}  {t.description}"
        for t in ordered
    ]


def account_ending(answer: Answer, last4: str) -> Account | None:
    """The one account whose name ends in last4 (the bridge writes it as "Name (1234)")."""
    found = [a for a in answer.accounts if a.name.rstrip(") ").endswith(last4)]
    return found[0] if len(found) == 1 else None


def rows(answer: Answer, linked: dict[str, str]) -> dict[str, list[Row]]:
    """Each linked tally account's posted rows, oldest first. Raises SimpleFINError if the
    bridge has no account for one. Banks without a reader of their own get _plain."""
    result = {}
    for last4, tally_account in sorted(linked.items()):
        account = account_ending(answer, last4)
        if account is None:
            raise SimpleFINError(f"the bridge has no one account ending in {last4}")
        make = _ROWS.get(tally_account, functools.partial(_plain, tally_account))
        posted = [t for t in account.transactions if not t.pending and t.posted is not None]
        result[tally_account] = sorted(
            (make(t) for t in posted), key=lambda r: (r.posted or r.date, r.description)
        )
    return result


def interest(answer: Answer, last4: str) -> list[Transaction]:
    """The interest the account ending last4 was paid: its posted money in described with
    "interest", oldest first. Empty if the bridge has no one account ending last4."""
    account = account_ending(answer, last4)
    if account is None:
        return []
    paid = [
        t
        for t in account.transactions
        if not t.pending
        and t.posted is not None
        and t.amount > 0
        and "interest" in t.description.casefold()
    ]
    return sorted(paid, key=lambda t: (t.posted, t.id))


_LAST4 = re.compile(r"\((\w{4})\)\s*$")
_CENT = Decimal("0.01")
_PRICE = Decimal("0.0001")


def snapshot(answer: Answer, today: datetime.date) -> fidelity.Snapshot:
    """What each brokerage account (one the bridge sends holdings for) holds now, in the
    shape the positions export gives them. Raises SimpleFINError.

    A holding worth exactly its shares, bought for nothing, is the money market: the
    account's cash. purchase_price is the average price paid per share, so a holding's cost
    is that times its shares; the bridge's cost_basis is always 0. A holding bought for
    nothing otherwise (a 401(k)'s fund) has no cost. Its price is its value over its shares.

    Dividends are the rows described "dividend received", and contributions those described
    "contribution" (a 401(k)'s payroll ones, an IRA's), money in both.
    """
    accounts = []
    for a in answer.accounts:
        found = a.unread.get("holdings")
        if not isinstance(found, list):
            continue
        label = _LAST4.search(a.name)
        if label is None:
            raise SimpleFINError(f"the brokerage account {a.name!r} has no last four in its name")
        cash: Decimal | None = None
        holdings = []
        for h in found:
            symbol = str(h.get("symbol") or "").strip()
            shares = _amount(h.get("shares"), "shares", symbol)
            value = _amount(h.get("market_value"), "market_value", symbol)
            paid = _amount(h.get("purchase_price"), "purchase_price", symbol)
            if not paid and value == shares:
                cash = (cash or Decimal(0)) + value
                continue
            if not symbol:
                raise SimpleFINError(f"a holding in {a.name!r} has no symbol")
            if not shares:
                continue
            cost = (paid * shares).quantize(_CENT) if paid else None
            holdings.append(
                fidelity.Holding(symbol, shares, (value / shares).quantize(_PRICE), cost)
            )
        paid_in = [t for t in a.transactions if not t.pending and t.posted and t.amount > 0]
        accounts.append(
            fidelity.Account(
                label[1],
                _LAST4.sub("", a.name).strip(),
                cash,
                holdings,
                _described(paid_in, "dividend received"),
                _described(paid_in, "contribution"),
            )
        )
    return fidelity.Snapshot(today, accounts)


def _described(rows: list[Transaction], text: str) -> tuple[tuple[datetime.date, Decimal], ...]:
    return tuple(
        sorted((t.posted, t.amount) for t in rows if t.posted and text in t.description.casefold())
    )


def _amount(value: Any, name: str, symbol: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise SimpleFINError(f"bad {name} {value!r} for holding {symbol!r}") from None


def pending(answer: Answer, linked: dict[str, str], now: datetime.datetime) -> list[Alert]:
    """The linked accounts' pending transactions, as alerts listed now (see BANK_PENDING).
    Accounts the bridge lacks are left out; rows() reports them."""
    result = []
    for last4, tally_account in sorted(linked.items()):
        account = account_ending(answer, last4)
        if account is None:
            continue
        for t in account.transactions:
            if t.pending:
                result.append(
                    Alert(
                        account=tally_account,
                        date=t.transacted or now.date(),
                        merchant=" ".join(t.description.split()),
                        amount=t.amount,
                        message_id=f"{BANK_PENDING}{account.id}:{t.id}",
                        received=now,
                    )
                )
    return result


def _bofa(t: Transaction) -> Row:
    # The bridge pads the description with spaces; the CSV has them collapsed.
    description = " ".join(t.description.split())
    posted = t.posted or t.transacted or datetime.date.min
    return Row(
        account=bofa.ACCOUNT,
        date=bofa.event_date(description, posted),
        posted=posted,
        description=description,
        merchant=bofa.clean_merchant(description),
        amount=t.amount,
    )


def _discover(t: Transaction) -> Row:
    return Row(
        account=discover.ACCOUNT,
        date=t.transacted or t.posted or datetime.date.min,
        posted=t.posted,
        description=t.description,
        merchant=discover.clean_merchant(t.description),
        amount=t.amount,
    )


def _plain(tally_account: str, t: Transaction) -> Row:
    """Any other bank's row: its description, spaces collapsed, is the merchant."""
    description = " ".join(t.description.split())
    return Row(
        account=tally_account,
        date=t.transacted or t.posted or datetime.date.min,
        posted=t.posted,
        description=description,
        merchant=description,
        amount=t.amount,
    )


# Banks whose rows need reading their own way, by tally-account.
_ROWS: dict[str, Callable[[Transaction], Row]] = {bofa.ACCOUNT: _bofa, discover.ACCOUNT: _discover}
