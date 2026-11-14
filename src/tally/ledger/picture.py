"""Your money at a glance, worked out from the ledger: what Claude's review and the chat
go by (see advice).

- each of the last months: pay in, spending out, what was kept, and the savings rate;
- this month by category, against the plan and against the months before;
- the merchants that got the most this month;
- net worth at the end of each month, holdings at their market price;
- each goal's balance against its target, and a suggested amount to put in each month;
- each Fidelity account: what it's worth, what was paid for what it holds, and the gain;
- this year's Roth IRA contributions against the limit.

Pay is everything booked to an Income account but Fidelity's (dividends and market
movement aren't pay); a 401(k)'s payroll contributions count, being pay you never saw.
Spending is every Expenses account, in the month the purchase happened. Saved is pay less
spending: whatever didn't go out stayed yours, in whichever account it went to.

The suggestions share out what's usually left of take-home pay (pay less 401(k)
contributions, which are saved already) after spending and the month's planned Roth IRA
transfer, over the last few whole months. A goal with a "months" target that's still short
comes first and gets most of it; the others split the rest. Once it's full, they split it
all.

An investment's gain is what it's worth less what was paid for what it holds (its cost
basis, cash at face value): what the market did with your money. Roth IRA contributions
are the money that came into the Roth's cash from your bank this year, plus what you made
where tally can't see (roth_elsewhere in config.toml).
"""

import calendar
import datetime
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from beancount.core import data, position, prices

from tally.ledger import accounts, entries, positions
from tally.plan import ROTH, Goal, Targets

MONTHS = 6
TOP_MERCHANTS = 8
USUAL_MONTHS = 3  # whole months the suggestions go by
FIRST_SHARE = Decimal("0.7")  # of what's left, for a "months" goal that's still short
ROTH_CASH = "Assets:Fidelity:Roth:Cash"
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Month:
    start: datetime.date
    pay: Decimal
    spent: Decimal
    take_home: Decimal  # pay less 401(k) contributions

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
    account: str  # where it's booked (the first, if several)
    fixed: bool  # a once-a-month bill
    spent: Decimal  # this month so far
    plan: Decimal | None  # per month
    usual: Decimal  # average of the months before this one that have any spending at all


@dataclass(frozen=True, slots=True)
class GoalView:
    name: str
    account: str
    balance: Decimal
    target: Decimal | None
    added: Decimal  # this month, in less out
    suggested: Decimal  # per month
    first: bool  # a "months" goal, which comes first while short

    @property
    def share(self) -> Decimal | None:
        if not self.target:
            return None
        return min(max(self.balance / self.target, _ZERO), Decimal(1))


@dataclass(frozen=True, slots=True)
class Spare:
    """What's usually left each month, from the whole months before this one."""

    months: int
    take_home: Decimal
    spent: Decimal
    roth: Decimal  # planned this month

    @property
    def left(self) -> Decimal:
        return self.take_home - self.spent - self.roth


@dataclass(frozen=True, slots=True)
class Investment:
    """A Fidelity account today."""

    name: str
    account: str
    worth: Decimal  # holdings at the latest price, and cash
    paid: Decimal  # for what it holds: the cost basis, and cash

    @property
    def gain(self) -> Decimal:
        return self.worth - self.paid

    @property
    def share(self) -> Decimal | None:
        """The gain as a share of what was paid."""
        return self.gain / self.paid if self.paid > 0 else None


@dataclass(frozen=True, slots=True)
class RothRoom:
    """This year's Roth IRA contributions against the limit."""

    year: int
    limit: Decimal
    put_in: Decimal

    @property
    def left(self) -> Decimal:
        return self.limit - self.put_in

    @property
    def share(self) -> Decimal:
        return min(self.put_in / self.limit, Decimal(1)) if self.limit > 0 else Decimal(1)

    @property
    def deadline(self) -> datetime.date:
        """The last day to contribute for this year: tax day the year after."""
        return datetime.date(self.year + 1, 4, 15)


@dataclass(frozen=True, slots=True)
class Picture:
    today: datetime.date
    months: list[Month]  # oldest first, this month last
    categories: list[Category]  # biggest first
    merchants: list[tuple[str, Decimal]]  # this month, biggest first
    net_worth: list[tuple[datetime.date, Decimal]]  # each month's end (today for this one)
    goals: list[GoalView]
    spare: Spare | None  # None until there's a whole month with pay
    investments: list[Investment]  # most valuable first
    roth: RothRoom | None  # None without a roth_limit

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


