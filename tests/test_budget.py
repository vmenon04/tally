from decimal import Decimal
from pathlib import Path

import pytest

from tally import budget, sheet
from tally.cli import main
from tally.report import MonthReport
from tally.sheet import Grid

FIXTURES = Path(__file__).parent / "fixtures"
CATEGORIES = ("Rent + fees", "Groceries")


def grid() -> Grid:
    """A small Budget tab laid out like the real one."""
    return [
        ["Budget: plan vs. actual"],
        [],
        ["", "Plan / mo", "Mar 2026", "Apr 2026", "Avg actual", "What it covers"],
        ["SPENDING"],
        ["Rent + fees", 800, "", "", '=IF(COUNT(C5:D5)=0,"",AVERAGE(C5:D5))', "note"],
        ["Groceries", 300, 12.34],
        ["Total spending", "=SUM(B5:B6)", "=SUM(C5:C6)", "=SUM(D5:D6)"],
        [budget.ROTH_ROW, "", 500, 400],
    ]


def month(
    year: int, number: int, rent: str = "0", groceries: str = "0", investing: str = "0"
) -> MonthReport:
    return MonthReport(
        year=year,
        month=number,
        spending={"Rent + fees": Decimal(rent), "Groceries": Decimal(groceries)},
        income=Decimal(0),
        savings=Decimal(0),
        investing=Decimal(investing),
        investing_year_to_date=Decimal(investing),
        uncategorized=[],
    )


def plan(reports: list[MonthReport], g: Grid | None = None) -> budget.BudgetPlan:
    return budget.plan(g if g is not None else grid(), reports, CATEGORIES, (2026, 3))


def test_fills_empty_cells_and_skips_matching_ones() -> None:
    result = plan([month(2026, 3, rent="1500.00", groceries="12.34", investing="500")])
    assert result.changes == [
        budget.CellChange("C5", "Rent + fees", "Mar 2026", "", Decimal("1500.00")),
    ]
    assert len(result.unchanged) == 2


def test_replaces_planned_roth_amount_with_actual() -> None:
    [change] = plan([month(2026, 3, groceries="12.34", investing="650.00")]).changes[1:]
    assert change == budget.CellChange("C8", budget.ROTH_ROW, "Mar 2026", 500, Decimal("650.00"))


def test_roth_plan_for_future_months_is_left_alone() -> None:
    result = plan([month(2026, 4, investing="0")])
    assert [c.cell for c in result.changes] == ["D5", "D6"]


def test_zero_spending_is_written() -> None:
    changes = plan([month(2026, 4)]).changes
    assert {c.cell: c.new for c in changes} == {"D5": Decimal(0), "D6": Decimal(0)}


def test_missing_month_column_and_row_are_reported() -> None:
    reports = [month(2026, 2, rent="1"), month(2026, 3, rent="2", groceries="12.34")]
    result = budget.plan(grid(), reports, (*CATEGORIES, "Family support"), (2026, 3))
    assert result.missing_months == ["Feb 2026"]
    assert result.missing_rows == ["Family support"]
    assert [c.cell for c in result.changes] == ["C5", "C8"]


def test_formula_in_a_target_cell_is_refused() -> None:
    g = grid()
    g[4][2] = "=1+1"
    with pytest.raises(budget.BudgetError, match=r"C5 \(Rent \+ fees, Mar 2026\) is a formula"):
        plan([month(2026, 3)], g)


def test_text_in_a_target_cell_counts_as_a_change() -> None:
    g = grid()
    g[4][2] = "n/a"
    [change] = [c for c in plan([month(2026, 3, rent="5")], g).changes if c.cell == "C5"]
    assert change.old == "n/a"


def test_missing_header_row_is_refused() -> None:
    g = grid()
    g[2][1] = "Plan"
    with pytest.raises(budget.BudgetError, match="no header row"):
        plan([month(2026, 3)], g)


def test_duplicate_label_is_refused() -> None:
    g = [*grid(), ["Groceries", 1]]
    with pytest.raises(budget.BudgetError, match="'Groceries' appears in column A more than once"):
        plan([month(2026, 3)], g)


