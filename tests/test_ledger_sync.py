import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from beancount import loader
from beancount.core.data import Transaction
from test_ledger_importers import BOFA

from tally import categorizing, store, sure
from tally.alerts import Alert
from tally.cli import main
from tally.ledger import accounts, book, updating, weekly

# Rules and a ledger to match.
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
include "plan.beancount"
include "imported/*.beancount"
include "pending.beancount"
"""
PLAN = """
2026-01-01 custom "budget" Expenses:Rent "monthly" 800.00 USD
2026-01-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD
2026-03-01 custom "roth-plan" 500.00 USD
2026-04-01 custom "paychecks" 2
2026-04-01 custom "roth-plan" 400.00 USD
"""
NOW = datetime.datetime(2026, 3, 18, tzinfo=datetime.UTC)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(folder))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    return folder


@pytest.fixture
def ledger_ready(data: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    folder = data / "ledger"
    (folder / "imported").mkdir(parents=True)
    (folder / "main.beancount").write_text(MAIN)
    (folder / "accounts.beancount").write_text(ACCOUNTS)
    (folder / "plan.beancount").write_text(PLAN)
    (folder / "imported" / "2026.beancount").write_text("")
    # An old config.toml still has sheet_id; it's ignored.
    (data / "config.toml").write_text('sheet_id = "old-sheet"\nroth_limit = 7500\n')
    (data / "rules.toml").write_text(RULES)
    assert main(["import", str(BOFA)]) == 0
    capsys.readouterr()
    return data


def imported(data: Path) -> str:
    return (data / "ledger" / "imported" / "2026.beancount").read_text()


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def claude_answers(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> None:
    def ask(question: str, schema: dict[str, Any], token: str | None, **_: Any) -> Any:
        return {"answers": [{"merchant": m, "category": c} for m, c in answers.items()]}

    monkeypatch.setattr(categorizing, "ask_claude", ask)


def test_sync_checks_the_ledger(ledger_ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "sync")
    assert code == 0
    assert "Ledger: all 13 imported rows are there once; bean-check is clean." in out


def test_sync_needs_a_ledger(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data.mkdir()
    (data / "rules.toml").write_text(RULES)
    assert main(["import", str(BOFA)]) == 0
    code, _, err = run(capsys, "sync")
    assert code == 1
    assert "there's no ledger" in err


def test_claude_fills_in_uncategorized_entries(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "  Expenses:Uncategorized   75.25 USD" in imported(ledger_ready)
    claude_answers(monkeypatch, {"REMITLY INC": "Groceries"})

    code, out, _ = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert "REMITLY INC → Groceries" in out
    assert "Would move" in out and "REMITLY INC: Uncategorized → Groceries" in out
    assert "  Expenses:Uncategorized   75.25 USD" in imported(ledger_ready)

    code, out, _ = run(capsys, "sync")
    assert code == 0
    assert "Moved" in out
    assert "  Expenses:Groceries   75.25 USD" in imported(ledger_ready)
    assert 'merchant = "REMITLY INC"' in (ledger_ready / "rules.toml").read_text()
    assert book.check(ledger_ready / "ledger") == []


def test_a_category_you_set_in_the_ledger_stays(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledger_ready / "ledger" / "imported" / "2026.beancount"
    path.write_text(
        imported(ledger_ready).replace(
            "  Expenses:Groceries     42.10 USD", "  Expenses:Rent  42.10 USD"
        )
    )
    code, _, _ = run(capsys, "sync")
    assert code == 0
    assert "  Expenses:Rent  42.10 USD" in imported(ledger_ready)
    # Reports go by the ledger, not the rule.
    code, out, _ = run(capsys, "report", "2026-03")
    assert code == 0
    rent = next(line for line in out.splitlines() if "Rent + fees" in line)
    groceries = next(line for line in out.splitlines() if "Groceries" in line)
    assert "$42.10" in rent and "$0.00" in groceries


def test_claude_failing_doesnt_stop_the_sync(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "sync")  # conftest's Claude always fails
    assert code == 0
    assert "sync: Claude didn't categorize" in err
    assert "bean-check is clean" in out


def alert(n: int, day: int, merchant: str, amount: str) -> Alert:
    return Alert(
        "bofa_checking",
        datetime.date(2026, 3, day),
        merchant,
        Decimal(amount),
        f"<{n}@example.com>",
        datetime.datetime(2026, 3, day, tzinfo=datetime.UTC),
    )


def test_a_category_set_on_a_pending_entry_is_kept(ledger_ready: Path) -> None:
    conn = store.connect(ledger_ready / "tally.db")
    store.save_alert(conn, alert(1, 18, "MAPLE MARKET", "-9.00"), now=NOW)
    session = updating.open_session(ledger_ready, conn)
    assert session is not None
    updating.refresh_pending(session, conn)
    pending_file = ledger_ready / "ledger" / "pending.beancount"
    text = pending_file.read_text()
    assert 'tally-category: "Groceries"' in text
    # You change it in Fava...
    pending_file.write_text(text.replace("Expenses:Groceries", "Expenses:Rent"))
    # ...and the next alerts run keeps it, as a rule for that one purchase.
    session = updating.open_session(ledger_ready, conn)
    assert session is not None
    kept = updating.refresh_pending(session, conn)
    conn.close()
    assert kept and "Rent + fees" in kept[0]
    after = pending_file.read_text()
    assert "Expenses:Rent" in after and 'tally-category: "Rent + fees"' in after
    assert "corrected" in (ledger_ready / "rules.toml").read_text()


def test_summary_takes_the_plan_from_the_ledger(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert code == 0
    assert "of   $300.00" in out  # Groceries' plan
    assert "April: 2 paycheck(s), $1,100.00 of spending planned, $400.00 to the Roth IRA." in out
    assert "FROM THE LEDGER (bean-query)" in out
    assert "Budget tab" not in out


def test_a_summary_without_a_plan_says_so(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (ledger_ready / "ledger" / "plan.beancount").write_text("")
    code, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert code == 0
    assert "The ledger has no plan yet" in out


def test_a_missing_entry_is_in_the_summary(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledger_ready / "ledger" / "imported" / "2026.beancount"
    text = imported(ledger_ready)
    path.write_text(text[: text.index('2026-03-13 * "REMITLY INC"')])
    code, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert code == 0
    assert "The ledger is missing 1 imported transaction(s)." in out


def test_summary_says_nothing_about_a_ledger_that_checks_out(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert "ledger" not in out.split("FROM THE LEDGER")[0].lower()


def test_a_broken_ledger_still_sends_the_summary(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (ledger_ready / "ledger" / "accounts.beancount").write_text(
        ACCOUNTS + '2026-01-01 open Assets:Other USD\n  tally-account: "discover"\n'
    )
    code, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert code == 0
    assert "Couldn't read the ledger" in out


def test_weekly_report_from_the_ledger(ledger_ready: Path) -> None:
    found = weekly.build(
        ledger_ready / "ledger", datetime.date(2026, 4, 9), Decimal(7500), Decimal(140)
    )
    lines = {line.category: line for line in found.lines}
    # March, by purchase day: groceries 42.10 bought 03/07, posted 03/09.
    assert lines["Groceries"].last_month == Decimal("42.10")
    assert found.roth_in == Decimal("640.00")  # 500 into the Roth in March, 140 elsewhere
    assert "From the ledger" in weekly.format_html(found)


def test_check_counts_rows_and_finds_extras(ledger_ready: Path) -> None:
    conn = store.connect(ledger_ready / "tally.db")
    found = updating.check(ledger_ready, conn)
    assert found is not None and found.ok and found.rows == 13
    conn.execute("DELETE FROM transactions WHERE id = (SELECT min(id) FROM transactions)")
    conn.commit()
    extra = updating.check(ledger_ready, conn)
    conn.close()
    assert extra is not None and extra.extra == 1


BALANCE_EMAIL = Path(__file__).parent / "fixtures" / "bofa_balance_alert.eml"


def a_balance_email(data: Path, amount: str) -> None:
    """Bank of America says checking had amount on Apr 1, 2026."""
    folder = data / "alerts" / "gmail"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "balance.eml").write_text(BALANCE_EMAIL.read_text().replace("$1,234.56", amount))


def checking_before_april(data: Path) -> Decimal:
    found, _, _ = loader.load_file(str(data / "ledger" / "main.beancount"))
    return sum(
        (
            p.units.number
            for e in found
            if isinstance(e, Transaction)
            and e.meta.get("purchased", e.date) < datetime.date(2026, 4, 1)
            for p in e.postings
            if p.account == "Assets:BofA:Checking" and p.units is not None and p.units.number
        ),
        Decimal(0),
    )


def test_the_ledger_is_checked_against_the_banks_balance(ledger_ready: Path) -> None:
    held = checking_before_april(ledger_ready)
    conn = store.connect(ledger_ready / "tally.db")
    a_balance_email(ledger_ready, f"${held:,.2f}")
    agrees = updating.check(ledger_ready, conn)
    a_balance_email(ledger_ready, f"${held + 300:,.2f}")
    short = updating.check(ledger_ready, conn)
    conn.close()
    assert agrees is not None and agrees.ok
    assert short is not None and not short.ok
    assert short.problems() == [
        f"Bank of America said Assets:BofA:Checking had ${held + 300:,.2f} on Apr 01, but the "
        f"ledger has ${held:,.2f} by then ($300.00 more at the bank): a CSV import should "
        "explain it."
    ]


def test_merchant_contains_takes_alternatives() -> None:
    opens, _, _ = loader.load_string(ACCOUNTS)
    found = accounts.from_entries(opens)
    assert found.counter("Savings transfer", "KEEP THE CHANGE TRANSFER") == "Assets:BofA:Savings"
    assert found.counter("Savings transfer", "Online Banking transfer to SAV 1234") == (
        "Assets:BofA:Savings"
    )
    assert found.counter("Savings transfer", "CAPITAL ONE") == "Assets:CapitalOne:Savings"
    assert found.category_of("Assets:BofA:Checking") is None


def test_sync_fills_sure_once_a_key_is_saved(
    ledger_ready: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts_file = ledger_ready / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text().replace(
            '  tally-account: "bofa_checking"\n',
            '  tally-account: "bofa_checking"\n  sure-account: "Checking"\n',
        )
    )
    _, out, _ = run(capsys, "sync", "--dry-run")
    assert "Sure:" not in out  # no key yet

    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "sure-api-key").write_text("key\n")
    sent: list[str] = []

    def request(self: sure.Client, method: str, path: str, **_: Any) -> Any:
        sent.append(method)
        name = path.removeprefix("/api/v1/")
        items = [{"id": "a", "name": "Checking"}] if name == "accounts" else []
        return {name: items, "pagination": {"total_pages": 1}} if method == "GET" else {"id": "x"}

    monkeypatch.setattr(sure.Client, "request", request)
    code, out, _ = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert "Sure: 13 to add, 0 to correct, 0 to remove, 0 already right." in out
    assert set(sent) == {"GET"}  # a dry run only reads

    code, out, _ = run(capsys, "sync")
    assert code == 0 and "Sure: 13 to add" in out
    assert sent.count("POST") > 13  # the transactions, and their categories

    def down(self: sure.Client, method: str, path: str, **_: Any) -> Any:
        raise sure.SureError("couldn't reach Sure at http://127.0.0.1:3200: ConnectionError")

    monkeypatch.setattr(sure.Client, "request", down)
    code, out, err = run(capsys, "sync")
    assert code == 0  # the ledger is still brought up to date
    assert "sync: Sure not updated: couldn't reach Sure" in err
