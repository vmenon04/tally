"""Your money at a glance, worked out from the ledger: what the Tally page in Fava shows.

- each of the last months: pay in, spending out, what was kept, and the savings rate;
- this month by category, against the plan and against the months before;
- the merchants that got the most this month;
- net worth at the end of each month, holdings at their market price;
- the purchases nobody has given a category yet.

Pay is everything booked to an Income account but Fidelity's (dividends and market
movement aren't pay); a 401(k)'s payroll contributions count, being pay you never saw.
Spending is every Expenses account, in the month the purchase happened. Saved is pay less
spending: whatever didn't go out stayed yours, in whichever account it went to.
"""

import calendar
import datetime
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from beancount.core import data, position, prices

from tally.ledger import accounts, entries
from tally.plan import Targets
from tally.rules import UNCATEGORIZED

MONTHS = 6
TOP_MERCHANTS = 8
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Month:
    start: datetime.date
    pay: Decimal
    spent: Decimal

    @property
    def kept(self) -> Decimal:
        return self.pay - self.spent

    @property
    def rate(self) -> Decimal | None:
        """The share of pay kept, or None without pay."""
        return self.kept / self.pay if self.pay > 0 else None

    @property
    def name(self) -> str:
        return calendar.month_abbr[self.start.month]


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    fixed: bool  # a once-a-month bill
    spent: Decimal  # this month so far
    plan: Decimal | None  # per month
    usual: Decimal  # average of the months before this one that have any spending at all


@dataclass(frozen=True, slots=True)
class Unsorted:
    """A purchase still in Uncategorized."""

    tally_id: str
    date: datetime.date
    payee: str
    amount: Decimal
    pending: bool


@dataclass(frozen=True, slots=True)
class Picture:
    today: datetime.date
    months: list[Month]  # oldest first, this month last
    categories: list[Category]  # biggest first
    merchants: list[tuple[str, Decimal]]  # this month, biggest first
    net_worth: list[tuple[datetime.date, Decimal]]  # each month's end (today for this one)
    unsorted: list[Unsorted]  # newest first
    choices: list[str]  # categories a purchase can be given

    @property
    def this_month(self) -> Month:
        return self.months[-1]

    @property
    def planned(self) -> Decimal:
        return sum((c.plan or _ZERO for c in self.categories), _ZERO)

    @property
    def pace(self) -> Decimal:
        """What the plan allows by today: bills in full, everyday spending in proportion to
        the days gone."""
        days = calendar.monthrange(self.today.year, self.today.month)[1]
        bills = sum((c.plan or _ZERO for c in self.categories if c.fixed), _ZERO)
        everyday = self.planned - bills
        return (bills + everyday * self.today.day / days).quantize(Decimal("0.01"))

    @property
    def pace_spent(self) -> Decimal:
        """Spent this month, bills counted at their plan (paid or not yet)."""
        return sum(
            (max(c.spent, c.plan or _ZERO) if c.fixed else c.spent for c in self.categories),
            _ZERO,
        )


def build(
    loaded: Sequence[data.Directive],
    found: accounts.Accounts,
    targets: Targets,
    today: datetime.date,
    pending_file: Path | None = None,
) -> Picture:
    starts = _month_starts(today, MONTHS)
    pay: dict[datetime.date, Decimal] = defaultdict(Decimal)
    spent: dict[datetime.date, Decimal] = defaultdict(Decimal)
    by_category: dict[tuple[str, datetime.date], Decimal] = defaultdict(Decimal)
    merchants: dict[str, Decimal] = defaultdict(Decimal)
    for e in loaded:
        if not isinstance(e, data.Transaction) or e.date > today + datetime.timedelta(days=31):
            continue
        bought = _purchased(e)
        for p in e.postings:
            if p.units is None or p.units.currency != entries.CURRENCY:
                continue
            number = p.units.number or _ZERO
            if _is_pay(p.account) and e.date <= today:
                pay[_month(e.date)] += -number
            elif p.account.startswith("Expenses:") and bought <= today:
                month = _month(bought)
                spent[month] += number
                name = found.category_of(p.account) or p.account.removeprefix("Expenses:")
                by_category[name, month] += number
                if month == starts[-1]:
                    merchants[e.payee or e.narration or ""] += number
    months = [Month(s, pay[s], spent[s]) for s in starts]
    return Picture(
        today,
        months,
        _categories(by_category, starts, targets, found.fixed),
        sorted(((m, v) for m, v in merchants.items() if v > 0), key=lambda x: -x[1])[
            :TOP_MERCHANTS
        ],
        _net_worth(loaded, starts, today),
        _unsorted(loaded, found, pending_file),
        _choices(found),
    )


