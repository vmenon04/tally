import copy
import datetime
import smtplib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_mailer import FakeSMTP

from tally import budget, categorizing, gmail, sheet, store, transactions_tab
from tally.alerts import Alert
from tally.cli import main
from tally.sheet import Grid

FIXTURES = Path(__file__).parent / "fixtures"
BUDGET_ID = 111

RULES = (
    'categories = ["Rent + fees", "Groceries"]\n'
    '[[rule]]\nmatch = "MAPLE MARKET"\ncategory = "Groceries"\n'
    '[[rule]]\nmerchant = "FID BKG SVC LLC"\ncategory = "Investing"\n'
)


def budget_grid() -> Grid:
    """A small Budget tab laid out like the real one."""
    return [
        ["Budget: plan vs. actual", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["", "Plan / mo", "Mar 2026", "Apr 2026", "Avg actual", "What it covers"],
        ["SPENDING", "", "", "", "", ""],
        ["Rent + fees", 800, "", "", '=IF(COUNT(C5:D5)=0,"",AVERAGE(C5:D5))', "note"],
        ["Groceries", 300, "", "", "", ""],
        ["Total spending", "=SUM(B5:B6)", "=SUM(C5:C6)", "=SUM(D5:D6)", "", ""],
        [budget.ROTH_ROW, "", 500, 400, "", ""],
    ]


class FakeSheet:
    """Stands in for GoogleSheet: shared tabs that batch_update really changes."""

    tab_ids: dict[str, int] = {}
    grids: dict[int, Grid] = {}
    batches: list[list[sheet.Request]] = []
    write_flags: list[bool] = []
    fail_writes = False

    def __init__(self, sheet_id: str, key_file: Path, *, write: bool = False) -> None:
        FakeSheet.write_flags.append(write)

    def tabs(self) -> dict[str, int]:
        return dict(FakeSheet.tab_ids)

    def values(self, cell_range: str) -> Grid:
        return copy.deepcopy(FakeSheet.grids[FakeSheet.tab_ids[cell_range.split("!")[0]]])

    def batch_update(self, requests: list[sheet.Request]) -> None:
        if FakeSheet.fail_writes:
            raise sheet.SheetError("applying changes: HTTP 500 INTERNAL")
        FakeSheet.batches.append(requests)
        for request in requests:
            _apply(request)


def _apply(request: dict[str, Any]) -> None:
    if "addSheet" in request:
        properties = request["addSheet"]["properties"]
        FakeSheet.tab_ids[properties["title"]] = properties["sheetId"]
        FakeSheet.grids[properties["sheetId"]] = []
    elif "updateSheetProperties" in request:
        properties = request["updateSheetProperties"]["properties"]
        grid = FakeSheet.grids[properties["sheetId"]]
        del grid[properties["gridProperties"]["rowCount"] :]
    elif "updateCells" in request:
        update = request["updateCells"]
        grid = FakeSheet.grids[update["range"]["sheetId"]]
        top, left = update["range"]["startRowIndex"], update["range"]["startColumnIndex"]
        for i, row in enumerate(update["rows"]):
            for j, cell in enumerate(row["values"]):
                value = cell.get("userEnteredValue", {})
                new: object = value.get("stringValue", value.get("numberValue", ""))
                if isinstance(new, float) and new.is_integer():
                    new = int(new)  # the API returns whole numbers without ".0"
                while len(grid) <= top + i:
                    grid.append([])
                while len(grid[top + i]) <= left + j:
                    grid[top + i].append("")
                grid[top + i][left + j] = new


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(sheet, "GoogleSheet", FakeSheet)
    FakeSheet.tab_ids = {"Budget": BUDGET_ID}
    FakeSheet.grids = {BUDGET_ID: budget_grid()}
    FakeSheet.batches, FakeSheet.write_flags, FakeSheet.fail_writes = [], [], False
    return data


@pytest.fixture
def ready(data: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    # The BofA fixture is March 2026: groceries 42.10 and 500.00 into the Roth IRA.
    assert main(["import", str(FIXTURES / "bofa_checking.csv")]) == 0
    (data / "config.toml").write_text('sheet_id = "test-sheet"\n')
    (data / "rules.toml").write_text(RULES)
    capsys.readouterr()
    return data


def sync(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["sync", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def budget_cell(cell: str) -> object:
    column, row = ord(cell[0]) - ord("A"), int(cell[1:]) - 1
    return FakeSheet.grids[BUDGET_ID][row][column]


def tab() -> Grid:
    return FakeSheet.grids[FakeSheet.tab_ids[transactions_tab.TAB]]


def test_dry_run_reads_only(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = sync(capsys, "--dry-run")
    assert code == 0
    assert "Dry run: nothing will be changed. Sheet: test-sheet" in out
    assert "C6   Groceries · Mar 2026" in out
    assert "Transactions tab: create with 13 rows (11 uncategorized)" in out
    assert FakeSheet.write_flags == [False]
    assert FakeSheet.batches == []


def test_sync_writes_budget_and_transactions_together(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = sync(capsys)
    assert code == 0
    assert FakeSheet.write_flags == [True]
    assert len(FakeSheet.batches) == 1
    assert (budget_cell("C5"), budget_cell("C6")) == (0, 42.1)
    assert "Updated 2 Budget cells and wrote 13 rows to the Transactions tab." in out

    rows = tab()
    assert rows[0] == transactions_tab.HEADER
    assert len(rows) == 14
    assert rows[1][1:5] == ["BofA checking", "REMITLY INC", -75.25, "Uncategorized"]
    groceries = next(r for r in rows if r[2] == "MAPLE MARKET SPRINGFIELD IL")
    assert groceries[4] == "Groceries"
    conn = store.connect(ready / "tally.db")
    try:
        assert len(store.sheet_categories(conn, "test-sheet")) == 13
        assert len(store.sheet_writes(conn, "test-sheet")) == 3
    finally:
        conn.close()


def test_second_sync_rewrites_tab_without_complaint(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Budget changes: 0 cells (3 already up to date)" in out
    assert "Transactions tab: rewrite with 13 rows" in out
    assert len(tab()) == 14


def test_rule_change_updates_tab_and_budget(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    (ready / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))
    assert sync(capsys)[0] == 0
    assert budget_cell("C6") == 0
    groceries = next(r for r in tab() if r[2] == "MAPLE MARKET SPRINGFIELD IL")
    assert groceries[4] == "Uncategorized"


def test_edited_category_becomes_a_rule(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    row = next(r for r in tab() if r[2] == "Zelle payment to SAM PLACEHOLDER")
    row[4] = "Groceries"

    code, out, _ = sync(capsys, "--dry-run")
    assert code == 0
    assert "Corrections (not saved, dry run):" in out
    assert "Added rule: Zelle payment to SAM PLACEHOLDER (03/11/2026, -30.00) → Groceries" in out
    assert "C6   Groceries · Mar 2026  42.1 → 72.10" in out
    assert "[[rule]]  # corrected" not in (ready / "rules.toml").read_text()

    code, out, _ = sync(capsys)
    assert code == 0
    assert "(saved to rules.toml)" in out
    assert budget_cell("C6") == 72.1
    text = (ready / "rules.toml").read_text()
    assert 'merchant = "Zelle payment to SAM PLACEHOLDER"' in text
    assert text.index("Corrections from the Sheet") < text.index('match = "MAPLE MARKET"')

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Corrections" not in out


def test_clearing_a_correction_undoes_it(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    next(r for r in tab() if r[2] == "VENMO")[4] = "Groceries"
    assert sync(capsys)[0] == 0
    venmo = next(r for r in tab() if r[2] == "VENMO" and r[4] == "Groceries")
    venmo[4] = ""
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Removed correction: VENMO" in out
    assert "Groceries" not in [r[4] for r in tab() if r[2] == "VENMO"]
    assert "[[rule]]  # corrected" not in (ready / "rules.toml").read_text()


def test_correction_blocked_by_budget_conflict_saves_nothing(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    next(r for r in tab() if r[2] == "VENMO")[4] = "Groceries"
    FakeSheet.grids[BUDGET_ID][4][2] = 99  # Rent, Mar: typed by hand
    rules_before = (ready / "rules.toml").read_text()
    assert sync(capsys)[0] == 1
    assert (ready / "rules.toml").read_text() == rules_before


def test_sorting_the_tab_is_not_an_edit(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    rows = tab()
    rows[1:] = sorted(rows[1:], key=lambda r: str(r[2]))
    assert sync(capsys)[0] == 0


def test_row_added_by_hand_stops_the_sync(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    tab().append([46100, "Cash", "LEMONADE", -3, "Groceries", ""])
    code, out, _ = sync(capsys)
    assert code == 1
    assert "row 15: LEMONADE" in out
    assert "tally didn't write" in out


def test_rearranged_tab_is_refused(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    tab()[0][2], tab()[0][3] = "Amount", "Merchant"
    code, _, err = sync(capsys)
    assert code == 1
    assert "the header row isn't" in err


def test_existing_tab_tally_did_not_write_is_refused(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeSheet.tab_ids[transactions_tab.TAB] = 222
    FakeSheet.grids[222] = [list(transactions_tab.HEADER), [46100, "x", "MINE", 1, "Groceries", 7]]
    code, out, _ = sync(capsys, "--dry-run")
    assert code == 1
    assert "row 2: MINE" in out


def save_alerts(data: Path, *alerts: tuple[str, int, str, str]) -> None:
    """Store alerts given as (account, day in March 2026, merchant, amount)."""
    conn = store.connect(data / "tally.db")
    try:
        for n, (account, day, merchant, amount) in enumerate(alerts):
            store.save_alert(
                conn,
                Alert(
                    account=account,
                    date=datetime.date(2026, 3, day),
                    merchant=merchant,
                    amount=Decimal(amount),
                    message_id=f"<alert{n}@example.com>",
                    received=datetime.datetime(2026, 3, day, 12, tzinfo=datetime.UTC),
                ),
            )
    finally:
        conn.close()


@pytest.fixture
def alerted(ready: Path) -> Path:
    save_alerts(
        ready,
        ("discover", 12, "MAPLE MARKET #0412", "-12.34"),  # no Discover CSV yet: pending
        ("bofa_checking", 13, "REMITLY INC", "-75.25"),  # posted in the BofA CSV
        ("bofa_checking", 5, "CORNER CAFE", "-9.99"),  # the CSV reaches it but hasn't it
    )
    return ready


def test_pending_alerts_count_until_they_post(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Transactions tab: create with 14 rows (1 pending, 11 uncategorized)" in out
    assert budget_cell("C6") == 54.44  # 42.10 posted + 12.34 pending
    pending_rows = [r for r in tab() if r[1] == "Discover (pending)"]
    assert [r[2:5] for r in pending_rows] == [["MAPLE MARKET #0412", -12.34, "Groceries"]]
    assert isinstance(pending_rows[0][5], int) and pending_rows[0][5] < 0
    assert "CORNER CAFE" not in [r[2] for r in tab()]

    assert main(["import", str(FIXTURES / "discover_recent.csv")]) == 0
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Transactions tab: rewrite with 22 rows (" in out
    assert "pending," not in out
    assert budget_cell("C6") == 54.44  # the posted row took the alert's place
    assert "Discover (pending)" not in [r[1] for r in tab()]


def pending_row() -> list[object]:
    return next(r for r in tab() if r[1] == "Discover (pending)")


def test_correcting_a_pending_row(alerted: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    pending_row()[4] = "Rent + fees"
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Added rule: MAPLE MARKET #0412 (03/12/2026, -12.34) → Rent + fees" in out
    assert (budget_cell("C5"), budget_cell("C6")) == (12.34, 42.1)
    assert pending_row()[4] == "Rent + fees"
    assert 'merchant = "MAPLE MARKET #0412"' in (alerted / "rules.toml").read_text()


def test_correction_moves_to_the_posted_row(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    pending_row()[4] = "Rent + fees"
    assert sync(capsys)[0] == 0
    assert main(["import", str(FIXTURES / "discover_recent.csv")]) == 0
    capsys.readouterr()

    # Before the next sync, report already counts the posted row as corrected...
    rules_before = (alerted / "rules.toml").read_text()
    assert main(["report", "2026-03"]) == 0
    assert "  Rent + fees                 $12.34" in capsys.readouterr().out
    assert (alerted / "rules.toml").read_text() == rules_before

    # ...and sync saves the move.
    code, out, _ = sync(capsys)
    assert code == 0
    assert (
        "Posted, correction carried over: MAPLE MARKET #0412 SPRINGFIELD IL (03/12/2026, -12.34)"
        " → Rent + fees" in out
    )
    text = (alerted / "rules.toml").read_text()
    assert 'merchant = "MAPLE MARKET #0412"\n' not in text
    assert 'merchant = "MAPLE MARKET #0412 SPRINGFIELD IL"' in text
    assert "# carried over from its alert" in text
    posted = next(r for r in tab() if r[2] == "MAPLE MARKET #0412 SPRINGFIELD IL")
    assert posted[4] == "Rent + fees"
    assert budget_cell("C5") == 12.34

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Corrections" not in out


def test_pending_row_that_posted_while_you_edited(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    pending_row()[4] = "Rent + fees"
    assert main(["import", str(FIXTURES / "discover_recent.csv")]) == 0
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Added rule: MAPLE MARKET #0412 (03/12/2026, -12.34) → Rent + fees" in out
    assert "Posted, correction carried over: MAPLE MARKET #0412 SPRINGFIELD IL" in out
    assert budget_cell("C5") == 12.34


def test_report_includes_pending_alerts(alerted: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", "2026-03"]) == 0
    out = capsys.readouterr().out
    assert "  Groceries                   $54.44" in out
    assert "Includes 1 pending card alert(s) not yet in a bank export" in out
    assert main(["report", "2026-04"]) == 0
    assert "pending card alert" not in capsys.readouterr().out


def test_budget_conflict_blocks_everything(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    FakeSheet.grids[BUDGET_ID][5][2] = 12.34  # Groceries, Mar: typed by hand
    code, out, _ = sync(capsys)
    assert code == 1
    assert "✗ not written by tally" in out
    assert FakeSheet.batches == []
    assert transactions_tab.TAB not in FakeSheet.tab_ids


def test_planned_roth_amount_is_replaced(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    FakeSheet.grids[BUDGET_ID][7][2] = 300
    assert sync(capsys)[0] == 0
    assert budget_cell("C8") == 500


def test_missing_layout_blocks_writing(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    FakeSheet.grids[BUDGET_ID][2][2] = "Not a month"
    assert sync(capsys, "--dry-run")[0] == 0
    code, out, err = sync(capsys)
    assert code == 1
    assert "column 'Mar 2026'" in out
    assert "Nothing written" in err
    assert FakeSheet.batches == []


def test_failed_write_records_nothing(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    FakeSheet.fail_writes = True
    code, _, err = sync(capsys)
    assert code == 1
    assert "HTTP 500 INTERNAL. Nothing was written." in err
    conn = store.connect(ready / "tally.db")
    try:
        assert store.sheet_writes(conn, "test-sheet") == {}
        assert store.sheet_categories(conn, "test-sheet") == {}
    finally:
        conn.close()
    FakeSheet.fail_writes = False
    assert sync(capsys)[0] == 0


def test_missing_budget_tab(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    FakeSheet.tab_ids = {}
    code, _, err = sync(capsys, "--dry-run")
    assert code == 1
    assert "no 'Budget' tab" in err


def test_sync_without_config(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ready / "config.toml").unlink()
    code, _, err = sync(capsys, "--dry-run")
    assert code == 1
    assert "sync: no config file at" in err


def test_sync_without_key_file(
    ready: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.undo()
    monkeypatch.setenv("TALLY_DATA_DIR", str(ready))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(ready.parent / "secrets"))
    code, _, err = sync(capsys, "--dry-run")
    assert code == 1
    assert "sync: no service account key at" in err


def test_number_cell() -> None:
    assert sheet.number_cell(7, "AB12", Decimal("42.10")) == {
        "updateCells": {
            "range": {
                "sheetId": 7,
                "startRowIndex": 11,
                "endRowIndex": 12,
                "startColumnIndex": 27,
                "endColumnIndex": 28,
            },
            "rows": [{"values": [{"userEnteredValue": {"numberValue": 42.1}}]}],
            "fields": "userEnteredValue",
        }
    }


def test_summary_command(alerted: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (alerted / "config.toml").write_text('sheet_id = "test-sheet"\nroth_limit = 1000\n')
    assert main(["summary", "--as-of", "2026-03-15"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("tally: week of Mar 9 – Mar 15, 2026\n")
    assert "MAPLE MARKET #0412" in out and "(pending)" in out
    assert "  Groceries       $54.44 of   $300.00    18%" in out
    assert "Not in the bank export: Mar 5  CORNER CAFE  $9.99." in out
    assert "Roth IRA 2026: $500.00 in so far, $400.00 more planned." in out
    assert FakeSheet.write_flags == [False]


def test_summary_without_the_sheet(
    alerted: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreachable(self: FakeSheet, cell_range: str) -> Grid:
        raise sheet.SheetError("reading Budget!A1:Z100: HTTP 503")

    monkeypatch.setattr(FakeSheet, "values", unreachable)
    assert main(["summary", "--as-of", "2026-03-15"]) == 0
    out = capsys.readouterr().out
    assert "(Couldn't read the Budget tab, so there's no plan to compare with:" in out
    assert "  Groceries       $54.44\n" in out


def test_summary_rejects_a_bad_date() -> None:
    with pytest.raises(SystemExit):
        main(["summary", "--as-of", "last sunday"])


@pytest.fixture
def mail_ready(alerted: Path, monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    (alerted / "config.toml").write_text(
        'sheet_id = "test-sheet"\n'
        'gmail_account = "alerts@example.com"\n'
        'summary_to = "me@example.com"\n'
    )
    gmail.save_password(alerted.parent / "secrets" / "gmail-app-password", "abcdefghijklmnop")
    FakeSMTP.instances, FakeSMTP.fail_login = [], False
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_summary_email(mail_ready: type[FakeSMTP], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", "--as-of", "2026-03-15", "--email"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("Emailed to me@example.com.")
    [conn] = mail_ready.instances
    [msg] = conn.sent
    assert (msg["From"], msg["To"]) == ("alerts@example.com", "me@example.com")
    assert msg["Subject"] == "tally: week of Mar 9 – Mar 15, 2026"
    body = msg.get_body(("plain",)).get_content()
    assert body.startswith("tally: week of Mar 9 – Mar 15, 2026\n")
    assert "MAPLE MARKET #0412" in body
    assert body.endswith("Budget: https://docs.google.com/spreadsheets/d/test-sheet\n")
    html = msg.get_body(("html",)).get_content()
    assert "MAPLE MARKET #0412" in html and ">pending</span>" in html
    assert 'href="https://docs.google.com/spreadsheets/d/test-sheet"' in html


def test_summary_without_email_sends_nothing(mail_ready: type[FakeSMTP]) -> None:
    assert main(["summary", "--as-of", "2026-03-15"]) == 0
    assert mail_ready.instances == []


def test_summary_email_needs_both_addresses(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (alerted / "config.toml").write_text('sheet_id = "test-sheet"\nsummary_to = "me@example.com"\n')
    assert main(["summary", "--email"]) == 1
    assert "needs gmail_account (the sender) and summary_to" in capsys.readouterr().err


def test_summary_email_failure(
    mail_ready: type[FakeSMTP], capsys: pytest.CaptureFixture[str]
) -> None:
    mail_ready.fail_login = True
    assert main(["summary", "--as-of", "2026-03-15", "--email"]) == 1
    captured = capsys.readouterr()
    assert "tally: week of" in captured.out  # still printed
    assert "summary: not emailed: Gmail refused the app password" in captured.err
    assert "abcdefghijklmnop" not in captured.err


def test_summary_syncs_first(alerted: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", "--as-of", "2026-03-15", "--sync"]) == 0
    out = capsys.readouterr().out
    assert len(FakeSheet.batches) == 1
    assert budget_cell("C6") == 54.44
    assert out.startswith("tally: week of")  # the sync's own output isn't shown
    assert "The Sheet wasn't updated" not in out


def test_summary_says_when_the_sync_was_blocked(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeSheet.grids[BUDGET_ID][5][2] = 12.34  # Groceries, Mar: typed by hand
    assert main(["summary", "--as-of", "2026-03-15", "--sync"]) == 0
    out = capsys.readouterr().out
    assert FakeSheet.batches == []
    assert (
        "  - The Sheet wasn't updated before this summary: 1 cell(s) marked ✗ hold a value tally "
        "didn't write, probably one you typed. Run `tally sync` to see the details." in out
    )


def test_summary_names_a_row_added_by_hand(
    alerted: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    tab().append([46100, "Cash", "LEMONADE", -3, "Groceries", ""])
    assert main(["summary", "--as-of", "2026-03-15", "--sync"]) == 0
    assert (
        "The Sheet wasn't updated before this summary: Transactions tab: 1 row(s) tally didn't "
        "write. Run `tally sync`" in capsys.readouterr().out
    )


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Claude puts Remitly under Rent + fees and can't tell the rest; returns its questions."""
    questions: list[str] = []

    def answer(question: str, schema: dict[str, Any], token: str | None) -> Any:
        questions.append(question)
        return {"answers": [{"merchant": "REMITLY INC", "category": "Rent + fees"}]}

    monkeypatch.setattr(categorizing, "ask_claude", answer)
    return questions


def test_claude_categorizes_during_sync(
    ready: Path, claude: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = sync(capsys, "--dry-run")
    assert code == 0
    assert "Categorized by Claude (not saved, dry run):\n  REMITLY INC → Rent + fees" in out
    assert "by Claude" not in (ready / "rules.toml").read_text()

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Categorized by Claude (saved to rules.toml):" in out
    assert next(r for r in tab() if r[2] == "REMITLY INC")[4] == "Rent + fees"
    assert budget_cell("C5") == 75.25
    assert 'merchant = "REMITLY INC"' in (ready / "rules.toml").read_text()

    # Only the merchants still uncategorized are asked about again.
    sync(capsys)
    assert "REMITLY INC" in claude[1] and "REMITLY INC" not in claude[2]


def test_correcting_claudes_category_on_the_sheet(
    ready: Path, claude: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync(capsys)[0] == 0
    next(r for r in tab() if r[2] == "REMITLY INC")[4] = "Groceries"
    code, out, _ = sync(capsys)
    assert code == 0
    assert "Changed Claude's rule for REMITLY INC to Groceries" in out
    text = (ready / "rules.toml").read_text()
    assert 'category = "Rent + fees"' not in text
    assert "corrected on the Sheet" not in text


def test_sync_goes_on_without_claude(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = sync(capsys)
    assert code == 0
    assert "sync: Claude didn't categorize: not asked in tests" in err
    assert "Updated 2 Budget cells" in out


def test_summary_carries_claudes_note(
    ready: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def answer(question: str, schema: dict[str, Any], token: str | None, **_: Any) -> Any:
        return {"note": "That charge looks like a hold."}

    monkeypatch.setattr(categorizing, "ask_claude", answer)
    assert main(["summary"]) == 0
    assert "CLAUDE'S NOTE\n  That charge looks like a hold." in capsys.readouterr().out


def test_summary_goes_out_without_claudes_note(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["summary"]) == 0
    out = capsys.readouterr().out
    assert "CLAUDE'S NOTE" not in out
    assert "No note from Claude: not asked in tests" in out


def test_past_summary_asks_claude_nothing(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", "--as-of", "2026-03-15"]) == 0
    assert "No note from Claude" not in capsys.readouterr().out
