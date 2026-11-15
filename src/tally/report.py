"""A month's spending per category, the way the weekly summary counts it.

Transactions belong to the month they happened in (their date, not the posting date).
Spending is shown as a positive number, so a refund or a friend's payback lowers it.
Card payments are left out entirely: each purchase already counts on the card.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from tally.rules import UNCATEGORIZED, Rules
from tally.transaction import Transaction


@dataclass(frozen=True, slots=True)
class MonthReport:
    year: int
    month: int
    spending: dict[str, Decimal]  # every spending category, in the rules' order
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


def money(amount: Decimal) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
