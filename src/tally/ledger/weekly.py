"""The weekly report, worked out from the ledger with bean-query.

- spending per category, this month so far against the same days last month, and last
  month's total;
- Roth IRA contributions this year against the limit.

Spending belongs to the month the purchase happened (the `purchased` date when there is
one, else the entry's date), and includes pending alerts.
"""

import calendar
import datetime
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path

import beanquery
from beancount import loader

from tally.ledger import accounts, book, positions
from tally.ledger.picture import ROTH_CASH

# The day a purchase happened: `purchased` when the entry has it, else the entry's date.
_DAY = "coalesce(date(entry_meta('purchased')), date)"


class ReportError(Exception):
    """The ledger couldn't be read or queried."""


@dataclass(frozen=True, slots=True)
class Line:
    category: str
    this_month: Decimal  # day 1 to today
    last_month_so_far: Decimal  # day 1 to the same day last month
    last_month: Decimal  # all of last month


@dataclass(frozen=True, slots=True)
class Report:
    today: datetime.date
    lines: list[Line]
    pending: int  # entries this month still waiting for a bank export
    roth_in: Decimal  # contributed this year: in the ledger plus elsewhere
    roth_limit: Decimal | None

    def total(self, field: str) -> Decimal:
        return sum((getattr(line, field) for line in self.lines), Decimal(0))


def build(
    folder: Path, today: datetime.date, roth_limit: Decimal | None, roth_elsewhere: Decimal
) -> Report:
    entries, errors, options = loader.load_file(str(folder / book.MAIN))
    try:
        found = accounts.from_entries(entries)
    except accounts.AccountsError as e:
        raise ReportError(str(e)) from e
    connection = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)

    def query(sql: str) -> list[tuple[object, ...]]:
        try:
            return list(connection.execute(sql).fetchall())
        except Exception as e:  # beanquery raises its own error types for bad queries
            raise ReportError(f"bean-query failed: {e}") from e

    def spending(start: datetime.date, end: datetime.date) -> dict[str, Decimal]:
        rows = query(
            f"SELECT account, sum(number) WHERE account ~ '^Expenses:' "
            f"AND {_DAY} >= {start} AND {_DAY} <= {end} GROUP BY account"
        )
        return {str(account): Decimal(str(total)) for account, total in rows}

    first = today.replace(day=1)
    last_end = first - datetime.timedelta(days=1)
    last_start = last_end.replace(day=1)
    last_same = last_start.replace(day=min(today.day, last_end.day))
    now, before, whole = (
        spending(first, today),
        spending(last_start, last_same),
        spending(last_start, last_end),
    )
    names = sorted(
        {*now, *before, *whole},
        key=lambda a: (a == _uncategorized(found), _order(found, a), a),
    )
    lines = [
        Line(
            found.category_of(a) or a.removeprefix("Expenses:"),
            now.get(a, Decimal(0)),
            before.get(a, Decimal(0)),
            whole.get(a, Decimal(0)),
        )
        for a in names
    ]
    pending_ids = query(
        f"SELECT id WHERE flag = '!' AND account ~ '^Expenses:' "
        f"AND {_DAY} >= {first} AND {_DAY} <= {today}"
    )
    roth = query(
        # Money in from your bank, not the cash a Fidelity snapshot moves into holdings.
        f"SELECT sum(number) WHERE account = '{ROTH_CASH}' AND number > 0 "
        f"AND NOT ('{positions.TAG}' IN tags) "
        f"AND year({_DAY}) = {today.year} AND {_DAY} <= {today}"
    )
    roth_in = Decimal(str(roth[0][0])) if roth and roth[0][0] is not None else Decimal(0)
    return Report(
        today, lines, len({row[0] for row in pending_ids}), roth_in + roth_elsewhere, roth_limit
    )


def _order(found: accounts.Accounts, account: str) -> int:
    """Accounts in the order accounts.beancount opens them, like the Budget tab's lines."""
    names = list(dict.fromkeys(c.account for cs in found.categories.values() for c in cs))
    return names.index(account) if account in names else len(names)


def _uncategorized(found: accounts.Accounts) -> str:
    choices = found.categories.get("Uncategorized", ())
    return choices[0].account if choices else ""


def format_text(r: Report) -> str:
    month, last = calendar.month_name[r.today.month], _last_month_name(r.today)
    headers = (f"{month} so far", f"{last} same days", last)
    widths = [max(len(h), 11) + 2 for h in headers]

    def row(name: str, values: tuple[Decimal, ...]) -> str:
        return f"  {name[:26]:26}" + "".join(
            f"{_money(v):>{w}}" for v, w in zip(values, widths, strict=True)
        )

    lines = [
        "FROM THE LEDGER (bean-query)",
        "",
        f"  {'':26}" + "".join(f"{h:>{w}}" for h, w in zip(headers, widths, strict=True)),
    ]
    for line in r.lines:
        lines.append(row(line.category, (line.this_month, line.last_month_so_far, line.last_month)))
    lines.append(
        row(
            "Total",
            (r.total("this_month"), r.total("last_month_so_far"), r.total("last_month")),
        )
    )
    if r.pending:
        lines.append(f"  Includes {r.pending} pending purchase(s) not yet in a bank export.")
    lines += ["", f"  {_roth(r)}"]
    return "\n".join(lines)


def format_html(r: Report) -> str:
    month, last = calendar.month_name[r.today.month], _last_month_name(r.today)
    cell = "padding:6px 8px;border-bottom:1px solid #e8eaed;"
    right = cell + "text-align:right;white-space:nowrap;"
    rows = [
        f'<tr><th style="{cell}text-align:left;"></th>'
        f'<th style="{right}">{escape(month)} so far</th>'
        f'<th style="{right}">{escape(last)} same days</th>'
        f'<th style="{right}">{escape(last)}</th></tr>'
    ]
    for line in [*r.lines, None]:
        name = "Total" if line is None else line.category
        values = (
            (r.total("this_month"), r.total("last_month_so_far"), r.total("last_month"))
            if line is None
            else (line.this_month, line.last_month_so_far, line.last_month)
        )
        weight = "font-weight:600;" if line is None else ""
        rows.append(
            f'<tr><td style="{cell}{weight}">{escape(name)}</td>'
            + "".join(f'<td style="{right}{weight}">{_money(v)}</td>' for v in values)
            + "</tr>"
        )
    pending = (
        f'<p style="margin:8px 0 0;color:#5f6368;">Includes {r.pending} pending purchase(s) '
        "not yet in a bank export.</p>"
        if r.pending
        else ""
    )
    return (
        '<h2 style="margin:28px 0 8px;font-size:16px;">From the ledger</h2>'
        '<table style="border-collapse:collapse;width:100%;font-size:14px;">'
        + "".join(rows)
        + "</table>"
        + pending
        + f'<p style="margin:12px 0 0;">{escape(_roth(r))}</p>'
    )


def _roth(r: Report) -> str:
    text = f"Roth IRA {r.today.year}: {_money(r.roth_in)} contributed"
    if r.roth_limit is None:
        return text + "."
    left = r.roth_limit - r.roth_in
    if left >= 0:
        return f"{text} of the {_money(r.roth_limit)} limit; {_money(left)} of room left."
    return f"{text}, {_money(-left)} OVER the {_money(r.roth_limit)} limit."


def _last_month_name(today: datetime.date) -> str:
    return calendar.month_name[(today.month - 2) % 12 + 1]


def _money(amount: Decimal) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
