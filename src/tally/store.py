"""The SQLite database that holds every imported transaction.

Banks give no transaction IDs, so a row's identity is its account, dates, raw description
and amount. Identical rows on the same day are real (two equal coffees), so an import adds
only as many copies of an identity as the file has beyond what is already stored.
"""

import datetime
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tally.transaction import Transaction

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rows_in_file INTEGER NOT NULL,
    rows_new INTEGER NOT NULL
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    posted TEXT,
    description TEXT NOT NULL,
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    bank_category TEXT,
    balance_cents INTEGER,
    import_id INTEGER NOT NULL REFERENCES imports (id)
);

CREATE INDEX transactions_identity
    ON transactions (account, date, posted, description, amount_cents);
"""

type _Identity = tuple[str, str, str | None, str, int]


class StoreError(Exception):
    """An import would contradict what the database already holds."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_id: int
    rows_in_file: int
    rows_new: int


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating it if needed."""
    conn = sqlite3.connect(path, autocommit=False)
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        conn.commit()
    elif version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} has schema version {version}, expected {SCHEMA_VERSION}")
    return conn


def save(
    conn: sqlite3.Connection,
    transactions: Sequence[Transaction],
    *,
    account: str,
    file_name: str,
    file_sha256: str,
    now: datetime.datetime | None = None,
) -> ImportResult:
    """Store one file's transactions, skipping rows already stored. All or nothing."""
    if any(t.account != account for t in transactions):
        raise ValueError(f"every transaction must belong to {account!r}")
    imported_at = (now or datetime.datetime.now(datetime.UTC)).isoformat(timespec="seconds")

    with conn:
        import_id = conn.execute(
            "INSERT INTO imports"
            " (account, file_name, file_sha256, imported_at, rows_in_file, rows_new)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (account, file_name, file_sha256, imported_at, len(transactions)),
        ).lastrowid
        assert import_id is not None

        stored: dict[_Identity, list[int | None]] = {}
        seen: Counter[_Identity] = Counter()
        rows_new = 0
        for t in transactions:
            identity = _identity(t)
            if identity not in stored:
                stored[identity] = _stored_balances(conn, identity)
            occurrence = seen[identity]
            seen[identity] += 1

            balance = _cents(t.balance) if t.balance is not None else None
            if occurrence < len(stored[identity]):
                # Already stored. Where the bank gives running balances, the overlap must agree,
                # otherwise this file and the database disagree about the account's history.
                stored_balance = stored[identity][occurrence]
                if balance is not None and stored_balance is not None and balance != stored_balance:
                    raise StoreError(
                        f"{t.date} {t.description!r} {t.amount}: balance {t.balance} doesn't "
                        f"match the stored balance {_dollars(stored_balance)}"
                    )
                continue

            conn.execute(
                "INSERT INTO transactions (account, date, posted, description, merchant,"
                " amount_cents, bank_category, balance_cents, import_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*identity[:4], t.merchant, identity[4], t.bank_category, balance, import_id),
            )
            rows_new += 1

        conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (rows_new, import_id))
    return ImportResult(import_id, len(transactions), rows_new)


def transactions(conn: sqlite3.Connection, account: str | None = None) -> list[Transaction]:
    """Stored transactions in the order they were added, optionally for one account."""
    query = (
        "SELECT account, date, posted, description, merchant, amount_cents, bank_category,"
        " balance_cents FROM transactions"
    )
    params: tuple[str, ...] = ()
    if account is not None:
        query += " WHERE account = ?"
        params = (account,)
    rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [
        Transaction(
            account=row[0],
            date=datetime.date.fromisoformat(row[1]),
            posted=datetime.date.fromisoformat(row[2]) if row[2] is not None else None,
            description=row[3],
            merchant=row[4],
            amount=_dollars(row[5]),
            bank_category=row[6],
            balance=_dollars(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]


def _identity(t: Transaction) -> _Identity:
    posted = t.posted.isoformat() if t.posted is not None else None
    return (t.account, t.date.isoformat(), posted, t.description, _cents(t.amount))


def _stored_balances(conn: sqlite3.Connection, identity: _Identity) -> list[int | None]:
    rows = conn.execute(
        "SELECT balance_cents FROM transactions WHERE account = ? AND date = ?"
        " AND posted IS ? AND description = ? AND amount_cents = ? ORDER BY id",
        identity,
    ).fetchall()
    return [row[0] for row in rows]


def _cents(amount: Decimal) -> int:
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount} has fractions of a cent")
    return int(cents)


def _dollars(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)
