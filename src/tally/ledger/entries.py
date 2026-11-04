"""Turning tally Transactions into Beancount entries, and recognizing ones already written."""

import datetime
import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal

from beancount.core import amount, data

from tally.ledger.accounts import Accounts
from tally.rules import Rules
from tally.transaction import Transaction

CURRENCY = "USD"
ID_KEY = "tally-id"
PURCHASED_KEY = "purchased"
DUPLICATE_KEY = "__duplicate__"  # what beangulp marks a duplicate with

_LONG_NUMBER = re.compile(r"\d{5,}")


def identity(t: Transaction) -> str:
    """A short hash of the fields that make a row itself, as tally.db deduplicates them."""
    posted = t.posted.isoformat() if t.posted else ""
    key = "\x1f".join((t.account, t.date.isoformat(), posted, t.description, f"{t.amount:.2f}"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def mask(text: str) -> str:
    """Cut every run of 5 or more digits to its last 4: "ID:1000000000001" -> "ID:…0001"."""
    return _LONG_NUMBER.sub(lambda m: "…" + m[0][-4:], " ".join(text.split()))


def transaction(
    t: Transaction,
    accounts: Accounts,
    rules: Rules,
    *,
    flag: str = "*",
    meta: data.Meta | None = None,
) -> data.Transaction:
    """One row as a Beancount transaction: its account, and the other side by category."""
    date = t.posted or t.date
    entry_meta: data.Meta = dict(meta or {})
    entry_meta[ID_KEY] = identity(t)
    if t.date != date:
        entry_meta[PURCHASED_KEY] = t.date
    units = amount.Amount(t.amount, CURRENCY)
    counter = accounts.counter(rules.categorize(t), t.merchant)
    return data.Transaction(
        meta=entry_meta,
        date=date,
        flag=flag,
        payee=mask(t.merchant),
        narration=mask(t.description),
        tags=frozenset(),
        links=frozenset(),
        postings=[
            data.Posting(accounts.source(t.account), units, None, None, None, None),
            data.Posting(counter, -units, None, None, None, None),
        ],
    )


def balance(meta: data.Meta, date: datetime.date, account: str, value: Decimal) -> data.Balance:
    """The account's balance at the start of date, before any of that day's entries."""
    return data.Balance(meta, date, account, amount.Amount(value, CURRENCY), None, None)


def mark_duplicates(entries: Sequence[data.Directive], existing: Iterable[data.Directive]) -> None:
    """Mark the new entries the ledger already has, the way tally.db deduplicates rows.

    Identical rows are real (two equal coffees on one day), so of the new entries sharing a
    tally-id, only as many are marked as the ledger already holds. A balance is a duplicate
    when the ledger already asserts the same amount for that account on that day, and a
    price when the ledger already has it.
    """
    have: Counter[str] = Counter()
    balances: set[tuple[datetime.date, str, Decimal | None]] = set()
    prices: set[tuple[datetime.date, str, Decimal | None]] = set()
    for entry in existing:
        if isinstance(entry, data.Transaction) and ID_KEY in entry.meta:
            have[entry.meta[ID_KEY]] += 1
        elif isinstance(entry, data.Balance):
            balances.add(
                (entry.date, f"{entry.account} {entry.amount.currency}", entry.amount.number)
            )
        elif isinstance(entry, data.Price):
            prices.add((entry.date, entry.currency, entry.amount.number))
    for entry in entries:
        if isinstance(entry, data.Transaction) and ID_KEY in entry.meta:
            key = entry.meta[ID_KEY]
            if have[key] > 0:
                have[key] -= 1
                entry.meta[DUPLICATE_KEY] = True
        elif isinstance(entry, data.Balance):
            key = (entry.date, f"{entry.account} {entry.amount.currency}", entry.amount.number)
            if key in balances:
                entry.meta[DUPLICATE_KEY] = True
        elif isinstance(entry, data.Price):
            if (entry.date, entry.currency, entry.amount.number) in prices:
                entry.meta[DUPLICATE_KEY] = True
