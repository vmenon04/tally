"""Pairing alerts with the posted CSV rows that explain them.

A posted row explains an alert when it is on the same account, dated from a day before the
alert to three days after (the CSVs date a row by the purchase, and alerts sent around
midnight can land a day off), its merchant name agrees, and its amount is the same, up to
30% more (a tip), anything when the alert was a $1.00 authorization (gas stations), or
less when the alert was a hold of whole dollars, $50 or more (a gas pump's $200, a
hotel's), which the card later settles for what was actually spent.
Money in (a deposit, a refund) has no tip or hold, so its amount must be exactly the same,
and its alert gives the day it posted, so the window is counted from the row's posted date
(a refund's CSV date is the merchant's, which can be days earlier).

Names agree when one starts with the other, or both start with the same main word, after
punctuation and anything with a digit in it (store numbers, phone numbers, reference codes)
are dropped: "WAL-MART #1234" agrees with "WALMART STORE 01234 SPRINGFIELD IL". Payment
processor prefixes like "SQ *" are kept but never count as the main word.

If the bank spells a name differently in the CSV, an exact amount in the same date window
still matches when it's the only such row for that alert. Those are reported separately.
"""

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tally.alerts import BANK_PENDING, Alert
from tally.transaction import Transaction

DAYS_BEFORE = 1
DAYS_AFTER = 3
TIP = Decimal("1.30")
AUTHORIZATION = Decimal("1.00")
HOLD = Decimal(50)  # whole-dollar alerts from this much may settle for less
# A CSV that reaches this many days past an alert's date would show its row by now.
COVERED_AFTER = datetime.timedelta(days=3)

PROCESSORS = {"DD", "PAYPAL", "PP", "SP", "SPO", "SQ", "TST"}


@dataclass(frozen=True, slots=True)
class Match:
    alert: Alert
    transaction: Transaction
    by_amount_only: bool  # the names didn't agree


@dataclass(frozen=True, slots=True)
class Result:
    matches: list[Match]
    not_found: list[Alert]  # the CSVs cover the alert's dates, but no row explains it
    waiting: list[Alert]  # the CSVs don't reach the alert's date yet
    # On an account SimpleFIN fills: an email that came before its last answer, which
    # covered it, or a pending transaction it no longer lists.
    superseded: list[Alert] = field(default_factory=list)


def match(
    alerts: Sequence[Alert],
    transactions: Sequence[Transaction],
    fetched: dict[str, datetime.datetime] | None = None,
) -> Result:
    """Pair each alert with at most one posted row, and each row with at most one alert.

    Pairs are taken best first: agreeing names, then the closest amount, then the closest date.
    fetched says when SimpleFIN last answered for the accounts it fills: on those, an alert
    left over only counts if SimpleFIN listed it as pending then, or it's an email since.
    """
    candidates = []
    for i, a in enumerate(alerts):
        in_window = [
            (j, t)
            for j, t in enumerate(transactions)
            if t.account == a.account and _in_window(a, t)
        ]
        named = {j for j, t in in_window if names_agree(a.merchant, t.merchant)}
        candidates += [
            (False, i, j) for j, t in in_window if j in named and _amount_fits(a.amount, t.amount)
        ]
        exact = [j for j, t in in_window if j not in named and t.amount == a.amount]
        if len(exact) == 1:
            candidates.append((True, i, exact[0]))

    def rank(candidate: tuple[bool, int, int]) -> tuple[bool, Decimal, int, int, int]:
        by_amount_only, i, j = candidate
        a, t = alerts[i], transactions[j]
        return (by_amount_only, abs(t.amount - a.amount), abs((_row_day(a, t) - a.date).days), i, j)

    used_alerts: set[int] = set()
    used_rows: set[int] = set()
    matches: dict[int, Match] = {}
    for by_amount_only, i, j in sorted(candidates, key=rank):
        if i in used_alerts or j in used_rows:
            continue
        used_alerts.add(i)
        used_rows.add(j)
        matches[i] = Match(alerts[i], transactions[j], by_amount_only)

    covered: dict[str, datetime.date] = {}
    for t in transactions:
        day = t.posted or t.date
        covered[t.account] = max(covered.get(t.account, day), day)
    not_found, waiting, superseded = [], [], []
    for i, a in enumerate(alerts):
        if i in matches:
            continue
        since = (fetched or {}).get(a.account)
        if since is not None and not _current(a, since):
            superseded.append(a)
            continue
        reaches = covered.get(a.account)
        if reaches is not None and reaches >= a.date + COVERED_AFTER:
            not_found.append(a)
        else:
            waiting.append(a)
    return Result([matches[i] for i in sorted(matches)], not_found, waiting, superseded)


def _current(a: Alert, since: datetime.datetime) -> bool:
    if a.message_id.startswith(BANK_PENDING):
        return a.received >= since  # listed in SimpleFIN's last answer
    return a.received > since


def names_agree(alert_name: str, posted_name: str) -> bool:
    a, b = name_words(alert_name), name_words(posted_name)
    if not a or not b:
        return False
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter] or _main_word(a) == _main_word(b)


def name_words(name: str) -> list[str]:
    """The words of a merchant name that identify it: "SQ *JOE'S #12" -> ["SQ", "JOES"]."""
    # Only between letters: "WAL-MART" is one word, but "MBTA-5500" is a name and a number.
    joined = re.sub(r"(?<=[A-Z])['-](?=[A-Z])", "", name.upper())
    return [w for w in re.split(r"[^A-Z0-9]+", joined) if w and not re.search(r"\d", w)]


def _main_word(words: list[str]) -> str:
    return next((w for w in words if w not in PROCESSORS), words[0])


def _in_window(a: Alert, t: Transaction) -> bool:
    return -DAYS_BEFORE <= (_row_day(a, t) - a.date).days <= DAYS_AFTER


def _row_day(a: Alert, t: Transaction) -> datetime.date:
    """The row's date to compare with the alert's: posted for money in, else the purchase's."""
    return (t.posted or t.date) if a.amount > 0 else t.date


def _amount_fits(alerted: Decimal, posted: Decimal) -> bool:
    if (alerted < 0) != (posted < 0):
        return False
    if alerted > 0:
        return posted == alerted
    alerted, posted = abs(alerted), abs(posted)
    if alerted == AUTHORIZATION:
        return True
    if alerted >= HOLD and alerted == alerted.to_integral_value() and posted < alerted:
        return True
    return alerted <= posted <= alerted * TIP
