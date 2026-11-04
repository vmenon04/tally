import datetime
import io
import shutil
from decimal import Decimal
from pathlib import Path

from test_ledger_importers import BOFA, DISCOVER, OPENS, RULES

from tally import importing, store
from tally.alerts import Alert
from tally.ledger import book, updating

MAIN = """\
include "accounts.beancount"
include "imported/*.beancount"
include "pending.beancount"
"""


def make_data_dir(tmp_path: Path, *, ledger: bool = True) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "rules.toml").write_text(RULES)
    if ledger:
        folder = data_dir / "ledger"
        (folder / "imported").mkdir(parents=True)
        (folder / "main.beancount").write_text(MAIN)
        (folder / "accounts.beancount").write_text(OPENS)
        # An empty year file so the glob include matches before the first import.
        (folder / "imported" / "2026.beancount").write_text("")
    return data_dir


def run(data_dir: Path, *paths: Path, dry_run: bool = False) -> tuple[bool, str, str]:
    out, err = io.StringIO(), io.StringIO()
    ok = importing.run(
        list(paths), data_dir, dry_run=dry_run, out=out, err=err, today=datetime.date(2026, 4, 1)
    )
    return ok, out.getvalue(), err.getvalue()


def ledger_text(data_dir: Path) -> str:
    return (data_dir / "ledger" / "imported" / "2026.beancount").read_text()


def test_import_writes_the_ledger_and_it_checks(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    ok, out, err = run(data_dir, BOFA, DISCOVER)
    assert ok, err
    assert err == ""
    assert "ledger: 15 new entries" in out  # 13 rows + 2 balances
    assert "ledger: 9 new entries" in out
    assert ledger_text(data_dir).count("  tally-id: ") == 22
    assert ledger_text(data_dir).startswith("; Written by tally import")
    assert book.check(data_dir / "ledger") == []


def test_reimporting_adds_nothing_to_the_ledger(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    run(data_dir, BOFA)
    before = ledger_text(data_dir)
    ok, out, _ = run(data_dir, BOFA)
    assert ok
    assert "ledger: 0 new entries" in out
    assert ledger_text(data_dir) == before


def test_backfill_adds_to_the_ledger_rows_tally_db_already_has(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path, ledger=False)
    run(data_dir, BOFA)
    shutil.rmtree(data_dir / "ledger", ignore_errors=True)
    ledger_dir = make_data_dir(tmp_path / "other") / "ledger"
    shutil.copytree(ledger_dir, data_dir / "ledger")
    ok, out, _ = run(data_dir, BOFA)
    assert ok
    assert "0 new, 13 already imported" in out
    assert "ledger: 15 new entries" in out


def test_without_a_ledger_import_works_as_before(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path, ledger=False)
    ok, out, err = run(data_dir, BOFA)
    assert ok, err
    assert "ledger" not in out
    assert not (data_dir / "ledger").exists()


def test_a_ledger_without_a_needed_account_stops_the_import(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    accounts = data_dir / "ledger" / "accounts.beancount"
    accounts.write_text(OPENS.replace('category: "Groceries"', ""))
    ok, _, err = run(data_dir, BOFA)
    assert not ok
    assert "Nothing imported" in err
    assert not (data_dir / "tally.db").exists()


def test_dry_run_changes_no_ledger_file(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    ok, out, _ = run(data_dir, BOFA, dry_run=True)
    assert ok
    assert "ledger: 15 new entries" in out
    assert ledger_text(data_dir) == ""


def test_a_bean_check_problem_is_reported(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    # Without the opening balance, the file's first balance assertion fails.
    accounts = data_dir / "ledger" / "accounts.beancount"
    accounts.write_text(OPENS.split("2026-02-28 *")[0])
    ok, _, err = run(data_dir, BOFA)
    assert ok
    assert "bean-check found" in err
    assert "Balance failed" in err


def alert(date: datetime.date, merchant: str, amount: str, n: int) -> Alert:
    return Alert(
        account="bofa_checking",
        date=date,
        merchant=merchant,
        amount=Decimal(amount),
        message_id=f"<{n}@example.com>",
        received=datetime.datetime(2026, 3, 20, tzinfo=datetime.UTC),
    )


def test_waiting_alerts_go_to_pending_after_the_last_balance(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    run(data_dir, BOFA)  # covers 03/01-03/14; balance asserted on 03/15
    conn = store.connect(data_dir / "tally.db")
    now = datetime.datetime(2026, 3, 20, tzinfo=datetime.UTC)
    store.save_alert(conn, alert(datetime.date(2026, 3, 13), "SNACK BAR", "-4.00", 1), now=now)
    store.save_alert(conn, alert(datetime.date(2026, 3, 18), "MAPLE MARKET", "-9.00", 2), now=now)
    session = updating.open_session(data_dir)
    assert session is not None
    assert updating.refresh_pending(session, conn)
    conn.close()

    pending = (data_dir / "ledger" / "pending.beancount").read_text()
    assert pending.count(" ! ") == 2
    assert '2026-03-15 ! "SNACK BAR"' in pending
    assert "purchased: 2026-03-13" in pending
    assert '2026-03-18 ! "MAPLE MARKET"' in pending
    assert book.check(data_dir / "ledger") == []


def test_pending_is_rewritten_only_when_it_changes(tmp_path: Path) -> None:
    data_dir = make_data_dir(tmp_path)
    run(data_dir, BOFA)
    conn = store.connect(data_dir / "tally.db")
    session = updating.open_session(data_dir)
    assert session is not None
    assert not updating.refresh_pending(session, conn)  # the import already wrote it
    conn.close()