def from_folder(
    folder: Path,
    today: datetime.date,
    roth_limit: Decimal | None = None,
    roth_elsewhere: Decimal = _ZERO,
) -> tuple[Picture, list[str]]:
    """The picture of the ledger in folder, and the plan's `about` lines.

    Raises LedgerError if bean-check finds errors, AccountsError, PlanError.
    """
    from beancount import loader

    from tally.ledger import book
    from tally.plan import read

    loaded, errors, _ = loader.load_file(str(folder / book.MAIN))
    if errors:
        raise book.LedgerError(f"the ledger has {len(errors)} error(s); run bean-check")
    found = accounts.from_entries(loaded)
    targets = read(loaded, found, today)
    picture = build(loaded, found, targets, today, roth_limit, roth_elsewhere)
    return picture, targets.about


def build(
    loaded: Sequence[data.Directive],
    found: accounts.Accounts,
    targets: Targets,
    today: datetime.date,
    roth_limit: Decimal | None = None,
    roth_elsewhere: Decimal = _ZERO,
) -> Picture:
    starts = _month_starts(today, MONTHS)
    pay: dict[datetime.date, Decimal] = defaultdict(Decimal)
    payroll: dict[datetime.date, Decimal] = defaultdict(Decimal)  # to the 401(k)
    contributions = set(found.contributions_from.values())
    balances: dict[str, Decimal] = defaultdict(Decimal)
    added: dict[str, Decimal] = defaultdict(Decimal)
    spent: dict[datetime.date, Decimal] = defaultdict(Decimal)
    by_category: dict[tuple[str, datetime.date], Decimal] = defaultdict(Decimal)
    category_account: dict[str, str] = {}
    merchants: dict[str, Decimal] = defaultdict(Decimal)
    roth_in = roth_elsewhere
    for e in loaded:
        if not isinstance(e, data.Transaction) or e.date > today + datetime.timedelta(days=31):
            continue
        bought = _purchased(e)
        for p in e.postings:
            if p.units is None or p.units.currency != entries.CURRENCY:
                continue
            number = p.units.number or _ZERO
            if e.date <= today:
                balances[p.account] += number
                if (
                    p.account == ROTH_CASH
                    and number > 0
                    and positions.TAG not in e.tags
                    and bought.year == today.year
                ):
                    roth_in += number
                if e.date >= starts[-1]:
                    added[p.account] += number
            if _is_pay(p.account) and e.date <= today:
                pay[_month(e.date)] += -number
                if p.account in contributions:
                    payroll[_month(e.date)] += -number
            elif p.account.startswith("Expenses:") and bought <= today:
                month = _month(bought)
                spent[month] += number
                name = found.category_of(p.account) or p.account.removeprefix("Expenses:")
                by_category[name, month] += number
                category_account.setdefault(name, p.account)
                if month == starts[-1]:
                    merchants[e.payee or e.narration or ""] += number
    months = [Month(s, pay[s], spent[s], pay[s] - payroll[s]) for s in starts]
    spare = _spare(months, targets, today)
    price_map = prices.build_price_map(list(loaded))  # type: ignore[no-untyped-call]
    return Picture(
        today,
        months,
        _categories(by_category, category_account, found, starts, targets),
        sorted(((m, v) for m, v in merchants.items() if v > 0), key=lambda x: -x[1])[
            :TOP_MERCHANTS
        ],
        _net_worth(loaded, price_map, starts, today),
        _goals(targets, balances, added, spare),
        spare,
        investments(loaded, found, price_map, today),
        None if roth_limit is None else RothRoom(today.year, roth_limit, roth_in),
    )


def _spare(months: list[Month], targets: Targets, today: datetime.date) -> Spare | None:
    whole = [m for m in months[:-1] if m.pay > 0][-USUAL_MONTHS:]
    if not whole:
        return None
    n = len(whole)
    roth = targets.for_month(ROTH, today.year, today.month) or _ZERO
    return Spare(
        n,
        (sum((m.take_home for m in whole), _ZERO) / n).quantize(Decimal("0.01")),
        (sum((m.spent for m in whole), _ZERO) / n).quantize(Decimal("0.01")),
        roth,
    )


