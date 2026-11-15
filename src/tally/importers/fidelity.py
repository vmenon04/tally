"""Fidelity holdings: what each account holds on a day, as SimpleFIN gives them.

Only the last four characters of an account number are kept.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    quantity: Decimal
    price: Decimal
    cost: Decimal | None  # total cost basis; None when the brokerage doesn't say


@dataclass(frozen=True, slots=True)
class Account:
    label: str  # last four characters of the account number
    name: str
    cash: Decimal | None  # money market balance, if the account has one
    holdings: list[Holding]
    # Dividends paid lately, by day, when the source says (SimpleFIN does, the export doesn't).
    dividends: tuple[tuple[datetime.date, Decimal], ...] = ()
    # Contributions made lately, by day, likewise: money that came in from outside it.
    contributions: tuple[tuple[datetime.date, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    date: datetime.date
    accounts: list[Account]
