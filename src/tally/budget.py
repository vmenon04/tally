"""Working out what tally would write to the Budget tab.

The tab has a header row ("", "Plan / mo", "Oct 2026", "Nov 2026", ...) and one row per
category, found by its label in column A. tally fills in the month cells of the category
rows and of the Roth IRA row. Everything else (plan amounts, notes, formula rows) is yours.
"""

import calendar
import re
from dataclasses import dataclass, field
from decimal import Decimal

from tally.report import MonthReport
from tally.sheet import Grid

TAB = "Budget"
RANGE = f"{TAB}!A1:Z100"
PLAN_HEADER = "Plan / mo"
ROTH_ROW = "Roth IRA contribution (from checking)"

_MONTH_HEADER = re.compile(r"[A-Z][a-z]{2} \d{4}")


class BudgetError(ValueError):
    """The Budget tab isn't laid out the way tally expects."""


@dataclass(frozen=True, slots=True)
class CellChange:
    cell: str  # A1 address, like "C10"
    row: str
    month: str
    old: object  # as the Sheet has it: "" when empty
    new: Decimal


@dataclass(slots=True)
class BudgetPlan:
    changes: list[CellChange] = field(default_factory=list)
    unchanged: int = 0
    missing_rows: list[str] = field(default_factory=list)
    missing_months: list[str] = field(default_factory=list)


def month_header(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def plan(
    grid: Grid,
    reports: list[MonthReport],
    categories: tuple[str, ...],
    roth_through: tuple[int, int],
) -> BudgetPlan:
    """The cell changes that would bring the Budget tab up to date with the reports.

    The Roth IRA row is only filled for months up to roth_through (normally the current
    month), so planned contributions for future months stay as you typed them.
    """
    header_row = next(
        (i for i, row in enumerate(grid) if len(row) > 1 and row[1] == PLAN_HEADER), None
    )
    if header_row is None:
        raise BudgetError(f"no header row with {PLAN_HEADER!r} in column B")
    columns = {
        cell: i
        for i, cell in enumerate(grid[header_row])
        if isinstance(cell, str) and _MONTH_HEADER.fullmatch(cell)
    }

    rows: dict[str, int] = {}
    for i, row in enumerate(grid):
        label = row[0] if row else ""
        if isinstance(label, str) and label:
            if label in rows:
                raise BudgetError(f"label {label!r} appears in column A more than once")
            rows[label] = i

    result = BudgetPlan()
    result.missing_rows = [name for name in (*categories, ROTH_ROW) if name not in rows]
    for report in sorted(reports, key=lambda r: (r.year, r.month)):
        header = month_header(report.year, report.month)
        if header not in columns:
            result.missing_months.append(header)
            continue
        values = dict(report.spending)
        if (report.year, report.month) <= roth_through:
            values[ROTH_ROW] = report.investing
        for name, new in values.items():
            if name not in rows:
                continue
            row_index, column_index = rows[name], columns[header]
            old = _cell(grid, row_index, column_index)
            cell = f"{_column_letter(column_index)}{row_index + 1}"
            if isinstance(old, str) and old.startswith("="):
                raise BudgetError(f"{cell} ({name}, {header}) is a formula; tally won't touch it")
            if _same(old, new):
                result.unchanged += 1
            else:
                result.changes.append(CellChange(cell, name, header, old, new))
    return result


def format_plan(result: BudgetPlan) -> str:
    lines = []
    if result.missing_rows or result.missing_months:
        lines.append("Missing from the Budget tab (needs a one-time setup):")
        lines += [f"  row {name!r}" for name in result.missing_rows]
        lines += [f"  column {header!r}" for header in result.missing_months]
        lines.append("")
    lines.append(
        f"Budget changes: {len(result.changes)} cells ({result.unchanged} already up to date)"
    )
    width = max((len(f"{c.row} · {c.month}") for c in result.changes), default=0)
    for c in result.changes:
        old = "(empty)" if c.old == "" else str(c.old)
        lines.append(f"  {c.cell:<4} {f'{c.row} · {c.month}':<{width}}  {old} → {c.new:.2f}")
    return "\n".join(lines)


def _cell(grid: Grid, row: int, column: int) -> object:
    if row < len(grid) and column < len(grid[row]):
        return grid[row][column]
    return ""


def _same(old: object, new: Decimal) -> bool:
    if isinstance(old, bool) or not isinstance(old, int | float):
        return False
    return Decimal(str(old)) == new


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters
