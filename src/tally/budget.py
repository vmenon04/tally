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
PAYCHECKS_ROW = "Paychecks this month"

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
    unchanged: list[CellChange] = field(default_factory=list)  # already hold the right value
    missing_rows: list[str] = field(default_factory=list)
    missing_months: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Targets:
    """The numbers you typed on the Budget tab, as tally reads them for the weekly summary."""

    monthly: dict[str, Decimal]  # row label -> its "Plan / mo" amount
    months: dict[str, dict[str, Decimal]]  # row label -> month header -> amount

    def for_month(self, label: str, year: int, month: int) -> Decimal | None:
        return self.months.get(label, {}).get(month_header(year, month))


def read_targets(grid: Grid) -> Targets:
    """Every number typed in the plan column and the month columns, by row label.

    Formulas and text are skipped: tally reads the plan, it doesn't evaluate the Sheet.
    """
    header_row = _header_row(grid)
    header = grid[header_row]
    monthly: dict[str, Decimal] = {}
    months: dict[str, dict[str, Decimal]] = {}
    for row in grid[header_row + 1 :]:
        label = row[0] if row else ""
        if not isinstance(label, str) or not label:
            continue
        for i, cell in enumerate(row[1:], start=1):
            amount = _number(cell)
            column = header[i] if i < len(header) else ""
            if amount is None:
                continue
            if column == PLAN_HEADER:
                monthly[label] = amount
            elif isinstance(column, str) and _MONTH_HEADER.fullmatch(column):
                months.setdefault(label, {})[column] = amount
    return Targets(monthly, months)


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
    month), so planned contributions for future months stay as you typed them. In that last
    month it's only filled once a contribution has gone in, so the month's plan stays until then.
    """
    header_row = _header_row(grid)
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
        month = (report.year, report.month)
        if month < roth_through or (month == roth_through and report.investing):
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
                result.unchanged.append(CellChange(cell, name, header, old, new))
            else:
                result.changes.append(CellChange(cell, name, header, old, new))
    return result


def find_conflicts(result: BudgetPlan, written: dict[tuple[str, str], Decimal]) -> list[CellChange]:
    """Changes that would overwrite a value tally didn't write, so probably one you typed.

    A cell may be changed if it is empty, still holds what tally last wrote there, or holds
    a planned Roth IRA amount that tally hasn't replaced yet.
    """
    conflicts = []
    for c in result.changes:
        if c.old == "":
            continue
        last = written.get((c.row, c.month))
        if last is None and c.row == ROTH_ROW:
            continue
        if last is not None and _same(c.old, last):
            continue
        conflicts.append(c)
    return conflicts


def format_plan(result: BudgetPlan, conflicts: list[CellChange] | None = None) -> str:
    conflicts = conflicts or []
    lines = []
    if result.missing_rows or result.missing_months:
        lines.append("Missing from the Budget tab (needs a one-time setup):")
        lines += [f"  row {name!r}" for name in result.missing_rows]
        lines += [f"  column {header!r}" for header in result.missing_months]
        lines.append("")
    lines.append(
        f"Budget changes: {len(result.changes)} cells ({len(result.unchanged)} already up to date)"
    )
    width = max((len(f"{c.row} · {c.month}") for c in result.changes), default=0)
    for c in result.changes:
        old = "(empty)" if c.old == "" else str(c.old)
        mark = "   ✗ not written by tally" if c in conflicts else ""
        lines.append(f"  {c.cell:<4} {f'{c.row} · {c.month}':<{width}}  {old} → {c.new:.2f}{mark}")
    if conflicts:
        lines += [
            "",
            f"{len(conflicts)} cell(s) marked ✗ hold a value tally didn't write, probably one "
            "you typed.",
            "tally won't overwrite them. Clear those cells (or fix the rules) and sync again.",
        ]
    return "\n".join(lines)


def _header_row(grid: Grid) -> int:
    header_row = next(
        (i for i, row in enumerate(grid) if len(row) > 1 and row[1] == PLAN_HEADER), None
    )
    if header_row is None:
        raise BudgetError(f"no header row with {PLAN_HEADER!r} in column B")
    return header_row


def _number(cell: object) -> Decimal | None:
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        return None
    return Decimal(str(cell))


def _cell(grid: Grid, row: int, column: int) -> object:
    if row < len(grid) and column < len(grid[row]):
        return grid[row][column]
    return ""


def _same(old: object, new: Decimal) -> bool:
    return _number(old) == new


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters
