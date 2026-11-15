"""Made-up sample data for the tests, and putting it in a data folder the way sync does.

The samples are kept as bank-export CSVs in fixtures/, since that's an easy shape to read and
change. tally itself no longer reads CSVs (SimpleFIN brings its rows in), so the small
readers here are for the tests alone.
"""

import calendar
import csv
import datetime
import hashlib
import io
import re
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from tally import rules, store
from tally.importers import bofa, discover
from tally.importers.fidelity import Account, Holding, Snapshot
from tally.ledger import book, entries, positions
from tally.transaction import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
BOFA = FIXTURES / "bofa_checking.csv"
DISCOVER = FIXTURES / "discover_recent.csv"
POSITIONS = FIXTURES / "fidelity_positions.csv"


def bofa_rows(text: str | None = None) -> list[Transaction]:
    """The rows of a BofA checking export (the table below its summary), oldest first."""
    lines = (text if text is not None else BOFA.read_text(newline="")).splitlines()
    table = lines[lines.index("Date,Description,Amount,Running Bal.") + 1 :]
    rows = []
    for row in csv.reader(table):
        if len(row) != 4 or not row[2].strip():
            continue  # the opening balance, or a blank line
        posted = _date(row[0])
        rows.append(
            Transaction(
                account=bofa.ACCOUNT,
                date=bofa.event_date(row[1], posted),
                posted=posted,
                description=row[1],
                merchant=bofa.clean_merchant(row[1]),
                amount=Decimal(row[2].replace(",", "")),
            )
        )
    return rows


def discover_rows(text: str | None = None) -> list[Transaction]:
    """The rows of a Discover export, newest first as it lists them; money out negative."""
    source = text if text is not None else DISCOVER.read_text(newline="")
    reader = csv.reader(io.StringIO(source))
    next(reader)
    return [
        Transaction(
            account=discover.ACCOUNT,
            date=_date(trans_date),
            posted=_date(post_date),
            description=description,
            merchant=discover.clean_merchant(description),
            amount=-Decimal(amount.replace(",", "")),
            bank_category=category or None,
        )
        for trans_date, post_date, description, amount, category in reader
        if trans_date
    ]


def snapshot(text: str | None = None) -> Snapshot:
    """A Fidelity positions export as a holdings snapshot."""
    source = text if text is not None else POSITIONS.read_text(encoding="utf-8", newline="")
    rows = list(csv.reader(io.StringIO(source)))
    header = rows[0]
    column = {name: i for i, name in enumerate(header)}
    day = None
    accounts: dict[str, Account] = {}
    for row in rows[1:]:
        found = re.search(r"Date downloaded (\w{3})-(\d{2})-(\d{4})", " ".join(row))
        if found:
            month = list(calendar.month_abbr).index(found[1])
            day = datetime.date(int(found[3]), month, int(found[2]))
            continue
        if len(row) < len(header) or not row[column["Symbol"]].strip():
            continue
        label = row[0].strip()[-4:]
        account = accounts.setdefault(label, Account(label, row[1].strip(), None, []))
        symbol, quantity = row[column["Symbol"]].strip(), row[column["Quantity"]].strip()
        if symbol.endswith("**") and not quantity:
            cash = _money(row[column["Current value"]])
            accounts[label] = Account(label, account.name, cash, account.holdings)
            continue
        account.holdings.append(
            Holding(
                symbol,
                Decimal(quantity),
                _money(row[column["Last price"]]),
                _money(row[column["Cost basis total"]]),
            )
        )
    assert day is not None, "no 'Date downloaded' line"
    return Snapshot(day, list(accounts.values()))


def seed(data_dir: Path, rows: list[Transaction]) -> int:
    """Store rows and add them to the ledger, as sync does with SimpleFIN's. The number new."""
    conn = store.connect(data_dir / "tally.db")
    try:
        result = store.save(
            conn,
            rows,
            account=rows[0].account,
            file_name="sample",
            file_sha256=hashlib.sha256(repr(rows).encode()).hexdigest(),
        )
    finally:
        conn.close()
    month_rules = rules.load(data_dir / "rules.toml")
    ledger = book.load(book.folder(data_dir), month_rules)
    meta = data.new_metadata("sample", 0)
    made = [entries.transaction(t, ledger.accounts, month_rules, meta=meta) for t in result.new]
    book.add(ledger, book.new_entries(made, ledger))
    return result.rows_new


def record(data_dir: Path, holdings: Snapshot) -> int:
    """Add a holdings snapshot to the ledger, as sync does with SimpleFIN's. The entries added."""
    ledger = book.load(book.folder(data_dir), rules.load(data_dir / "rules.toml"))
    made = positions.entries(holdings, ledger.entries, ledger.accounts, "simplefin")
    new = book.new_entries(made, ledger)
    book.add(ledger, new)
    return len(new)


def _date(text: str) -> datetime.date:
    month, day, year = (int(part) for part in text.strip().split("/"))
    return datetime.date(year, month, day)


def _money(text: str) -> Decimal:
    return Decimal(text.strip().replace("$", "").replace(",", ""))
