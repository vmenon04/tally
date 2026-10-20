import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tally import budget, sheet, store, transactions_tab
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
    assert "Corrections from the Transactions tab (not saved, dry run):" in out
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


def test_nudge_after_two_corrections(ready: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync(capsys)[0] == 0
    for row in [r for r in tab() if r[2] == "VENMO"]:
        row[4] = "Groceries"
    code, out, _ = sync(capsys)
    assert code == 0
    assert "You've corrected VENMO to Groceries 2 times" in out
    assert '  merchant = "VENMO"\n  category = "Groceries"' in out


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
