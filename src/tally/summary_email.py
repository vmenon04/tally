"""The weekly summary as an HTML email, alongside the plain text version.

Built from the same Summary as the text, laid out with tables and inline styles because mail
apps (Gmail especially) ignore most other CSS. Every piece of text is escaped: merchant names
come from bank emails and CSVs, so they're never trusted as markup.
"""

import calendar
from decimal import Decimal
from html import escape

from tally import report
from tally.summary import AHEAD, CategoryLine, Item, Summary, title, whole

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_TEXT = "#202124"
_MUTED = "#5f6368"
_LINE = "#e8eaed"
_GREEN = "#188038"
_AMBER = "#e37400"
_RED = "#d93025"
_BLUE = "#1a73e8"


def format_html(s: Summary, extra: str = "") -> str:
    """extra: more HTML for the end, already escaped (the ledger's report)."""
    days = calendar.monthrange(s.today.year, s.today.month)[1]
    plan_total = sum((c.plan for c in s.month if c.plan is not None), Decimal(0))
    parts = [_header(s, plan_total, days)]
    if s.note:
        parts.append(_callout(s.note, _AMBER, "#fef7e0"))
    if s.claude_note:
        parts.append(_heading("Claude's note"))
        parts.append(_callout(s.claude_note, _BLUE, "#e8f0fe"))
    parts.append(_heading("This week"))
    parts.append(_week(s.week))
    month = calendar.month_name[s.today.month]
    parts.append(_heading(f"{month} so far", f"day {s.today.day} of {days}"))
    parts.append(_month(s, plan_total, days))
    if s.last_month:
        parts.append(f'<p style="margin:12px 0 0;color:{_MUTED};">{escape(s.last_month)}</p>')
    parts.append(_heading("Look out for"))
    if s.look_out:
        parts += [_callout(line, _AMBER, "#fef7e0") for line in s.look_out]
    else:
        parts.append(_callout("Nothing this week.", _GREEN, "#e6f4ea"))
    if s.coming_up:
        parts.append(_heading("Coming up"))
        parts += [_callout(line, _BLUE, "#e8f0fe") for line in s.coming_up]
    if extra:
        parts.append(extra)
    body = "\n".join(parts)
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title(s))}</title></head>"
        '<body style="margin:0;padding:0;background:#f1f3f4;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f1f3f4;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;background:#ffffff;border-radius:12px;font-family:{_FONT};'
        f'font-size:14px;line-height:1.45;color:{_TEXT};">'
        f'<tr><td style="padding:24px;">\n{body}\n</td></tr></table>'
        f'<p style="font-family:{_FONT};font-size:12px;color:{_MUTED};margin:12px 0 0;">'
        "Sent by tally every Sunday.</p>"
        "</td></tr></table></body></html>\n"
    )


def _header(s: Summary, plan_total: Decimal, days: int) -> str:
    week = title(s).removeprefix("tally: week")
    count = len(s.week)
    line = f"spent on {count} purchase{'s' if count != 1 else ''}"
    if plan_total:
        line += f" · your plan allows about {whole(plan_total * 7 / days)} a week"
    return (
        f'<div style="color:{_MUTED};font-size:13px;">Week{escape(week)}</div>'
        f'<div style="font-size:34px;font-weight:700;margin:4px 0 2px;">'
        f"{escape(report.money(s.week_total))}</div>"
        f'<div style="color:{_MUTED};">{escape(line)}</div>'
    )


def _heading(text: str, aside: str = "") -> str:
    side = (
        f'<span style="color:{_MUTED};font-weight:400;"> · {escape(aside)}</span>' if aside else ""
    )
    return (
        f'<h2 style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;'
        f'color:{_TEXT};margin:28px 0 8px;padding-bottom:6px;border-bottom:1px solid {_LINE};">'
        f"{escape(text)}{side}</h2>"
    )


def _week(items: list[Item]) -> str:
    if not items:
        return f'<p style="margin:0;color:{_MUTED};">No purchases.</p>'
    rows = []
    for i in items:
        t = i.transaction
        style = f"color:{_MUTED};font-style:italic;" if i.pending else ""
        badge = (
            f' <span style="font-size:11px;color:{_MUTED};border:1px solid {_LINE};'
            'border-radius:8px;padding:0 6px;font-style:normal;">pending</span>'
            if i.pending
            else ""
        )
        rows.append(
            f'<tr style="{style}">'
            f'<td style="padding:6px 8px 6px 0;color:{_MUTED};white-space:nowrap;'
            f'vertical-align:top;">{t.date:%a %b %-d}</td>'
            f'<td style="padding:6px 8px;vertical-align:top;">{escape(t.merchant)}{badge}'
            f'<div style="font-size:12px;color:{_MUTED};">{escape(i.category)}</div></td>'
            f'<td style="padding:6px 0 6px 8px;text-align:right;white-space:nowrap;'
            f'vertical-align:top;">{escape(report.money(-t.amount))}</td></tr>'
        )
    return _table("".join(rows))


def _month(s: Summary, plan_total: Decimal, days: int) -> str:
    gone = Decimal(s.today.day) / days
    rows = [_category(c, gone) for c in s.month]
    spent_total = sum((c.spent for c in s.month), Decimal(0))
    total = report.money(spent_total)
    if plan_total:
        total += f" of {report.money(plan_total)}"
    rows.append(
        f'<tr><td style="padding:10px 0 0;font-weight:700;border-top:1px solid {_LINE};">Total</td>'
        f'<td style="padding:10px 0 0;text-align:right;font-weight:700;'
        f'border-top:1px solid {_LINE};">{escape(total)}</td></tr>'
    )
    days_left = days - s.today.day
    if plan_total and days_left > 0:
        per_day = (plan_total - spent_total) / days_left
        rows.append(
            f'<tr><td colspan="2" style="padding:2px 0 0;color:{_MUTED};">'
            f"{escape(whole(per_day))} a day left for the rest of the month</td></tr>"
        )
    return _table("".join(rows))


def _category(c: CategoryLine, gone: Decimal) -> str:
    if not c.plan:
        amount = report.money(c.spent)
        bar = ""
    else:
        used = c.spent / c.plan
        color = _RED if used > 1 else _AMBER if used > gone + AHEAD else _GREEN
        width = min(int(used * 100), 100)
        amount = f"{report.money(c.spent)} of {report.money(c.plan)} · {used:.0%}"
        cells = [(width, color), (100 - width, _LINE)]
        bar = (
            '<tr><td colspan="2" style="padding:3px 0 8px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{_LINE};border-radius:3px;"><tr>'
            + "".join(
                f'<td width="{w}%" style="background:{bg};height:6px;font-size:0;'
                'line-height:0;border-radius:3px;">&nbsp;</td>'
                for w, bg in cells
                if w > 0
            )
            + "</tr></table></td></tr>"
        )
    return (
        f'<tr><td style="padding:6px 0 0;">{escape(c.name)}</td>'
        f'<td style="padding:6px 0 0;text-align:right;white-space:nowrap;color:{_MUTED};">'
        f"{escape(amount)}</td></tr>{bar}"
    )


def _callout(text: str, color: str, background: str) -> str:
    return (
        f'<div style="border-left:3px solid {color};background:{background};'
        f'padding:8px 12px;margin:8px 0;border-radius:4px;">{escape(text)}</div>'
    )


def _table(rows: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;">{rows}</table>'
    )
