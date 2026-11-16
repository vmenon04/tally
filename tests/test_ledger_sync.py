import datetime
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from beancount import loader
from beancount.core.data import Transaction
from sample import bofa_rows, seed

from tally import categorizing, cli, notify, simplefin, store, sure
from tally.alerts import Alert
from tally.cli import main
from tally.ledger import accounts, book, mirror, updating, weekly

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
    (data / "config.toml").write_text("roth_limit = 7500\n")
    (data / "rules.toml").write_text(RULES)
    seed(data, bofa_rows())
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


def test_the_example_folder_syncs_from_a_fresh_start(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(Path(__file__).parent.parent / "example", data)
    code, out, _ = run(capsys, "sync")
    assert code == 0
    assert "SimpleFIN is off: run `tally simplefin-login`" in out
    assert "bean-check is clean" in out
    assert (data / "tally.db").exists()


def test_sync_needs_a_ledger(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data.mkdir()
    (data / "rules.toml").write_text(RULES)
    conn = store.connect(data / "tally.db")
    store.save(conn, bofa_rows(), account="bofa_checking", file_name="a", file_sha256="a")
    conn.close()
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
    # The summary goes by the ledger, not the rule.
    code, out, _ = run(capsys, "summary", "--as-of", "2026-03-20")
    assert code == 0
    rent = next(line for line in out.splitlines() if "Rent + fees" in line)
    assert "$42.10" in rent


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


def test_a_category_set_on_a_pending_entry_is_kept(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = store.connect(ledger_ready / "tally.db")
    store.see_alert(conn, alert(1, 18, "MAPLE MARKET", "-9.00"))
    conn.close()
    run(capsys, "sync")
    pending_file = ledger_ready / "ledger" / "pending.beancount"
    text = pending_file.read_text()
    assert 'tally-category: "Groceries"' in text
    # You change it in Fava...
    pending_file.write_text(text.replace("Expenses:Groceries", "Expenses:Rent"))
    # ...and the next sync keeps it, as a rule for that one purchase.
    code, out, _ = run(capsys, "sync")
    assert code == 0 and "Rent + fees" in out
    after = pending_file.read_text()
    assert "Expenses:Rent" in after and 'tally-category: "Rent + fees"' in after
    assert "set on a pending purchase" in (ledger_ready / "rules.toml").read_text()


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

    def wrong_balance(self: sure.Client, method: str, path: str, **_: Any) -> Any:
        name = path.removeprefix("/api/v1/")
        if name == "transactions":
            # Everything tally sent is there, so only the balance is compared.
            return {name: [], "pagination": {"total_pages": 1}}
        items = (
            [{"id": "a", "name": "Checking", "balance_cents": 1, "classification": "asset"}]
            if name == "accounts"
            else []
        )
        return {name: items, "pagination": {"total_pages": 1}}

    monkeypatch.setattr(sure.Client, "request", wrong_balance)
    monkeypatch.setattr(mirror, "rows", lambda ledger: [])
    code, out, err = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert "  Checking: Sure says 0.01, the ledger " in out
    assert "sync: Sure's balance for Checking differs from the ledger's" in err
    assert "0.01" not in err  # no amounts where the scheduled runs log


def linked_bank(
    ledger_ready: Path,
    monkeypatch: pytest.MonkeyPatch,
    *rows: simplefin.Transaction,
    balance: Decimal = Decimal(0),
) -> None:
    """Link checking to a SimpleFIN account ending 1234 that has these rows."""
    accounts_file = ledger_ready / "ledger" / "accounts.beancount"
    if "simplefin-account" not in accounts_file.read_text():
        accounts_file.write_text(
            accounts_file.read_text().replace(
                '  tally-account: "bofa_checking"\n',
                '  tally-account: "bofa_checking"\n  simplefin-account: "1234"\n',
            )
        )
    account = simplefin.Account(
        "a", "Adv SafeBalance (1234)", "Bank", "USD", balance, NOW.date(), list(rows)
    )
    answer = simplefin.Answer([account], ["Capital One needs you to sign in again"])
    monkeypatch.setattr(cli, "_bank", lambda: lambda: answer)


def bank_row(posted: str, amount: str, description: str) -> simplefin.Transaction:
    day = datetime.date.fromisoformat(posted)
    return simplefin.Transaction(posted, day, None, Decimal(amount), description, False)


def test_sync_adds_the_rows_simplefin_posted_since_the_csvs(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    linked_bank(
        ledger_ready,
        monkeypatch,
        # In the CSV already, described the bridge's way: the same day and amount.
        bank_row("2026-03-13", "-75.25", "REMITLY INC      DES:REMITTANCE ID:XXXXXHIJKLMNO"),
        bank_row("2026-03-16", "-12.00", "MAPLE MARKET 03/15 PURCHASE    SPRINGFIELD IL"),
        simplefin.Transaction("p", None, None, Decimal("-5.00"), "PENDING", True),
    )
    code, out, err = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert "From SimpleFIN, bofa_checking (would add 1):" in out
    assert "  2026-03-15      -12.00  MAPLE MARKET" in out
    assert "sync: SimpleFIN: Capital One needs you to sign in again" in err
    assert "MAPLE MARKET 03/15" not in imported(ledger_ready)

    code, out, _ = run(capsys, "sync")
    assert code == 0
    assert "From SimpleFIN, bofa_checking (added 1):" in out
    text = imported(ledger_ready)
    assert "MAPLE MARKET 03/15 PURCHASE SPRINGFIELD IL" in text  # its spaces collapsed
    assert "  Expenses:Groceries     12.00 USD" in text  # categorized by the rules
    # 13 from the CSV, the one new, and the one still pending.
    assert "Ledger: all 15 imported rows are there once" in out
    assert len(list((ledger_ready / "raw" / "simplefin").glob("*.json"))) == 1
    pending_file = ledger_ready / "ledger" / "pending.beancount"
    assert '"PENDING"' in pending_file.read_text()

    code, out, _ = run(capsys, "sync")
    assert code == 0 and "From SimpleFIN, bofa_checking: nothing new." in out

    # It's no longer pending (it posted, or was let go): it's no longer counted.
    linked_bank(
        ledger_ready,
        monkeypatch,
        bank_row("2026-03-16", "-12.00", "MAPLE MARKET 03/15 PURCHASE    SPRINGFIELD IL"),
    )
    code, out, _ = run(capsys, "sync")
    assert code == 0
    assert '"PENDING"' not in pending_file.read_text()


def test_each_sync_checks_the_ledger_against_the_banks_balance(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    held = checking_before_april(ledger_ready)
    linked_bank(ledger_ready, monkeypatch, balance=held)
    code, out, err = run(capsys, "sync", "--dry-run")
    assert code == 0 and "the bank says" not in out

    linked_bank(ledger_ready, monkeypatch, balance=held + 300)
    code, out, err = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert (
        f"Assets:BofA:Checking: the bank says {held + 300:,.2f}, the ledger's posted rows "
        f"{held:,.2f}" in out
    )
    assert "sync: SimpleFIN: the bank's balance for Assets:BofA:Checking differs" in err
    assert f"{held:,.2f}" not in err  # no amounts where the scheduled runs log


def test_an_account_can_be_linked_for_its_balance_alone() -> None:
    found, _, _ = loader.load_string(
        '2026-01-01 open Assets:Savings USD\n  simplefin-account: "5678"\n'
        '2026-01-01 open Assets:Checking USD\n  tally-account: "bofa_checking"\n'
        '  simplefin-account: "1234"\n'
    )
    linked = accounts.from_entries(found)
    assert linked.bank_accounts == {"5678": "Assets:Savings", "1234": "Assets:Checking"}
    assert linked.simplefin == {"1234": "bofa_checking"}  # rows come in only with tally-account


def test_interest_from_needs_a_simplefin_account_and_no_tally_account() -> None:
    found, _, _ = loader.load_string(
        '2026-01-01 open Assets:Savings USD\n  simplefin-account: "5678"\n'
        '  interest-from: "Income:Interest"\n'
    )
    assert accounts.from_entries(found).interest_from == {"5678": "Income:Interest"}
    for meta in ("", '  tally-account: "bofa_checking"\n  simplefin-account: "5678"\n'):
        found, _, _ = loader.load_string(
            f'2026-01-01 open Assets:Savings USD\n{meta}  interest-from: "Income:Interest"\n'
        )
        with pytest.raises(accounts.AccountsError, match="interest-from"):
            accounts.from_entries(found)


def savings_paying_interest(
    ledger_ready: Path, monkeypatch: pytest.MonkeyPatch, *rows: simplefin.Transaction
) -> None:
    """Link Capital One savings (ending 6262) for its interest, with these rows and a balance
    that is the ledger's plus the interest in them."""
    linked_bank(ledger_ready, monkeypatch)
    accounts_file = ledger_ready / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text().replace(
            "2026-01-01 open Assets:CapitalOne:Savings USD\n",
            '2026-01-01 open Assets:CapitalOne:Savings USD\n  simplefin-account: "6262"\n'
            '  interest-from: "Income:Interest"\n'
            "2026-01-01 open Income:Interest USD\n",
        )
    )
    found, _, _ = loader.load_file(str(ledger_ready / "ledger" / "main.beancount"))
    held = sum(
        (
            p.units.number
            for e in found
            if isinstance(e, Transaction)
            for p in e.postings
            if p.account == "Assets:CapitalOne:Savings" and p.units and p.units.number
        ),
        Decimal(0),
    )
    paid = sum((t.amount for t in simplefin.interest(answer_with(rows), "6262")), Decimal(0))
    checking = simplefin.Account(
        "a", "Adv SafeBalance (1234)", "Bank", "USD", Decimal(0), NOW.date(), []
    )
    savings = simplefin.Account(
        "c",
        "360 Performance Savings (6262)",
        "Capital One",
        "USD",
        held + paid,
        NOW.date(),
        list(rows),
    )
    answer = simplefin.Answer([checking, savings], [])
    monkeypatch.setattr(cli, "_bank", lambda: lambda: answer)


def answer_with(rows: tuple[simplefin.Transaction, ...]) -> simplefin.Answer:
    account = simplefin.Account("c", "Savings (6262)", "", "USD", Decimal(0), NOW.date(), [*rows])
    return simplefin.Answer([account], [])


def test_sync_adds_only_the_interest_of_an_account_with_interest_from(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    savings_paying_interest(
        ledger_ready,
        monkeypatch,
        bank_row("2026-02-28", "0.08", "Monthly Interest Paid"),
        bank_row("2026-03-31", "0.10", "Monthly Interest Paid"),
        # Tally has these from checking's side, or they aren't interest.
        bank_row("2026-03-05", "100.00", "Deposit from BofA Checking"),
        bank_row("2026-03-06", "-0.10", "Interest adjustment"),
        simplefin.Transaction("p", None, None, Decimal("0.11"), "Interest Paid", True),
    )
    code, out, err = run(capsys, "sync", "--dry-run")
    assert code == 0
    assert "Interest from SimpleFIN (would add 2):" in out
    assert "  2026-03-31        0.10  Assets:CapitalOne:Savings" in out
    assert "Assets:CapitalOne:Savings: the bank says" not in out  # not until it's added
    assert "Interest Paid" not in imported(ledger_ready)

    code, out, err = run(capsys, "sync")
    assert code == 0
    assert "Interest from SimpleFIN (added 2):" in out
    assert "Assets:CapitalOne:Savings: the bank says" not in out  # the ledger now matches
    assert "Deposit from BofA" not in imported(ledger_ready)
    found, errors, _ = loader.load_file(str(ledger_ready / "ledger" / "main.beancount"))
    assert not errors
    paid = [
        e
        for e in found
        if isinstance(e, Transaction) and any(p.account == "Income:Interest" for p in e.postings)
    ]
    assert [(e.date, e.payee, e.meta["simplefin-id"]) for e in paid] == [
        (datetime.date(2026, 2, 28), "Capital One", "c:2026-02-28"),
        (datetime.date(2026, 3, 31), "Capital One", "c:2026-03-31"),
    ]
    assert "Ledger: all" in out  # not counted as rows tally.db is missing

    code, out, _ = run(capsys, "sync")
    assert code == 0 and "Interest from SimpleFIN" not in out


def ntfy_posts(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    assert main(["ntfy-setup"]) == 0
    posted: list[bytes] = []

    def post(url: str, **kwargs: Any) -> requests.Response:
        posted.append(kwargs["data"])
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests, "post", post)
    return posted


def test_a_sync_announces_new_pending_purchases_once(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_row = simplefin.Transaction("p", None, None, Decimal("-5.00"), "OAK BAKERY", True)
    held = checking_before_april(ledger_ready)
    linked_bank(ledger_ready, monkeypatch, pending_row, balance=held)
    posted = ntfy_posts(monkeypatch)
    capsys.readouterr()
    run(capsys, "sync", "--dry-run")
    assert posted == []  # a dry run announces nothing

    code, out, _ = run(capsys, "sync")
    assert code == 0 and "Notified: 1 new pending purchase" in out
    run(capsys, "sync")
    assert posted == [
        b"a bank needs you to sign in again at SimpleFIN",  # what linked_bank's bridge says
        b"1 new pending purchase",
    ]


def test_simplefin_failing_is_announced_once_it_lasts(
    ledger_ready: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = ntfy_posts(monkeypatch)

    def down() -> simplefin.Answer:
        raise simplefin.SimpleFINError("couldn't reach the bridge: ConnectionError")

    linked_bank(ledger_ready, monkeypatch)
    monkeypatch.setattr(cli, "_bank", lambda: down)
    code, _, err = run(capsys, "sync")
    assert code == 0 and "sync: SimpleFIN: couldn't reach the bridge" in err
    assert posted == []  # one failure is network noise
    monkeypatch.setattr(notify, "BANK_GRACE", datetime.timedelta(0))
    run(capsys, "sync")
    assert posted == [b"can't read your accounts through SimpleFIN"]
