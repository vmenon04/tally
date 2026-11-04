"""The SQLite database that holds every imported transaction, and the alerts read from email.

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

from tally.alerts import Alert
from tally.transaction import Transaction

SCHEMA_VERSION = 5

# Version 1. Later versions are reached by the migrations below, in order.
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

# _MIGRATIONS[n] takes the schema from version n to n + 1.
_MIGRATIONS = {
    1: """
CREATE TABLE sheet_writes (
    sheet_id TEXT NOT NULL,
    row_label TEXT NOT NULL,
    month TEXT NOT NULL,
    value_cents INTEGER NOT NULL,
    written_at TEXT NOT NULL,
    PRIMARY KEY (sheet_id, row_label, month)
);
""",
    2: """
CREATE TABLE sheet_categories (
    sheet_id TEXT NOT NULL,
    transaction_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (sheet_id, transaction_id)
);
""",
    3: """
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    read_at TEXT NOT NULL
);
""",
    4: """
CREATE TABLE notifications (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
""",
}

type _Identity = tuple[str, str, str | None, str, int]


class StoreError(Exception):
    """An import would contradict what the database already holds."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_id: int
    rows_in_file: int
    new: tuple[Transaction, ...]

    @property
    def rows_new(self) -> int:
        return len(self.new)


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating it if needed. Only the owner may read the file (600)."""
    path.touch(exist_ok=True)
    path.chmod(0o600)
    return _prepare(sqlite3.connect(path, autocommit=False), path)


def connect_copy(path: Path) -> sqlite3.Connection:
    """An in-memory copy of the database (empty if it doesn't exist), for trial runs."""
    conn = sqlite3.connect(":memory:", autocommit=False)
    if path.exists():
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            source.backup(conn)
        finally:
            source.close()
    return _prepare(conn, path)


def _prepare(conn: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} has schema version {version}, expected {SCHEMA_VERSION}")
    if version == 0:
        conn.executescript(_SCHEMA + "PRAGMA user_version = 1;")
        version = 1
    while version < SCHEMA_VERSION:
        conn.executescript(_MIGRATIONS[version] + f"PRAGMA user_version = {version + 1};")
        version += 1
    conn.commit()
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
        return _save(conn, transactions, account, file_name, file_sha256, imported_at)


def _save(
    conn: sqlite3.Connection,
    transactions: Sequence[Transaction],
    account: str,
    file_name: str,
    file_sha256: str,
    imported_at: str,
) -> ImportResult:
    last_balance = conn.execute(
        "SELECT date, balance_cents FROM transactions"
        " WHERE account = ? AND balance_cents IS NOT NULL"
        " ORDER BY posted DESC, id DESC LIMIT 1",
        (account,),
    ).fetchone()

    import_id = conn.execute(
        "INSERT INTO imports"
        " (account, file_name, file_sha256, imported_at, rows_in_file, rows_new)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (account, file_name, file_sha256, imported_at, len(transactions)),
    ).lastrowid
    assert import_id is not None

    stored: dict[_Identity, list[int | None]] = {}
    seen: Counter[_Identity] = Counter()
    new: list[Transaction] = []
    overlaps = False
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
            overlaps = True
            continue

        conn.execute(
            "INSERT INTO transactions (account, date, posted, description, merchant,"
            " amount_cents, bank_category, balance_cents, import_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*identity[:4], t.merchant, identity[4], t.bank_category, balance, import_id),
        )
        new.append(t)

    # A file that shares no rows with the stored history must continue it exactly,
    # otherwise transactions between the two are missing.
    first = transactions[0] if transactions else None
    if last_balance and first and first.balance is not None and not overlaps:
        opening = _cents(first.balance) - _cents(first.amount)
        if opening != last_balance[1]:
            raise StoreError(
                f"gap: this file starts at balance {_dollars(opening)} on {first.date}, "
                f"but the last stored balance is {_dollars(last_balance[1])} on "
                f"{last_balance[0]}. Download a date range that overlaps what is stored."
            )

    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (len(new), import_id))
    return ImportResult(import_id, len(transactions), tuple(new))


def transactions(conn: sqlite3.Connection, account: str | None = None) -> list[Transaction]:
    """Stored transactions in the order they were added, optionally for one account."""
    return [t for _, t in transactions_with_ids(conn, account)]


def transactions_with_ids(
    conn: sqlite3.Connection, account: str | None = None
) -> list[tuple[int, Transaction]]:
    """Like transactions(), paired with each one's ID in the database."""
    query = (
        "SELECT id, account, date, posted, description, merchant, amount_cents, bank_category,"
        " balance_cents FROM transactions"
    )
    params: tuple[str, ...] = ()
    if account is not None:
        query += " WHERE account = ?"
        params = (account,)
    rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [
        (
            row[0],
            Transaction(
                account=row[1],
                date=datetime.date.fromisoformat(row[2]),
                posted=datetime.date.fromisoformat(row[3]) if row[3] is not None else None,
                description=row[4],
                merchant=row[5],
                amount=_dollars(row[6]),
                bank_category=row[7],
                balance=_dollars(row[8]) if row[8] is not None else None,
            ),
        )
        for row in rows
    ]


def save_alert(
    conn: sqlite3.Connection, alert: Alert, now: datetime.datetime | None = None
) -> bool:
    """Store an alert unless one with the same Message-ID is stored. True if it was new."""
    read_at = (now or datetime.datetime.now(datetime.UTC)).isoformat(timespec="seconds")
    with conn:
        cursor = conn.execute(
            "INSERT INTO alerts"
            " (message_id, account, date, merchant, amount_cents, received_at, read_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
            (
                alert.message_id,
                alert.account,
                alert.date.isoformat(),
                alert.merchant,
                _cents(alert.amount),
                alert.received.isoformat(),
                read_at,
            ),
        )
    return cursor.rowcount == 1


def alerts(conn: sqlite3.Connection) -> list[Alert]:
    """Stored alerts, oldest purchase first."""
    return [a for _, a in alerts_with_ids(conn)]


def alerts_with_ids(conn: sqlite3.Connection) -> list[tuple[int, Alert]]:
    """Like alerts(), paired with each one's ID in the database."""
    rows = conn.execute(
        "SELECT id, account, date, merchant, amount_cents, message_id, received_at FROM alerts"
        " ORDER BY date, received_at, id"
    ).fetchall()
    return [
        (
            row[0],
            Alert(
                account=row[1],
                date=datetime.date.fromisoformat(row[2]),
                merchant=row[3],
                amount=_dollars(row[4]),
                message_id=row[5],
                received=datetime.datetime.fromisoformat(row[6]),
            ),
        )
        for row in rows
    ]


def notifications(conn: sqlite3.Connection) -> dict[str, datetime.datetime]:
    """When each notification (by its key) was last sent."""
    rows = conn.execute("SELECT key, sent_at FROM notifications").fetchall()
    return {key: datetime.datetime.fromisoformat(sent_at) for key, sent_at in rows}


def record_notifications(
    conn: sqlite3.Connection, keys: Sequence[str], now: datetime.datetime
) -> None:
    sent_at = now.isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            "INSERT INTO notifications (key, sent_at) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET sent_at = excluded.sent_at",
            [(key, sent_at) for key in keys],
        )


