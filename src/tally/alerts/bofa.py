"""Bank of America alert emails for the checking account: "Your debit card was used" for
purchases on the debit card, and "A direct deposit was credited to your account" for
paychecks and other direct deposits.

Both are HTML only, laid out as a table with each label in one cell and its value in the
next, so a debit card alert's text reads:

    Your debit card was used
    Your debit card was used online, by phone, by mail or at a location where ...
    Your debit card
    ending in 1234
    Amount
    $44.28
    Purchase made at
    MAPLE MARKET #0412 SPRINGFIELD ,IL
    Purchase date
    October 17, 2026

and a deposit alert's (the payer is named as it is in the CSV, without the DES: details):

    A direct deposit was
    credited to your account
    Amount
    $1,234.56
    Account
    PERSONAL CHECKING/SAVINGS ACCOUNT - 1234
    Date
    October 23, 2026
    From
    Acme Payroll 123456

Other emails from Bank of America (statements, balances, offers) are not alerts and give None.

The daily "Your Available Balance" email isn't an alert either, but `balance` reads it, so
the ledger can be checked against what the bank says:

    Your available balance
    Balance
    $1,234.56
    Account
    PERSONAL CHECKING/SAVINGS ACCOUNT - 1234
    Date
    November 13, 2026
"""

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from tally.alerts import (
    Alert,
    AlertError,
    check_sender,
    html_lines,
    is_from,
    message_id,
    parse_amount,
    parse_date,
    read,
    received,
)

ACCOUNT = "bofa_checking"
DOMAIN = "bankofamerica.com"
PURCHASE = "Your debit card was used"
DEPOSIT = "A direct deposit was credited to your account"
BALANCE = "Your Available Balance"


@dataclass(frozen=True, slots=True)
class Balance:
    """What a balance email says an account had available, early on the day it states."""

    label: str  # the last four of the account number
    date: datetime.date
    amount: Decimal
    received: datetime.datetime


def balance(raw: bytes) -> Balance | None:
    """The balance in one email from Bank of America, or None if it's another kind of email."""
    message = read(raw)
    if not is_from(message, DOMAIN) or _subject(message) != BALANCE:
        return None
    check_sender(message, DOMAIN)
    html = message.get_body(("html",))
    if html is None:
        raise AlertError("balance email has no HTML part")
    lines = html_lines(html.get_content())
    account = re.search(r"\b(\d{4})$", _after(lines, "Account"))
    if account is None:
        raise AlertError("expected the balance email's account to end in its last four")
    stated = _after(lines, "Balance")
    amount = parse_amount(stated.removeprefix("-"))
    return Balance(
        label=account.group(1),
        date=parse_date(_after(lines, "Date")),
        amount=-amount if stated.startswith("-") else amount,
        received=received(message),
    )


def latest_balances(paths: Iterable[Path]) -> list[Balance]:
    """The newest balance email among these files for each account. Others are skipped, and
    so is one that can't be read: emails are checked as they arrive, by `tally alerts`."""
    newest: dict[str, Balance] = {}
    for path in paths:
        try:
            found = balance(path.read_bytes())
        except (OSError, AlertError):
            continue
        if found is not None and (
            found.label not in newest or found.received > newest[found.label].received
        ):
            newest[found.label] = found
    return sorted(newest.values(), key=lambda b: b.label)


def _subject(message: EmailMessage) -> str:
    return " ".join(str(message["Subject"] or "").split())


def parse(raw: bytes) -> Alert | None:
    """The alert in one email from Bank of America, or None if it's another kind of email."""
    message = read(raw)
    check_sender(message, DOMAIN)
    subject = _subject(message)
    if subject not in (PURCHASE, DEPOSIT):
        return None
    html = message.get_body(("html",))
    if html is None:
        raise AlertError(f"{subject!r} alert has no HTML part")
    lines = html_lines(html.get_content())
    amount = parse_amount(_after(lines, "Amount"))
    if subject == PURCHASE:
        date_label, merchant_label = "Purchase date", "Purchase made at"
        amount = Decimal(0) - amount
    else:
        date_label, merchant_label = "Date", "From"
    return Alert(
        account=ACCOUNT,
        date=parse_date(_after(lines, date_label)),
        merchant=_after(lines, merchant_label),
        amount=amount,
        message_id=message_id(message),
        received=received(message),
    )


def _after(lines: list[str], label: str) -> str:
    """The line after the one that is exactly label. Some alerts repeat the whole block of
    details, so the label can appear more than once if its value is the same each time."""
    positions = [i for i, line in enumerate(lines) if line == label]
    values = {lines[i + 1] for i in positions if i + 1 < len(lines)}
    if len(values) != 1 or positions[-1] + 1 == len(lines):
        raise AlertError(f"expected one {label!r} line followed by its value in the alert")
    return values.pop()
