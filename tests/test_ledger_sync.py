from pathlib import Path

import pytest
from beancount import loader
from test_ledger_importers import BOFA
from test_sync import BUDGET_ID, FakeSheet, budget_cell, budget_grid, sync

from tally import rules, sheet, store
from tally.cli import main
from tally.ledger import accounts, book

# Rules and a ledger to match, in the shape of test_sync's.
RULES = (
    'categories = ["Rent + fees", "Groceries"]\n'
    '[[rule]]\nmatch = "MAPLE MARKET"\ncategory = "Groceries"\n'
    '[[rule]]\nmerchant = "FID BKG SVC LLC"\ncategory = "Investing"\n'
)
ACCOUNTS = """
2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:BofA:Checking USD
  tally-account: "bofa_checking"
2026-01-01 open Liabilities:Discover:Card USD
  tally-account: "discover"
2026-01-01 open Assets:Transfers:Discover USD
  category: "Card payment"
2026-01-01 open Assets:BofA:Savings USD
  category: "Savings transfer"
  merchant-contains: "KEEP THE CHANGE|SAV 1234"
2026-01-01 open Assets:CapitalOne:Savings USD
  category: "Savings transfer"
2026-01-01 open Assets:Fidelity:Roth:Cash USD
  category: "Investing"
2026-01-01 open Income:Salary
  category: "Income"
2026-01-01 open Expenses:Rent
  category: "Rent + fees"
2026-01-01 open Expenses:Groceries
  category: "Groceries"
2026-01-01 open Expenses:Uncategorized
  category: "Uncategorized"

2026-02-28 * "Opening balance"
  Assets:BofA:Checking  1200.00 USD
  Equity:Opening-Balances
"""
MAIN = """option "operating_currency" "USD"
include "accounts.beancount"
include "imported/*.beancount"
include "pending.beancount"
"""


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """As test_sync's: a data folder, and a fake Sheet in place of Google's."""
    folder = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(folder))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(sheet, "GoogleSheet", FakeSheet)
    FakeSheet.tab_ids = {"Budget": BUDGET_ID}
    FakeSheet.grids = {BUDGET_ID: budget_grid()}
    FakeSheet.batches, FakeSheet.write_flags, FakeSheet.fail_writes = [], [], False
    return folder


@pytest.fixture
def ledger_ready(data: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    folder = data / "ledger"
    (folder / "imported").mkdir(parents=True)
    (folder / "main.beancount").write_text(MAIN)
    (folder / "accounts.beancount").write_text(ACCOUNTS)
    (folder / "imported" / "2026.beancount").write_text("")
    (data / "config.toml").write_text('sheet_id = "test-sheet"\n')
    (data / "rules.toml").write_text(RULES)
    assert main(["import", str(BOFA)]) == 0
    capsys.readouterr()
    return data


def imported(data: Path) -> str:
    return (data / "ledger" / "imported" / "2026.beancount").read_text()


def test_sync_reports_the_ledger_matches(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = sync(capsys)
    assert code == 0, err
    assert "Ledger: all 13 rows and every month's totals match; bean-check is clean." in out


def test_a_rule_change_moves_the_ledger_entry_too(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "Expenses:Groceries" in imported(ledger_ready)
    (ledger_ready / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))

    code, out, _ = sync(capsys, "--dry-run")
    assert code == 0
    assert "Ledger: would move 1 entries to their current category:" in out
    assert "MAPLE MARKET SPRINGFIELD IL: Groceries → Uncategorized" in out
    assert "Expenses:Groceries" in imported(ledger_ready)

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Ledger: moved 1 entries" in out
    assert "Expenses:Groceries" not in imported(ledger_ready)
    assert "rows and every month's totals match" in out
    assert book.check(ledger_ready / "ledger") == []


def test_an_edit_on_the_posting_line_survives_a_move(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledger_ready / "ledger" / "imported" / "2026.beancount"
    text = imported(ledger_ready).replace(
        "  Expenses:Groceries     42.10 USD", "  Expenses:Groceries     42.10 USD ; weekly shop"
    )
    path.write_text(text)
    (ledger_ready / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))
    assert sync(capsys)[0] == 0
    assert "  Expenses:Uncategorized     42.10 USD ; weekly shop" in imported(ledger_ready)


def test_a_missing_entry_is_reported_in_sync_and_summary(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledger_ready / "ledger" / "imported" / "2026.beancount"
    text = imported(ledger_ready)
    start = text.index('2026-03-13 * "REMITLY INC"')
    path.write_text(text[:start])  # also drops the settled balance after it

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Ledger: The ledger is missing 1 of tally's transactions." in out
    assert "missing: 2026-03-13      -75.25  REMITLY INC" in out

    assert main(["summary", "--as-of", "2026-03-20"]) == 0
    out = capsys.readouterr().out
    assert "The ledger is missing 1 of tally's transactions." in out


def test_a_ledger_problem_never_fails_the_sync(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_file = ledger_ready / "ledger" / "accounts.beancount"
    accounts_file.write_text(ACCOUNTS.replace('category: "Groceries"', ""))
    code, out, err = sync(capsys)
    assert code == 0
    assert "Updated 2 Budget cells" in out
    assert "Ledger: not updated" in err
    assert budget_cell("C6") == 42.1
    assert FakeSheet.write_flags == [True]


def test_summary_says_nothing_when_the_ledger_matches(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["summary", "--as-of", "2026-03-20"]) == 0
    assert "ledger" not in capsys.readouterr().out.lower()


def test_entries_you_wrote_are_left_alone(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledger_ready / "ledger" / "imported" / "2026.beancount"
    mine = '\n2026-03-20 * "Cash gift"\n  Assets:BofA:Checking  -20.00 USD\n  Expenses:Groceries\n'
    path.write_text(imported(ledger_ready) + mine)
    (ledger_ready / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Ledger: moved 1 entries" in out
    assert mine in imported(ledger_ready)


def test_merchant_contains_takes_alternatives() -> None:
    opens, _, _ = loader.load_string(ACCOUNTS)
    found = accounts.from_entries(opens)
    assert found.counter("Savings transfer", "KEEP THE CHANGE TRANSFER") == "Assets:BofA:Savings"
    assert (
        found.counter("Savings transfer", "Online Banking transfer to SAV 1234 Confirmation# 1")
        == "Assets:BofA:Savings"
    )
    assert found.counter("Savings transfer", "CAPITAL ONE") == "Assets:CapitalOne:Savings"
    assert found.category_of("Assets:BofA:Savings") == "Savings transfer"
    assert found.category_of("Assets:BofA:Checking") is None


def test_parity_counts_rows_from_the_database(ledger_ready: Path) -> None:
    from tally.ledger import updating

    conn = store.connect(ledger_ready / "tally.db")
    rows = store.transactions(conn)
    conn.close()
    found = updating.check(ledger_ready, rows, rules.parse(RULES))
    assert found is not None
    assert found.ok
    assert found.rows == 13
    extra = updating.check(ledger_ready, rows[1:], rules.parse(RULES))
    assert extra is not None and extra.extra == 1