def forget_notification(conn: sqlite3.Connection, key: str) -> None:
    """Allow a notification to be sent again straight away (the problem was fixed)."""
    with conn:
        conn.execute("DELETE FROM notifications WHERE key = ?", (key,))


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


def sheet_writes(conn: sqlite3.Connection, sheet_id: str) -> dict[tuple[str, str], Decimal]:
    """What tally last wrote to each (row label, month) cell of the given Sheet."""
    rows = conn.execute(
        "SELECT row_label, month, value_cents FROM sheet_writes WHERE sheet_id = ?", (sheet_id,)
    ).fetchall()
    return {(row, month): _dollars(cents) for row, month, cents in rows}


def record_sheet_writes(
    conn: sqlite3.Connection,
    sheet_id: str,
    cells: Sequence[tuple[str, str, Decimal]],
    now: datetime.datetime | None = None,
) -> None:
    """Remember the values tally wrote (or found already correct) in the given Sheet."""
    written_at = (now or datetime.datetime.now(datetime.UTC)).isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            "INSERT INTO sheet_writes (sheet_id, row_label, month, value_cents, written_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (sheet_id, row_label, month)"
            " DO UPDATE SET value_cents = excluded.value_cents, written_at = excluded.written_at",
            [(sheet_id, row, month, _cents(value), written_at) for row, month, value in cells],
        )


def sheet_categories(conn: sqlite3.Connection, sheet_id: str) -> dict[int, str]:
    """The category tally last wrote for each transaction on the Sheet's Transactions tab."""
    rows = conn.execute(
        "SELECT transaction_id, category FROM sheet_categories WHERE sheet_id = ?", (sheet_id,)
    ).fetchall()
    return dict(rows)


def replace_sheet_categories(
    conn: sqlite3.Connection, sheet_id: str, categories: dict[int, str]
) -> None:
    """Record the categories of a freshly rewritten Transactions tab, replacing the old record."""
    with conn:
        conn.execute("DELETE FROM sheet_categories WHERE sheet_id = ?", (sheet_id,))
        conn.executemany(
            "INSERT INTO sheet_categories (sheet_id, transaction_id, category) VALUES (?, ?, ?)",
            [(sheet_id, tid, category) for tid, category in categories.items()],
        )


def last_new_transaction(conn: sqlite3.Connection) -> datetime.datetime | None:
    """When tally last took in a transaction it didn't have: from an export, or an alert."""
    row = conn.execute(
        "SELECT max(t) FROM ("
        " SELECT max(imported_at) AS t FROM imports WHERE rows_new > 0"
        " UNION ALL SELECT max(read_at) FROM alerts)"
    ).fetchone()
    return datetime.datetime.fromisoformat(row[0]) if row and row[0] else None
