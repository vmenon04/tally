"""Checking the ledger against tally.db, so the Sheet can be retired with confidence.

Three checks, each of which should find nothing:
- rows: every posted row and waiting alert tally counts has exactly one ledger entry with
  its tally-id, and the ledger has no tally entries tally doesn't know;
- totals: each month's spending per category in the ledger is what the Sheet gets;
- bean-check: the ledger has no errors (failed balances above all).
"""

import calendar
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from beancount.core import data

from tally import report
from tally.ledger import book, entries
from tally.rules import Rules
from tally.transaction import Transaction


@dataclass(frozen=True, slots=True)
class Parity:
    rows: int  # tally's rows the ledger has
    missing: list[Transaction] = field(default_factory=list)  # in tally, not the ledger
    extra: int = 0  # tally entries in the ledger that tally doesn't have
    totals: list[str] = field(default_factory=list)  # months and categories that differ
    errors: list[str] = field(default_factory=list)  # bean-check's

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.totals or self.errors)

    def problems(self) -> list[str]:
        """What's wrong, one sentence each, for the sync output and the weekly summary."""
        found = []
        if self.missing:
            found.append(f"The ledger is missing {len(self.missing)} of tally's transactions.")
        if self.extra:
            found.append(f"The ledger has {self.extra} imported entries tally doesn't have.")
        if self.totals:
            found.append(
                f"The ledger's spending differs from the Sheet's in {len(self.totals)} "
                f"month and category pair(s), first {self.totals[0]}."
            )
        if self.errors:
            found.append(
                f"bean-check finds {len(self.errors)} problem(s) in the ledger, "
                f"first: {self.errors[0]}"
            )
        return found

    def details(self) -> list[str]:
        lines = [f"  missing: {t.date}  {t.amount:>10}  {t.merchant}" for t in self.missing]
        lines += [f"  differs: {line}" for line in self.totals]
        lines += [f"  bean-check: {message}" for message in self.errors]
        return lines


def compare(ledger: book.Ledger, counted: Sequence[Transaction], rules: Rules) -> Parity:
    """counted: the posted rows and waiting alerts, as tally's reports count them."""
    ours = Counter(entries.identity(t) for t in counted)
    theirs = Counter(
        e.meta[entries.ID_KEY]
        for e in ledger.entries
        if isinstance(e, data.Transaction) and entries.ID_KEY in e.meta
    )
    short = ours - theirs
    missing = []
    for t in counted:
        key = entries.identity(t)
        if short[key] > 0:
            short[key] -= 1
            missing.append(t)
    extra = sum((theirs - ours).values())

    spent = _ledger_spending(ledger)
    totals = []
    for year, month in sorted({(t.date.year, t.date.month) for t in counted}):
        sheet = report.build(list(counted), rules, year, month).spending
        for category in rules.categories:
            ours_amount = sheet.get(category, Decimal(0))
            theirs_amount = spent.get((year, month, category), Decimal(0))
            if ours_amount != theirs_amount:
                totals.append(
                    f"{calendar.month_abbr[month]} {year} {category}: "
                    f"Sheet {ours_amount:.2f}, ledger {theirs_amount:.2f}"
                )
    return Parity(sum(theirs.values()) - extra, missing, extra, totals, list(ledger.errors))


def _ledger_spending(ledger: book.Ledger) -> dict[tuple[int, int, str], Decimal]:
    """Spending per (year, month, category), by the day each entry's purchase happened."""
    sources = set(ledger.accounts.sources.values())
    spent: defaultdict[tuple[int, int, str], Decimal] = defaultdict(Decimal)
    for e in ledger.entries:
        if not isinstance(e, data.Transaction) or entries.ID_KEY not in e.meta:
            continue
        day = e.meta.get(entries.PURCHASED_KEY, e.date)
        for p in e.postings:
            category = ledger.accounts.category_of(p.account)
            if p.account in sources or category is None or p.units is None:
                continue
            spent[(day.year, day.month, category)] += p.units.number or Decimal(0)
    return dict(spent)
