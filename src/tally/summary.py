"""The weekly summary: the past week's spending, the month so far against the plan, what to look
out for, and what's coming up.

The numbers and advice come from fixed rules here, worked out from the stored transactions, the
pending alerts and the numbers typed on the Budget tab. Claude only adds a note on the finished
summary (see summary_note):

- A category is *over plan* when the month's spending passes its "Plan / mo".
- It's *ahead of pace* when the share of its plan used is more than AHEAD above the share of
  the month gone. Only categories with at least PACE_PURCHASES purchases this month count:
  rent or a yearly fee land in one go, so pace says nothing about them.
- A purchase is *big* when it's BIG_SHARE or more of its category's monthly plan, and the
  category had other purchases this month (a lone one is a bill, like rent or a remittance).
- The monthly CSV import is *due* from IMPORT_DUE_DAY of a month until an account's CSVs reach
  the last day of the month before.
- The Roth IRA check adds what tally has seen this year, `roth_elsewhere` from config.toml, and
  the amounts typed for the rest of the year, and compares them with `roth_limit`.
"""

import calendar
import datetime
from dataclasses import dataclass
from decimal import Decimal

from tally import plan, report
from tally.advice import Review
from tally.alerts import Alert
from tally.pending import is_pending
from tally.plan import Targets
from tally.rules import UNCATEGORIZED, Rules
from tally.transaction import Transaction

ACCOUNT_NAMES = {"discover": "Discover", "bofa_checking": "BofA checking"}

AHEAD = Decimal("0.15")
PACE_PURCHASES = 3
BIG_SHARE = Decimal("0.5")
IMPORT_DUE_DAY = 3
WEEK = datetime.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Item:
    transaction: Transaction
    category: str
    pending: bool


@dataclass(frozen=True, slots=True)
class CategoryLine:
    name: str
    spent: Decimal
    plan: Decimal | None


@dataclass(frozen=True, slots=True)
class Roth:
    limit: Decimal | None
    elsewhere: Decimal


@dataclass(frozen=True, slots=True)
class Summary:
    today: datetime.date
    week: list[Item]  # spending in the 7 days up to today, newest first
    month: list[CategoryLine]  # this month so far, in Budget tab order
    last_month: str | None  # how last month ended, while this week still reaches into it
    look_out: list[str]
    coming_up: list[str]
    note: str | None  # why the plan comparisons are missing, if they are
    claude_note: str | None = None  # Claude's advice (see summary_note)
    review: Review | None = None  # Claude's review of last month, in the first summary after it

    @property
    def week_total(self) -> Decimal:
        return sum((-i.transaction.amount for i in self.week), Decimal(0))


def build(
    today: datetime.date,
    transactions: list[tuple[int, Transaction]],
    rules: Rules,
    targets: Targets | None,
    not_found: list[Alert],
    roth: Roth,
    note: str | None = None,
) -> Summary:
    """transactions holds the stored ones and the pending alerts (negative IDs, see pending.py).

    Anything dated after today is left out, so a past day's summary is what it said then.
    """
    items = [
        Item(t, rules.categorize(t), is_pending(tid)) for tid, t in transactions if t.date <= today
    ]
    plans = targets.monthly if targets is not None else {}
    counted = [i.transaction for i in items]
    this_month = report.build(counted, rules, today.year, today.month)

    week = sorted(
        (
            i
            for i in items
            if today - WEEK < i.transaction.date <= today and i.category in rules.categories
        ),
        key=lambda i: (i.transaction.date, -i.transaction.amount),
        reverse=True,
    )
    month = [
        CategoryLine(name, spent, plans.get(name)) for name, spent in this_month.spending.items()
    ]

    look_out: list[str] = []
    look_out += _over_and_ahead(today, month, items)
    look_out += _big_purchases(today, week, items, plans)
    look_out += _uncategorized(today, items)
    look_out += [
        f"Not in the bank export: {a.date:%b %-d}  {a.merchant}  {report.money(-a.amount)}. "
        "Declined or refunded? If not, check your bank's website."
        for a in not_found
    ]
    look_out += _imports_due(today, [i.transaction for i in items if not i.pending])

    coming_up = _next_month(today, rules, targets)
    coming_up += _roth(today, this_month, targets, roth)

    return Summary(
        today=today,
        week=week,
        month=month,
        last_month=_last_month(today, counted, rules, plans),
        look_out=look_out,
        coming_up=coming_up,
        note=note,
    )


def title(s: Summary) -> str:
    start = s.today - WEEK + datetime.timedelta(days=1)
    return f"tally: week of {start:%b %-d} – {s.today:%b %-d, %Y}"


NOT_PROFESSIONAL = (
    "General guidance from the numbers in your ledger, not a financial professional's advice."
)