def test_format_plan() -> None:
    reports = [month(2026, 2), month(2026, 3, rent="1500", groceries="12.34", investing="650")]
    text = budget.format_plan(budget.plan(grid(), reports, CATEGORIES, (2026, 3)))
    assert text == "\n".join(
        [
            "Missing from the Budget tab (needs a one-time setup):",
            "  column 'Feb 2026'",
            "",
            "Budget changes: 2 cells (1 already up to date)",
            "  C5   Rent + fees · Mar 2026                            (empty) → 1500.00",
            "  C8   Roth IRA contribution (from checking) · Mar 2026  500 → 650.00",
        ]
    )


def test_column_letters() -> None:
    assert [budget._column_letter(i) for i in (0, 2, 25, 26, 27, 51, 52)] == [
        "A",
        "C",
        "Z",
        "AA",
        "AB",
        "AZ",
        "BA",
    ]


def test_conflict_rules() -> None:
    g = grid()
    g[4][2] = 99  # Rent, Mar: typed by hand
    g[4][3] = 10  # Rent, Apr: tally wrote 10 earlier
    reports = [month(2026, 3, rent="1", investing="650"), month(2026, 4, rent="2", groceries="3")]
    result = budget.plan(g, reports, CATEGORIES, (2026, 4))
    written = {("Rent + fees", "Apr 2026"): Decimal("10.00")}
    conflicts = budget.find_conflicts(result, written)
    # Typed by hand: Rent Mar (99) and Groceries Mar (12.34). Fine: Rent Apr (tally's own 10),
    # Groceries Apr (empty) and the Roth row (plan amounts tally hasn't replaced yet).
    assert [c.cell for c in conflicts] == ["C5", "C6"]


def test_roth_is_protected_once_tally_has_written_it() -> None:
    result = plan([month(2026, 3, groceries="12.34", investing="650")])
    written = {(budget.ROTH_ROW, "Mar 2026"): Decimal("600")}
    assert [c.cell for c in budget.find_conflicts(result, written)] == ["C8"]


def test_format_marks_conflicts() -> None:
    g = grid()
    g[4][2] = 99
    result = budget.plan(g, [month(2026, 3, rent="1", groceries="12.34")], CATEGORIES, (2026, 2))
    text = budget.format_plan(result, budget.find_conflicts(result, {}))
    assert "C5   Rent + fees · Mar 2026  99 → 1.00   ✗ not written by tally" in text
    assert "1 cell(s) marked ✗ hold a value tally didn't write" in text


class FakeSheet:
    """Stands in for GoogleSheet: one shared grid, and a log of what was asked of it."""

    grid: Grid = []
    updates: list[dict[str, Decimal]] = []
    write_flags: list[bool] = []
    fail_writes = False

    def __init__(self, sheet_id: str, key_file: Path, *, write: bool = False) -> None:
        FakeSheet.write_flags.append(write)

    def values(self, cell_range: str) -> Grid:
        assert cell_range == budget.RANGE
        return [list(row) for row in FakeSheet.grid]

    def update(self, cells: dict[str, Decimal]) -> None:
        if FakeSheet.fail_writes:
            raise sheet.SheetError("writing: HTTP 500 INTERNAL")
        FakeSheet.updates.append(cells)
        for address, value in cells.items():
            cell = address.removeprefix(f"{budget.TAB}!")
            column, row = ord(cell[0]) - ord("A"), int(cell[1:]) - 1
            FakeSheet.grid[row][column] = float(value)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(sheet, "GoogleSheet", FakeSheet)
    FakeSheet.grid = [row + [""] * (6 - len(row)) for row in grid()]
    FakeSheet.grid[5][2] = ""  # Groceries, Mar: start empty
    FakeSheet.updates, FakeSheet.write_flags, FakeSheet.fail_writes = [], [], False
    return data


RULES = (
    'categories = ["Rent + fees", "Groceries"]\n'
    '[[rule]]\nmatch = "MAPLE MARKET"\ncategory = "Groceries"\n'
    '[[rule]]\nmerchant = "FID BKG SVC LLC"\ncategory = "Investing"\n'
)


