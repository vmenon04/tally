import dataclasses
import datetime
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from tally import store
from tally.importers import bofa, discover
from tally.transaction import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = store.connect(tmp_path / "tally.db")
    yield conn
    conn.close()


def coffee(amount: str = "4.50", day: int = 2, **changes: object) -> Transaction:
    t = Transaction(
        account="discover",
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description="COFFEE SHOP SPRINGFIELD IL",
        merchant="COFFEE SHOP SPRINGFIELD IL",
        amount=Decimal(f"-{amount}"),
        bank_category="Restaurants",
    )
    return dataclasses.replace(t, **changes)  # type: ignore[arg-type]


def save(
    conn: sqlite3.Connection, transactions: list[Transaction], account: str = "discover"
) -> store.ImportResult:
    return store.save(
        conn, transactions, account=account, file_name="f.csv", file_sha256="abc", now=NOW
    )


def test_new_database_has_schema_version(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_round_trip_keeps_every_field(db: sqlite3.Connection) -> None:
    rows = discover.load(FIXTURES / "discover_recent.csv")
    result = save(db, rows)
    assert (result.rows_in_file, result.rows_new) == (9, 9)
    assert store.transactions(db) == rows


def test_round_trip_keeps_two_decimal_places(db: sqlite3.Connection) -> None:
    save(db, [coffee("1450.00", balance=Decimal("1450.00"))])
    [stored] = store.transactions(db)
    assert (str(stored.amount), str(stored.balance)) == ("-1450.00", "1450.00")


def test_bofa_round_trip_keeps_balances(db: sqlite3.Connection) -> None:
    rows = bofa.load(FIXTURES / "bofa_checking.csv")
    save(db, rows, account="bofa_checking")
    assert store.transactions(db, account="bofa_checking") == rows
    assert store.transactions(db, account="discover") == []


def test_same_file_twice_adds_nothing(db: sqlite3.Connection) -> None:
    rows = discover.load(FIXTURES / "discover_recent.csv")
    save(db, rows)
    assert save(db, rows).rows_new == 0
    assert len(store.transactions(db)) == 9


def test_overlapping_file_adds_only_new_rows(db: sqlite3.Connection) -> None:
    rows = discover.load(FIXTURES / "discover_recent.csv")
    save(db, rows[4:])
    assert save(db, rows).rows_new == 4
    assert sorted(store.transactions(db), key=rows.index) == rows


def test_repeat_purchase_seen_in_later_file_is_added(db: sqlite3.Connection) -> None:
    save(db, [coffee()])
    assert save(db, [coffee(), coffee()]).rows_new == 1
    assert len(store.transactions(db)) == 2


def test_file_with_fewer_repeats_removes_nothing(db: sqlite3.Connection) -> None:
    save(db, [coffee(), coffee()])
    assert save(db, [coffee()]).rows_new == 0
    assert len(store.transactions(db)) == 2


@pytest.mark.parametrize(
    "different",
    [
        coffee(amount="4.51"),
        coffee(day=3),
        coffee(posted=datetime.date(2026, 3, 4)),
        coffee(description="COFFEE SHOP SPRINGFIELD IL #2"),
        coffee(account="bofa_checking"),
    ],
)
def test_any_identity_field_makes_a_new_row(db: sqlite3.Connection, different: Transaction) -> None:
    save(db, [coffee()])
    assert save(db, [different], account=different.account).rows_new == 1


def test_changed_bank_category_is_not_a_new_row(db: sqlite3.Connection) -> None:
    save(db, [coffee()])
    assert save(db, [coffee(bank_category="Merchandise")]).rows_new == 0
    assert store.transactions(db)[0].bank_category == "Restaurants"


def test_missing_posted_date_is_part_of_identity(db: sqlite3.Connection) -> None:
    save(db, [coffee(posted=None)])
    assert save(db, [coffee(posted=None)]).rows_new == 0
    assert save(db, [coffee()]).rows_new == 1


def test_overlapping_balances_must_agree(db: sqlite3.Connection) -> None:
    rows = bofa.load(FIXTURES / "bofa_checking.csv")
    save(db, rows, account="bofa_checking")
    changed = [*rows[:-1], dataclasses.replace(rows[-1], balance=Decimal("1.00"))]
    expected = "balance 1.00 doesn't match the stored balance 1698.53"
    with pytest.raises(store.StoreError, match=expected):
        save(db, changed, account="bofa_checking")


def test_failed_import_saves_nothing(db: sqlite3.Connection) -> None:
    save(db, [coffee(balance=Decimal("10.00"))])
    with pytest.raises(store.StoreError):
        save(db, [coffee("9.99"), coffee(balance=Decimal("11.00"))])
    assert len(store.transactions(db)) == 1
    assert db.execute("SELECT count(*) FROM imports").fetchone()[0] == 1


def test_import_is_recorded(db: sqlite3.Connection) -> None:
    save(db, [coffee()])
    result = save(db, [coffee(), coffee("1.00")])
    row = db.execute(
        "SELECT account, file_name, file_sha256, imported_at, rows_in_file, rows_new"
        " FROM imports WHERE id = ?",
        (result.import_id,),
    ).fetchone()
    assert row == ("discover", "f.csv", "abc", "2026-03-20T12:00:00+00:00", 2, 1)


def test_rows_from_another_account_are_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="must belong to 'bofa_checking'"):
        save(db, [coffee()], account="bofa_checking")


