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
    assert result.unchanged == 2


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


class FakeSheet:
    def __init__(self, sheet_id: str, key_file: Path) -> None:
        self.sheet_id = sheet_id

    def values(self, cell_range: str) -> Grid:
        assert cell_range == budget.RANGE
        return grid()


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(sheet, "GoogleSheet", FakeSheet)
    return data


def set_up(data: Path) -> None:
    assert main(["import", str(FIXTURES / "bofa_checking.csv")]) == 0
    (data / "config.toml").write_text('sheet_id = "test-sheet"\n')
    (data / "rules.toml").write_text(
        'categories = ["Rent + fees", "Groceries"]\n'
        '[[rule]]\nmatch = "MAPLE MARKET"\ncategory = "Groceries"\n'
        '[[rule]]\nmerchant = "FID BKG SVC LLC"\ncategory = "Investing"\n'
    )


def test_sync_dry_run(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data)
    capsys.readouterr()
    assert main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing will be changed. Sheet: test-sheet" in out
    assert "C6   Groceries · Mar 2026" in out
    assert "12.34 → 42.10" in out


def test_sync_without_dry_run_is_refused(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data)
    assert main(["sync"]) == 1
    assert "only supports --dry-run" in capsys.readouterr().err


def test_sync_without_config(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_up(data)
    (data / "config.toml").unlink()
    assert main(["sync", "--dry-run"]) == 1
    assert "sync: no config file at" in capsys.readouterr().err


def test_sync_without_key_file(
    data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up(data)
    monkeypatch.undo()
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(data.parent / "secrets"))
    assert main(["sync", "--dry-run"]) == 1
    assert "sync: no service account key at" in capsys.readouterr().err