def review_points(r: Review) -> list[tuple[str, list[str]]]:
    """The review's points under their headings, leaving out empty ones."""
    found = [("Went well", r.went_well), ("Watch", r.watch), ("Try next month", r.try_next)]
    return [(heading, points) for heading, points in found if points]


def format_summary(s: Summary) -> str:
    lines = [title(s)]
    if s.note:
        lines += ["", s.note]
    if s.claude_note:
        lines += ["", "CLAUDE'S NOTE", f"  {s.claude_note}"]
    if s.review:
        lines += ["", f"CLAUDE'S REVIEW OF {s.review.title.upper()}", f"  {s.review.headline}"]
        for heading, points in review_points(s.review):
            lines += [f"  {heading}:", *(f"  - {point}" for point in points)]
        lines.append(f"  ({NOT_PROFESSIONAL})")

    plan_total = sum((c.plan for c in s.month if c.plan is not None), Decimal(0))
    days = calendar.monthrange(s.today.year, s.today.month)[1]
    lines += ["", "THIS WEEK", f"  Spent {report.money(s.week_total)} on {len(s.week)} purchase(s)"]
    if plan_total:
        weekly = plan_total * 7 / days
        lines[-1] += f" (your plan works out to about {whole(weekly)} a week)"
    width = max((len(i.transaction.merchant) for i in s.week), default=0)
    for i in s.week:
        t = i.transaction
        mark = "  (pending)" if i.pending else ""
        lines.append(
            f"  {t.date:%a %b %-d}  {t.merchant:<{width}}  {report.money(-t.amount):>9}  "
            f"{i.category}{mark}"
        )

    lines += [
        "",
        f"{calendar.month_name[s.today.month].upper()} SO FAR (day {s.today.day} of {days})",
    ]
    name_width = max(len(c.name) for c in s.month) if s.month else 0
    for c in s.month:
        if c.plan is None:
            lines.append(f"  {c.name:<{name_width}}  {report.money(c.spent):>9}")
            continue
        used = f"{c.spent / c.plan:.0%}" if c.plan else "-"
        lines.append(
            f"  {c.name:<{name_width}}  {report.money(c.spent):>9} of {report.money(c.plan):>9}"
            f"  {used:>5}"
        )
    spent_total = sum((c.spent for c in s.month), Decimal(0))
    total = f"  {'Total':<{name_width}}  {report.money(spent_total):>9}"
    if plan_total:
        total += f" of {report.money(plan_total):>9}"
        days_left = days - s.today.day
        if days_left > 0:
            per_day = (plan_total - spent_total) / days_left
            total += f"  ({whole(per_day)} a day left for the rest of the month)"
    lines.append(total)
    if s.last_month:
        lines += ["", s.last_month]

    lines += ["", "LOOK OUT FOR"]
    lines += [f"  - {line}" for line in s.look_out] or ["  Nothing this week."]
    if s.coming_up:
        lines += ["", "COMING UP"]
        lines += [f"  - {line}" for line in s.coming_up]
    return "\n".join(lines)


def _over_and_ahead(
    today: datetime.date, month: list[CategoryLine], items: list[Item]
) -> list[str]:
    days = calendar.monthrange(today.year, today.month)[1]
    gone = Decimal(today.day) / days
    purchases = {
        c.name: sum(
            i.category == c.name
            and i.transaction.amount < 0
            and (i.transaction.date.year, i.transaction.date.month) == (today.year, today.month)
            for i in items
        )
        for c in month
    }
    over = sorted(
        ((c.spent - c.plan, c.name) for c in month if c.plan and c.spent > c.plan), reverse=True
    )
    lines = []
    if over:
        names = ", ".join(f"{name} by {report.money(amount)}" for amount, name in over)
        lines.append(f"Over plan this month: {names}.")
    for c in month:
        if not c.plan or c.spent > c.plan:
            continue
        if c.spent / c.plan > gone + AHEAD and purchases[c.name] >= PACE_PURCHASES:
            left = c.plan - c.spent
            days_left = days - today.day
            per_day = f", about {whole(left / days_left)} a day" if days_left else ""
            lines.append(
                f"{c.name} is ahead of pace: {c.spent / c.plan:.0%} of its plan used with "
                f"{gone:.0%} of the month gone. {report.money(left)} left{per_day}."
            )
    return lines


def _big_purchases(
    today: datetime.date, week: list[Item], items: list[Item], plans: dict[str, Decimal]
) -> list[str]:
    in_month = [
        i
        for i in items
        if i.transaction.amount < 0
        and (i.transaction.date.year, i.transaction.date.month) == (today.year, today.month)
    ]
    lines = []
    for i in week:
        plan = plans.get(i.category)
        spent = -i.transaction.amount
        others = sum(other.category == i.category and other is not i for other in in_month)
        if plan and spent >= plan * BIG_SHARE and others:
            lines.append(
                f"{i.transaction.merchant} ({report.money(spent)}) was {spent / plan:.0%} of "
                f"the monthly {i.category} plan on its own."
            )
    return lines


