"""Fidelity "Positions" CSV export (Accounts → Positions → download, all accounts).

    Account number,Account name,Symbol,Description,Quantity,Last price,Last price change,
    Current value,...,Cost basis total,Average cost basis,Type
    Z00001234,ROTH IRA,SPAXX**,HELD IN MONEY MARKET,,,,$9.75,...
    Z00001234,ROTH IRA,FXAIX,FIDELITY 500 INDEX FUND,20.252,$263.13,...,$4854.18,$239.69,Cash
    ...
    (blank line, then disclaimers, then)
    "Date downloaded Nov-04-2026 4:19 p.m ET"

It's a snapshot: what each account holds on the day it was downloaded, with each holding's
total cost basis. A money market row ("**" after the symbol, no quantity) is the account's
cash. Only the last four characters of an account number are kept.
"""

import calendar
import csv
import datetime
import io
import re
from dataclasses import dataclass
from decimal import Decimal

from tally.importers import ParseError

HEADER_START = ["Account number", "Account name", "Symbol", "Description", "Quantity"]
_MONTHS = {name: i for i, name in enumerate(calendar.month_abbr) if name}
_DOWNLOADED = re.compile(r"Date downloaded (\w{3})-(\d{2})-(\d{4})")
_MONEY = re.compile(r"(-?)\$((\d{1,3}(,\d{3})+|\d+)(\.\d+)?)")
_QUANTITY = re.compile(r"-?\d+(\.\d+)?")
# Beancount commodity names: capitals, digits and a few marks, starting with a letter.
_SYMBOL = re.compile(r"[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]")


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    quantity: Decimal
    price: Decimal
    cost: Decimal  # total cost basis


@dataclass(frozen=True, slots=True)
class Account:
    label: str  # last four characters of the account number
    name: str
    cash: Decimal | None  # money market balance, if the account has one
    holdings: list[Holding]


@dataclass(frozen=True, slots=True)
class Snapshot:
    date: datetime.date
    accounts: list[Account]


def is_positions(first_row: list[str]) -> bool:
    return first_row[: len(HEADER_START)] == HEADER_START


def parse(text: str) -> Snapshot:
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"), newline="")))
    if not rows or not is_positions(rows[0]):
        raise ParseError(f"expected a Fidelity positions header, got {rows[0] if rows else []}")
    header = rows[0]
    column = {name: i for i, name in enumerate(header)}
    for needed in ("Last price", "Current value", "Cost basis total"):
        if needed not in column:
            raise ParseError(f"no {needed!r} column", line=1)

    date = None
    accounts: dict[str, Account] = {}
    for line, row in enumerate(rows[1:], start=2):
        joined = " ".join(row).strip()
        found = _DOWNLOADED.search(joined)
        if found:
            date = _date(found, line)
            continue
        if len(row) < len(header) or not row[column["Symbol"]].strip():
            continue  # blank lines and the disclaimers under the table
        label = row[0].strip()[-4:]
        if len(label) < 4:
            raise ParseError(f"account number {label!r} is too short", line)
        account = accounts.setdefault(label, Account(label, row[1].strip(), None, []))
        symbol = row[column["Symbol"]].strip()
        quantity = row[column["Quantity"]].strip()
        if symbol.endswith("**") and not quantity:
            if account.cash is not None:
                raise ParseError(f"account …{label} has two money market rows", line)
            cash = _money(row[column["Current value"]], line)
            accounts[label] = Account(label, account.name, cash, account.holdings)
            continue
        if not _SYMBOL.fullmatch(symbol):
            raise ParseError(f"symbol {symbol!r} can't be a Beancount commodity", line)
        if not _QUANTITY.fullmatch(quantity):
            raise ParseError(f"bad quantity {quantity!r}", line)
        account.holdings.append(
            Holding(
                symbol,
                Decimal(quantity),
                _money(row[column["Last price"]], line),
                _money(row[column["Cost basis total"]], line),
            )
        )
    if date is None:
        raise ParseError("no 'Date downloaded' line at the end")
    if not accounts:
        raise ParseError("no positions")
    return Snapshot(date, list(accounts.values()))


def _money(text: str, line: int) -> Decimal:
    match = _MONEY.fullmatch(text.strip())
    if match is None:
        raise ParseError(f"bad amount {text!r}", line)
    return Decimal(match[1] + match[2].replace(",", ""))


def _date(found: re.Match[str], line: int) -> datetime.date:
    month = _MONTHS.get(found[1])
    try:
        if month is None:
            raise ValueError
        return datetime.date(int(found[3]), month, int(found[2]))
    except ValueError:
        raise ParseError(f"bad download date {found[0]!r}", line) from None
