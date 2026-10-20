"""Bank of America checking account CSV export.

Layout (CRLF line endings, oldest first): a summary block, a blank line, then the
transactions, starting with a row that carries only the opening balance:

    Description,,Summary Amt.
    Beginning balance as of 03/01/2026,,"1,000.00"
    Total credits,,"250.00"
    Total debits,,"-4.50"
    Ending balance as of 03/31/2026,,"1,245.50"

    Date,Description,Amount,Running Bal.
    03/01/2026,Beginning balance as of 03/01/2026,,"1,000.00"
    03/02/2026,"COFFEE SHOP 03/01 PURCHASE SPRINGFIELD IL","-4.50","995.50"
    03/03/2026,"Zelle payment from ALEX SAMPLE Conf# QX7RT2LMP","250.00","1,245.50"

Amounts are already negative for money out. Every row's running balance and the summary
totals are checked, so a dropped or misread row fails the whole import.
"""

import csv
import datetime
import io
import re
from decimal import Decimal
from pathlib import Path

from tally.importers import ParseError, parse_amount, parse_date
from tally.transaction import Transaction

ACCOUNT = "bofa_checking"
SUMMARY_HEADER = ["Description", "", "Summary Amt."]
HEADER = ["Date", "Description", "Amount", "Running Bal."]

_SUMMARY = [
    ("beginning", re.compile(r"Beginning balance as of \d{2}/\d{2}/\d{4}")),
    ("credits", re.compile(r"Total credits")),
    ("debits", re.compile(r"Total debits")),
    ("ending", re.compile(r"Ending balance as of \d{2}/\d{2}/\d{4}")),
]
_OPENING = re.compile(r"Beginning balance as of \d{2}/\d{2}/\d{4}")

# Debit card purchases and ATM/branch withdrawals include the day they happened, which
# can be earlier than the posting date: "SHOP 03/07 PURCHASE ..." or "ATM 03/08 #0001 WITHDRWL".
_EVENT_DATE = re.compile(r" (\d{2})/(\d{2}) (?=PURCHASE\b|#\d+ WITHDRWL\b)")
# Per-row noise, removed to get a merchant name that stays the same across rows.
_NOISE = [
    (re.compile(r" \d{2}/\d{2} PURCHASE\b"), ""),
    (re.compile(r" \d{2}/\d{2} #\d+ WITHDRWL\b"), " WITHDRWL"),
    (re.compile(r" Conf# \S+$"), ""),
    (re.compile(r" FOR \d{2}/\d{2}/\d{2}$"), ""),
]

type _Row = tuple[int, list[str]]


def load(path: Path) -> list[Transaction]:
    return parse(path.read_text(encoding="utf-8"))


def parse(text: str) -> list[Transaction]:
    reader = csv.reader(io.StringIO(text.removeprefix("﻿"), newline=""))
    rows = [(reader.line_num, row) for row in reader]
    if not rows:
        raise ParseError("file is empty")

    line, row = rows[0]
    if row != SUMMARY_HEADER:
        raise ParseError(f"expected summary header {SUMMARY_HEADER}, got {row}", line)
    summary: dict[str, Decimal] = {}
    for i, (key, label) in enumerate(_SUMMARY, start=1):
        line, row = _row_at(rows, i)
        if len(row) != 3 or not label.fullmatch(row[0]) or row[1]:
            raise ParseError(f"expected summary row {label.pattern!r}, got {row}", line)
        summary[key] = parse_amount(row[2], line)

    i = _skip_blank(rows, len(_SUMMARY) + 1)
    line, row = _row_at(rows, i)
    if row != HEADER:
        raise ParseError(f"expected header {HEADER}, got {row}", line)

    line, row = _row_at(rows, i + 1)
    if len(row) != 4 or not _OPENING.fullmatch(row[1]) or row[2]:
        raise ParseError(f"expected the beginning balance row, got {row}", line)
    balance = parse_amount(row[3], line)
    if balance != summary["beginning"]:
        raise ParseError(
            f"beginning balance {balance} doesn't match summary {summary['beginning']}", line
        )

    transactions = []
    for line, row in rows[i + 2 :]:
        if not any(field.strip() for field in row):
            continue
        if len(row) != len(HEADER):
            raise ParseError(f"expected {len(HEADER)} fields, got {len(row)}", line)
        date, description, amount_text, balance_text = row
        posted = parse_date(date, line)
        amount = parse_amount(amount_text, line)
        expected = balance + amount
        balance = parse_amount(balance_text, line)
        if balance != expected:
            raise ParseError(
                f"running balance {balance} should be {expected} "
                f"(previous balance plus amount {amount})",
                line,
            )
        transactions.append(
            Transaction(
                account=ACCOUNT,
                date=event_date(description, posted),
                posted=posted,
                description=description,
                merchant=clean_merchant(description),
                amount=amount,
            )
        )

    _check_totals(transactions, balance, summary)
    return transactions


def event_date(description: str, posted: datetime.date) -> datetime.date:
    """The day a card purchase or withdrawal happened, or the posting date if not given."""
    match = _EVENT_DATE.search(description)
    if match:
        month, day = int(match[1]), int(match[2])
        # The description has no year: it's the latest such day not after posting.
        for year in (posted.year, posted.year - 1):
            try:
                date = datetime.date(year, month, day)
            except ValueError:
                continue
            if date <= posted:
                return date
    return posted


def clean_merchant(description: str) -> str:
    """Remove the parts of a description that change from row to row with the same payee."""
    description = " ".join(description.split())
    if " DES:" in description:
        # Bank transfers: "VENMO DES:PAYMENT ID:... INDN:... CO ID:... WEB".
        return description.split(" DES:", 1)[0]
    for pattern, replacement in _NOISE:
        description = pattern.sub(replacement, description)
    return description


def _check_totals(
    transactions: list[Transaction], ending: Decimal, summary: dict[str, Decimal]
) -> None:
    credits = sum((t.amount for t in transactions if t.amount > 0), Decimal(0))
    debits = sum((t.amount for t in transactions if t.amount < 0), Decimal(0))
    for key, label, actual in (
        ("credits", "total credits", credits),
        ("debits", "total debits", debits),
        ("ending", "ending balance", ending),
    ):
        if actual != summary[key]:
            raise ParseError(f"{label} is {actual} but the summary says {summary[key]}")


def _row_at(rows: list[_Row], i: int) -> _Row:
    if i >= len(rows):
        raise ParseError("file ends early", rows[-1][0])
    return rows[i]


def _skip_blank(rows: list[_Row], i: int) -> int:
    while i < len(rows) and not any(field.strip() for field in rows[i][1]):
        i += 1
    return i