def _uncategorized(today: datetime.date, items: list[Item]) -> list[str]:
    found = [
        i.transaction
        for i in items
        if i.category == UNCATEGORIZED
        and (i.transaction.date.year, i.transaction.date.month) == (today.year, today.month)
    ]
    if not found:
        return []
    names = ", ".join(f"{t.merchant} ({report.money(t.amount)})" for t in found)
    return [
        f"{len(found)} uncategorized this month, not counted in the totals: {names}. "
        "Pick a category on the Transactions tab and sync."
    ]


def _imports_due(today: datetime.date, posted: list[Transaction]) -> list[str]:
    if today.day < IMPORT_DUE_DAY:
        return []
    month_end = today.replace(day=1) - datetime.timedelta(days=1)
    covered: dict[str, datetime.date] = {}
    for t in posted:
        day = t.posted or t.date
        covered[t.account] = max(covered.get(t.account, day), day)
    return [
        f"Time for the monthly import: {ACCOUNT_NAMES.get(account, account)}'s CSVs end "
        f"{day:%b %-d}. Download {month_end:%B}'s statement CSV and run `tally import`."
        for account, day in sorted(covered.items())
        if day < month_end
    ]


def _last_month(
    today: datetime.date, counted: list[Transaction], rules: Rules, plans: dict[str, Decimal]
) -> str | None:
    if today.day >= 7:
        return None
    end = today.replace(day=1) - datetime.timedelta(days=1)
    last = report.build(counted, rules, end.year, end.month)
    line = f"{end:%B} ended at {report.money(last.total_spending)}"
    plan_total = sum((plans[c] for c in rules.categories if c in plans), Decimal(0))
    if plan_total:
        line += f" of a {report.money(plan_total)} plan"
    over = sorted(
        (
            (spent - plans[name], name)
            for name, spent in last.spending.items()
            if name in plans and spent > plans[name]
        ),
        reverse=True,
    )
    if over:
        line += "; over in " + ", ".join(f"{n} by {report.money(a)}" for a, n in over)
    return line + "."


def _next_month(today: datetime.date, rules: Rules, targets: Targets | None) -> list[str]:
    if targets is None:
        return []
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    parts = []
    paychecks = targets.for_month(plan.PAYCHECKS, year, month)
    if paychecks is not None:
        parts.append(f"{paychecks:.0f} paycheck(s)")
    plan_total = sum(
        (targets.monthly[c] for c in rules.categories if c in targets.monthly), Decimal(0)
    )
    if plan_total:
        parts.append(f"{report.money(plan_total)} of spending planned")
    roth = targets.for_month(plan.ROTH, year, month)
    if roth:
        parts.append(f"{report.money(roth)} to the Roth IRA")
    if not parts:
        return []
    return [f"{calendar.month_name[month]}: {', '.join(parts)}."]


def _roth(
    today: datetime.date, this_month: report.MonthReport, targets: Targets | None, roth: Roth
) -> list[str]:
    contributed = this_month.investing_year_to_date + roth.elsewhere
    planned = Decimal(0)
    if targets is not None:
        # What's still planned this month: the plan less what has gone in already.
        this = targets.for_month(plan.ROTH, today.year, today.month) or Decimal(0)
        planned = max(this - this_month.investing, Decimal(0))
        planned += sum(
            (
                amount
                for m in range(today.month + 1, 13)
                if (amount := targets.for_month(plan.ROTH, today.year, m)) is not None
            ),
            Decimal(0),
        )
    line = f"Roth IRA {today.year}: {report.money(contributed)} in so far"
    if planned:
        line += f", {report.money(planned)} more planned"
    if roth.limit is None:
        return [line + "."]
    room = roth.limit - contributed
    if room < 0:
        return [
            f"{line}. That's {report.money(-room)} over the {report.money(roth.limit)} limit: "
            "ask the brokerage to remove the excess before the tax deadline to avoid a penalty."
        ]
    if planned > room:
        return [
            f"{line}. That would go {report.money(planned - room)} over the "
            f"{report.money(roth.limit)} limit; lower a planned transfer."
        ]
    if planned == room:
        return [f"{line}, which reaches the {report.money(roth.limit)} limit exactly."]
    return [
        f"{line}. {report.money(room - planned)} of the {report.money(roth.limit)} limit "
        "would be left unused."
    ]


def whole(amount: Decimal) -> str:
    """Money in whole dollars, for estimates."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.0f}"
