"""A month's spending per category, the way the Budget tab counts it.

Transactions belong to the month they happened in (their date, not the posting date).
Spending is shown as a positive number, so a refund or a friend's payback lowers it.
Card payments are left out entirely: each purchase already counts on the card.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from tally.rules import UNCATEGORIZED, Rules
from tally.transaction import Transaction


@dataclass(frozen=True, slots=True)
class MonthReport:
    year: int
    month: int
    spending: dict[str, Decimal]  # every spending category, in Budget tab order
    income: Decimal
    savings: Decimal
    investing: Decimal
    investing_year_to_date: Decimal
    uncategorized: list[Transaction]

    @property
    def total_spending(self) -> Decimal:
        return sum(self.spending.values(), Decimal(0))


def build(transactions: list[Transaction], rules: Rules, year: int, month: int) -> MonthReport:
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    investing_year_to_date = Decimal(0)
    uncategorized = []
    for t in transactions:
        if t.date.year != year or t.date.month > month:
            continue
        category = rules.categorize(t)
        if category == "Investing":
            investing_year_to_date -= t.amount
        if t.date.month != month:
            continue
        totals[category] += t.amount
        if category == UNCATEGORIZED:
            uncategorized.append(t)

    return MonthReport(
        year=year,
        month=month,
        spending={name: Decimal(0) - totals[name] for name in rules.categories},
        income=totals["Income"],
        savings=Decimal(0) - totals["Savings transfer"],
        investing=Decimal(0) - totals["Investing"],
        investing_year_to_date=investing_year_to_date,
        uncategorized=sorted(uncategorized, key=lambda t: t.date),
    )


def format_report(report: MonthReport) -> str:
    width = max(len(name) for name in [*report.spending, "Investing (Roth IRA)"]) + 2
    title = f"{calendar.month_name[report.month]} {report.year}"

    def line(label: str, amount: Decimal, note: str = "") -> str:
        return f"  {label:<{width}}{_money(amount):>12}{note}".rstrip()

    lines = [title, "", "Spending"]
    lines += [line(name, amount) for name, amount in report.spending.items()]
    lines += [line("Total", report.total_spending), "", "Not spending"]
    lines.append(line("Income", report.income))
    lines.append(
        line(
            "Investing (Roth IRA)",
            report.investing,
            f"   {report.year} so far: {_money(report.investing_year_to_date)}",
        )
    )
    lines.append(line("Savings transfers", report.savings))
    lines += ["", f"Uncategorized ({len(report.uncategorized)})"]
    if not report.uncategorized:
        lines.append("  none")
    for t in report.uncategorized:
        lines.append(f"  {t.date}  {t.account:<13}  {_money(t.amount):>11}  {t.merchant}")
    return "\n".join(lines)


def _money(amount: Decimal) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
