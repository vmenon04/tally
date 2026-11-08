"""Your plan, as the weekly summary uses it, read from the ledger's plan.beancount.

    2026-09-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD
    2026-11-01 custom "paychecks" 2
    2026-11-01 custom "roth-plan" 600.00 USD

A budget line is Fava's own: Fava shows it as each month's budget for that account, and it
holds until a later budget line for the same account replaces it. Only "monthly" budgets
are used here. `paychecks` and `roth-plan` are tally's: how many paychecks a month brings,
and the Roth IRA transfer planned from checking that month; each covers the month it's
dated in.

A goal is money set aside in an account of its own:

    2026-11-01 custom "goal" "Emergency fund" Assets:CapitalOne:Savings "months" 3
    2026-11-01 custom "goal" "Travel" Assets:BofA:Savings
    2026-11-01 custom "goal" "Laptop" Assets:BofA:Savings "target" 1200.00 USD

`about` lines tell Claude's advice about you, in your words, in the order written:

    2026-11-01 custom "about" "First job; I want to travel more"

Its progress is the account's balance. "months" makes the target that many months of your
usual spending, and makes the goal come first while it's short. A later line for the same
name replaces an earlier one.
"""

import calendar
import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from beancount.core import data

from tally.ledger.accounts import Accounts

PAYCHECKS = "paychecks"
ROTH = "roth-plan"
GOAL = "goal"
ABOUT = "about"


class PlanError(ValueError):
    """A plan line is written in a way tally can't use."""


@dataclass(frozen=True, slots=True)
class Goal:
    name: str
    account: str
    months: Decimal | None = None  # target: this many months of usual spending
    target: Decimal | None = None  # or this amount


@dataclass(frozen=True, slots=True)
class Targets:
    monthly: dict[str, Decimal] = field(default_factory=dict)  # category -> plan per month
    months: dict[str, dict[str, Decimal]] = field(default_factory=dict)  # kind -> month -> n
    goals: list[Goal] = field(default_factory=list)
    about: list[str] = field(default_factory=list)

    def for_month(self, kind: str, year: int, month: int) -> Decimal | None:
        return self.months.get(kind, {}).get(month_header(year, month))


def month_header(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def read(entries: Iterable[data.Directive], found: Accounts, today: datetime.date) -> Targets:
    """The plan in effect today: each category's latest monthly budget, and every month's
    paychecks and planned Roth IRA transfer."""
    monthly: dict[str, tuple[datetime.date, Decimal]] = {}
    months: dict[str, dict[str, Decimal]] = {}
    goals: dict[str, tuple[datetime.date, Goal]] = {}
    about: list[str] = []
    for e in entries:
        if not isinstance(e, data.Custom):
            continue
        values = [v.value for v in e.values]
        if e.type == "budget":
            if len(values) != 3 or values[1] != "monthly":
                continue  # other periods are for Fava only
            account, _, amount = values
            category = found.category_of(str(account))
            if category is None:
                raise PlanError(f"{e.date}: budget for {account}, which has no category")
            if e.date <= today and (category not in monthly or monthly[category][0] <= e.date):
                monthly[category] = (e.date, _number(amount, e))
        elif e.type == ABOUT:
            if e.date <= today:
                about += [str(v) for v in values]
        elif e.type == GOAL:
            goal = _goal(values, e)
            if e.date <= today and (goal.name not in goals or goals[goal.name][0] <= e.date):
                goals[goal.name] = (e.date, goal)
        elif e.type in (PAYCHECKS, ROTH):
            if len(values) != 1:
                raise PlanError(f"{e.date}: {e.type} takes one number")
            months.setdefault(e.type, {})[month_header(e.date.year, e.date.month)] = _number(
                values[0], e
            )
    return Targets(
        {k: v for k, (_, v) in monthly.items()},
        months,
        [g for _, g in goals.values()],
        about,
    )


def _goal(values: list[object], e: data.Custom) -> Goal:
    if len(values) not in (2, 4) or not isinstance(values[0], str):
        raise PlanError(
            f'{e.date}: write a goal as "Name" Account, then "months" N or "target" X USD'
        )
    name, account = values[0], str(values[1])
    if len(values) == 2:
        return Goal(name, account)
    if values[2] == "months":
        return Goal(name, account, months=_number(values[3], e))
    if values[2] == "target":
        return Goal(name, account, target=_number(values[3], e))
    raise PlanError(f'{e.date}: a goal takes "months" or "target", not {values[2]!r}')


def _number(value: object, e: data.Custom) -> Decimal:
    number = getattr(value, "number", value)
    if isinstance(number, bool) or not isinstance(number, Decimal | int):
        raise PlanError(f"{e.date}: {e.type} needs a number, got {value!r}")
    return Decimal(number)