def set_up(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The BofA fixture is March 2026: groceries 42.10 and 500.00 into the Roth IRA.
    assert main(["import", str(FIXTURES / "bofa_checking.csv")]) == 0
    (data / "config.toml").write_text('sheet_id = "test-sheet"\n')
    (data / "rules.toml").write_text(RULES)
    capsys.readouterr()


def sync(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["sync", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_dry_run_reads_only(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    code, out, _ = sync(capsys, "--dry-run")
    assert code == 0
    assert "Dry run: nothing will be changed. Sheet: test-sheet" in out
    assert "C6   Groceries · Mar 2026" in out
    assert "(empty) → 42.10" in out
    assert FakeSheet.write_flags == [False]
    assert FakeSheet.updates == []


def test_sync_writes_and_remembers(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    code, out, _ = sync(capsys)
    assert code == 0
    assert FakeSheet.write_flags == [True]
    assert FakeSheet.updates == [{"Budget!C5": Decimal("0"), "Budget!C6": Decimal("42.10")}]
    assert "Updated 2 cells." in out

    code, out, _ = sync(capsys)
    assert code == 0
    assert "Budget changes: 0 cells (3 already up to date)" in out
    assert len(FakeSheet.updates) == 1


def test_sync_updates_its_own_values(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    assert sync(capsys)[0] == 0
    (data / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))
    code, _, _ = sync(capsys)
    assert code == 0
    assert FakeSheet.updates[-1] == {"Budget!C6": Decimal("0")}


def test_sync_refuses_to_overwrite_your_typing(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up(data, capsys)
    assert sync(capsys)[0] == 0
    FakeSheet.grid[5][2] = 50  # you typed over tally's 42.10
    code, out, _ = sync(capsys)
    assert code == 1
    assert "C6   Groceries · Mar 2026  50 → 42.10   ✗ not written by tally" in out
    assert len(FakeSheet.updates) == 1


def test_value_typed_before_tally_ever_wrote_is_protected(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up(data, capsys)
    FakeSheet.grid[5][2] = 12.34
    assert sync(capsys, "--dry-run")[0] == 1
    assert sync(capsys)[0] == 1
    assert FakeSheet.updates == []


def test_planned_roth_amount_is_replaced(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    FakeSheet.grid[7][2] = 300
    assert sync(capsys)[0] == 0
    assert FakeSheet.updates[0]["Budget!C8"] == Decimal("500.00")


def test_value_already_right_is_adopted(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    FakeSheet.grid[5][2] = 42.10  # typed, but it's what tally would write
    assert sync(capsys)[0] == 0
    (data / "rules.toml").write_text(RULES.replace("MAPLE MARKET", "NO SUCH SHOP"))
    assert sync(capsys)[0] == 0  # tally now owns that cell, so it may change it
    assert FakeSheet.updates[-1] == {"Budget!C6": Decimal("0")}


def test_missing_layout_blocks_writing(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    FakeSheet.grid[2][2] = "Not a month"
    assert sync(capsys, "--dry-run")[0] == 0
    code, out, err = sync(capsys)
    assert code == 1
    assert "column 'Mar 2026'" in out
    assert "Nothing written" in err
    assert FakeSheet.updates == []


def test_failed_write_records_nothing(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    FakeSheet.fail_writes = True
    code, _, err = sync(capsys)
    assert code == 1
    assert "HTTP 500 INTERNAL. Nothing was written." in err
    FakeSheet.fail_writes = False
    assert sync(capsys)[0] == 0
    assert FakeSheet.updates == [{"Budget!C5": Decimal("0"), "Budget!C6": Decimal("42.10")}]


def test_sync_without_config(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data, capsys)
    (data / "config.toml").unlink()
    code, _, err = sync(capsys, "--dry-run")
    assert code == 1
    assert "sync: no config file at" in err


def test_sync_without_key_file(
    data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up(data, capsys)
    monkeypatch.undo()
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(data.parent / "secrets"))
    code, _, err = sync(capsys, "--dry-run")
    assert code == 1
    assert "sync: no service account key at" in err


def test_update_body() -> None:
    body = sheet.update_body({"Budget!C5": Decimal("0"), "Budget!C6": Decimal("42.10")})
    assert body == {
        "valueInputOption": "RAW",
        "data": [
            {"range": "Budget!C5", "values": [[0.0]]},
            {"range": "Budget!C6", "values": [[42.1]]},
        ],
    }