def test_fraction_of_a_cent_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="fractions of a cent"):
        save(db, [coffee("4.505")])
    assert store.transactions(db) == []


def test_data_survives_reopening(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "tally.db")
    save(conn, [coffee()])
    conn.close()
    conn = store.connect(tmp_path / "tally.db")
    assert store.transactions(conn) == [coffee()]
    conn.close()


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tally.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(store.StoreError, match="schema version 99, expected 2"):
        store.connect(path)


def bofa_rows() -> list[Transaction]:
    return bofa.load(FIXTURES / "bofa_checking.csv")


def test_bofa_file_continuing_history_without_overlap_is_accepted(db: sqlite3.Connection) -> None:
    rows = bofa_rows()
    save(db, rows[:6], account="bofa_checking")
    assert save(db, rows[6:], account="bofa_checking").rows_new == 7


def test_bofa_file_overlapping_history_is_accepted(db: sqlite3.Connection) -> None:
    rows = bofa_rows()
    save(db, rows[:6], account="bofa_checking")
    assert save(db, rows[4:], account="bofa_checking").rows_new == 7


def test_bofa_gap_is_rejected(db: sqlite3.Connection) -> None:
    rows = bofa_rows()
    save(db, rows[:6], account="bofa_checking")
    expected = "gap: this file starts at balance 2428.78 on 2026-03-10, but the last stored balance"
    with pytest.raises(store.StoreError, match=expected):
        save(db, rows[8:], account="bofa_checking")
    assert len(store.transactions(db)) == 6


def test_older_bofa_file_without_overlap_is_rejected(db: sqlite3.Connection) -> None:
    rows = bofa_rows()
    save(db, rows[6:], account="bofa_checking")
    with pytest.raises(store.StoreError, match="gap"):
        save(db, rows[:6], account="bofa_checking")


def test_discover_has_no_gap_check(db: sqlite3.Connection) -> None:
    save(db, [coffee(day=2)])
    assert save(db, [coffee(day=20)]).rows_new == 1


def test_copy_has_the_data_but_changes_stay_in_memory(tmp_path: Path) -> None:
    path = tmp_path / "tally.db"
    conn = store.connect(path)
    save(conn, [coffee()])
    conn.close()

    copy = store.connect_copy(path)
    assert store.transactions(copy) == [coffee()]
    save(copy, [coffee("1.00")])
    copy.close()

    conn = store.connect(path)
    assert store.transactions(conn) == [coffee()]
    conn.close()


def test_copy_of_missing_database_is_empty_and_creates_no_file(tmp_path: Path) -> None:
    copy = store.connect_copy(tmp_path / "tally.db")
    assert store.transactions(copy) == []
    copy.close()
    assert not (tmp_path / "tally.db").exists()


def test_copy_checks_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "tally.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(store.StoreError, match="schema version 99"):
        store.connect_copy(path)


def test_version_1_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "tally.db"
    conn = sqlite3.connect(path)
    conn.executescript(store._SCHEMA + "PRAGMA user_version = 1;")
    conn.close()
    conn = store.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.sheet_writes(conn, "s") == {}
    conn.close()


def test_sheet_writes_are_remembered_per_sheet(db: sqlite3.Connection) -> None:
    store.record_sheet_writes(db, "a", [("Groceries", "Mar 2026", Decimal("42.10"))], now=NOW)
    store.record_sheet_writes(db, "b", [("Groceries", "Mar 2026", Decimal("1.00"))], now=NOW)
    store.record_sheet_writes(db, "a", [("Groceries", "Mar 2026", Decimal("0"))], now=NOW)
    assert store.sheet_writes(db, "a") == {("Groceries", "Mar 2026"): Decimal("0")}
    assert store.sheet_writes(db, "b") == {("Groceries", "Mar 2026"): Decimal("1.00")}