def _goals(
    targets: Targets,
    balances: dict[str, Decimal],
    added: dict[str, Decimal],
    spare: Spare | None,
) -> list[GoalView]:
    def target(g: Goal) -> Decimal | None:
        if g.months is not None:
            if spare is None:
                return None
            return (spare.spent * g.months / 100).quantize(Decimal(1)) * 100
        return g.target

    found = [(g, target(g), balances.get(g.account, _ZERO)) for g in targets.goals]
    open_ = [(g, t, b) for g, t, b in found if t is None or b < t]
    first = [g.name for g, t, b in open_ if g.months is not None]
    rest = [g.name for g, t, b in open_ if g.months is None]
    left = max(spare.left, _ZERO) if spare is not None else _ZERO
    share: dict[str, Decimal] = {}
    if first and rest:
        share |= {n: left * FIRST_SHARE / len(first) for n in first}
        share |= {n: left * (1 - FIRST_SHARE) / len(rest) for n in rest}
    elif first or rest:
        share |= {n: left / len(first or rest) for n in first or rest}
    views = []
    for g, t, b in found:
        suggested = share.get(g.name, _ZERO)
        if t is not None:
            suggested = min(suggested, max(t - b, _ZERO))
        views.append(
            GoalView(
                g.name,
                g.account,
                b,
                t,
                added.get(g.account, _ZERO),
                (suggested / 10).quantize(Decimal(1), rounding="ROUND_DOWN") * 10,
                g.months is not None,
            )
        )
    return views


def _categories(
    by_category: dict[tuple[str, datetime.date], Decimal],
    category_account: dict[str, str],
    mapping: accounts.Accounts,
    starts: list[datetime.date],
    targets: Targets,
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
                    category_account.get(name) or mapping.counter(name, ""),
                    name in mapping.fixed,
                    spent,
                    targets.monthly.get(name),
                    usual.quantize(Decimal("0.01")),
                )
            )
    return sorted(found, key=lambda c: (-max(c.spent, c.plan or _ZERO), c.name))


def _net_worth(
    loaded: Sequence[data.Directive],
    price_map: prices.PriceMap,
    starts: list[datetime.date],
    today: datetime.date,
) -> list[tuple[datetime.date, Decimal]]:
    """What everything is worth at each month's end: assets less what's owed, holdings at
    the latest price by then (or what they cost, if there's none)."""
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


def investments(
    loaded: Sequence[data.Directive],
    found: accounts.Accounts,
    price_map: prices.PriceMap,
    today: datetime.date,
) -> list[Investment]:
    """Each Fidelity account's worth today, and what was paid for what it holds."""
    roots = set(found.fidelity_accounts.values())
    held: dict[tuple[str, str], Decimal] = defaultdict(Decimal)  # (root, currency) -> units
    cost: dict[tuple[str, str], Decimal] = defaultdict(Decimal)  # -> what it cost, in USD
    for e in loaded:
        if not isinstance(e, data.Transaction) or e.date > today:
            continue
        for p in e.postings:
            root = next((r for r in roots if p.account == r or p.account.startswith(r + ":")), None)
            if root is None or p.units is None:
                continue
            number = p.units.number or _ZERO
            held[root, p.units.currency] += number
            if isinstance(p.cost, position.Cost) and p.cost.number is not None:
                cost[root, p.units.currency] += number * p.cost.number
    worth: dict[str, Decimal] = defaultdict(Decimal)
    paid: dict[str, Decimal] = defaultdict(Decimal)
    for (root, currency), units in held.items():
        if currency == entries.CURRENCY:
            worth[root] += units
            paid[root] += units
            continue
        _, price = prices.get_price(  # type: ignore[no-untyped-call]
            price_map, (currency, entries.CURRENCY), today
        )
        basis = cost[root, currency]
        worth[root] += units * price if price is not None else basis
        paid[root] += basis
    result = [
        Investment(
            root.rsplit(":", 1)[-1],
            root,
            worth[root].quantize(Decimal("0.01")),
            paid[root].quantize(Decimal("0.01")),
        )
        for root in worth
        if worth[root] or paid[root]
    ]
    return sorted(result, key=lambda i: (-i.worth, i.name))


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
