"""The one shape every importer produces."""

import datetime
import hashlib
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Transaction:
    """One row from a bank or card export.

    amount is negative for money leaving the account (purchases, transfers out) and
    positive for money coming in (refunds, paychecks, a card payment received by the card).
    """

    account: str
    date: datetime.date
    posted: datetime.date | None
    description: str  # exactly as the bank wrote it
    merchant: str  # description with per-row noise removed
    amount: Decimal
    bank_category: str | None = None
    balance: Decimal | None = None  # account balance after this row, if the bank gives it


def identity(t: Transaction) -> str:
    """A short hash of the fields that make a row itself, as tally.db deduplicates them.

    The ledger stores it as each entry's tally-id.
    """
    posted = t.posted.isoformat() if t.posted else ""
    key = "\x1f".join((t.account, t.date.isoformat(), posted, t.description, f"{t.amount:.2f}"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]