def _categories(
    by_category: dict[tuple[str, datetime.date], Decimal],
    starts: list[datetime.date],
    targets: Targets,
    fixed: frozenset[str],
) -> list[Category]:
    this, before = starts[-1], starts[:-1]
    names = {n for n, _ in by_category} | set(targets.monthly)
    found = []
    for name in names:
        earlier = [by_category[name, s] for s in before if (name, s) in by_category]
        usual = sum(earlier, _ZERO) / len(earlier) if earlier else _ZERO
        spent = by_category.get((name, this), _ZERO)
        if spent or usual or targets.monthly.get(name):
            found.append(
                Category(
                    name,
                    name in fixed,
                    spent,
                    targets.monthly.get(name),
                    usual.quantize(Decimal("0.01")),
                )
            )
    return sorted(found, key=lambda c: (-max(c.spent, c.plan or _ZERO), c.name))


def _net_worth(
    loaded: Sequence[data.Directive], starts: list[datetime.date], today: datetime.date
) -> list[tuple[datetime.date, Decimal]]:
    """What everything is worth at each month's end: assets less what's owed, holdings at
    the latest price by then (or what they cost, if there's none)."""
    price_map = prices.build_price_map(list(loaded))  # type: ignore[no-untyped-call]
    days = [_month_end(s) for s in starts[:-1]] + [today]
    held: dict[str, Decimal] = defaultdict(Decimal)  # currency -> units
    cost: dict[str, Decimal] = defaultdict(Decimal)  # currency -> what it cost, in USD
    ordered = sorted((e for e in loaded if isinstance(e, data.Transaction)), key=lambda e: e.date)
    found, i = [], 0
    for day in days:
        while i < len(ordered) and ordered[i].date <= day:
            for p in ordered[i].postings:
                if p.units is None or not p.account.startswith(("Assets:", "Liabilities:")):
                    continue
                number = p.units.number or _ZERO
                held[p.units.currency] += number
                if isinstance(p.cost, position.Cost) and p.cost.number is not None:
                    cost[p.units.currency] += number * p.cost.number
            i += 1
        total = _ZERO
        for currency, units in held.items():
            if currency == entries.CURRENCY:
                total += units
                continue
            _, price = prices.get_price(  # type: ignore[no-untyped-call]
                price_map, (currency, entries.CURRENCY), day
            )
            total += units * price if price is not None else cost[currency]
        found.append((day, total.quantize(Decimal("0.01"))))
    return found


def _unsorted(
    loaded: Sequence[data.Directive], found: accounts.Accounts, pending_file: Path | None
) -> list[Unsorted]:
    sources = set(found.sources.values())
    result = []
    for e in loaded:
        if not isinstance(e, data.Transaction) or entries.ID_KEY not in e.meta:
            continue
        others = [p for p in e.postings if p.account not in sources]
        if len(e.postings) != 2 or len(others) != 1 or others[0].units is None:
            continue
        if found.category_of(others[0].account) != UNCATEGORIZED:
            continue
        filename = Path(str(e.meta.get("filename", "")))
        pending = pending_file is not None and filename.resolve() == pending_file.resolve()
        result.append(
            Unsorted(
                str(e.meta[entries.ID_KEY]),
                _purchased(e),
                e.payee or e.narration or "",
                others[0].units.number or _ZERO,
                pending,
            )
        )
    return sorted(result, key=lambda u: u.date, reverse=True)


def _choices(found: accounts.Accounts) -> list[str]:
    return [
        name
        for name, options in found.categories.items()
        if name != UNCATEGORIZED and any(c.merchant_contains is None for c in options)
    ]


def _is_pay(account: str) -> bool:
    return account.startswith("Income:") and not account.startswith("Income:Fidelity:")


def _purchased(e: data.Transaction) -> datetime.date:
    day = e.meta.get(entries.PURCHASED_KEY)
    return day if isinstance(day, datetime.date) else e.date


def _month(day: datetime.date) -> datetime.date:
    return day.replace(day=1)


def _month_end(start: datetime.date) -> datetime.date:
    return start.replace(day=calendar.monthrange(start.year, start.month)[1])


def _month_starts(today: datetime.date, n: int) -> list[datetime.date]:
    starts = [today.replace(day=1)]
    while len(starts) < n:
        starts.insert(0, (starts[0] - datetime.timedelta(days=1)).replace(day=1))
    return starts
