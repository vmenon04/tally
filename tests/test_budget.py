from decimal import Decimal

import pytest

from tally import budget
from tally.report import MonthReport
from tally.sheet import Grid

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


def test_this_months_roth_plan_stays_until_a_contribution() -> None:
    assert "C8" not in [c.cell for c in plan([month(2026, 3)]).changes]
    past = budget.plan(grid(), [month(2026, 3)], CATEGORIES, (2026, 4))
    assert {c.cell: c.new for c in past.changes}["C8"] == Decimal(0)


def test_read_targets() -> None:
    g = grid()
    g.append([budget.PAYCHECKS_ROW, "", 2, 3])
    targets = budget.read_targets(g)
    assert targets.monthly == {"Rent + fees": Decimal(800), "Groceries": Decimal(300)}
    assert targets.for_month("Groceries", 2026, 3) == Decimal("12.34")
    assert targets.for_month(budget.ROTH_ROW, 2026, 4) == Decimal(400)
    assert targets.for_month(budget.PAYCHECKS_ROW, 2026, 4) == Decimal(3)
    assert targets.for_month("Rent + fees", 2026, 3) is None  # empty
    assert "Total spending" not in targets.monthly  # formulas are skipped


def test_zero_spending_is_written() -> None:
    changes = plan([month(2026, 4)]).changes
    assert {c.cell: c.new for c in changes} == {"D5": Decimal(0), "D6": Decimal(0)}


def test_missing_month_column_and_row_are_reported() -> None:
    reports = [month(2026, 2, rent="1"), month(2026, 3, rent="2", groceries="12.34")]
    result = budget.plan(grid(), reports, (*CATEGORIES, "Family support"), (2026, 3))
    assert result.missing_months == ["Feb 2026"]
    assert result.missing_rows == ["Family support"]
    assert [c.cell for c in result.changes] == ["C5"]  # no Roth contribution yet in March


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
