"""Discover alert emails: "Transaction Alert", sent for purchases over the limit you set, and
"Merchant refund/credit posted to your account".

A purchase alert's details are only in the HTML part (the plain-text part has the labels
with nothing after them). Its text reads:

    A transaction above the limit you set has been initiated.
    No action is needed.
    Merchant: MAPLE MARKET #0412
    Date: March 12, 2026
    Amount: $42.10

A refund alert's HTML is a table with each label in one cell and its value in the next, and
gives the day the refund posted, not the day of the purchase or the return:

    NAME, your Discover card account shows a refund
    Amount Credited:
    $16.06
    Posting Date:
    October 25, 2026
    Merchant:
    MAPLE MARKET #0412

A payment email ("We've received your payment") gives the day it posted too:

    Thanks for your payment
    Your payment of $1,234.56 posted to your account on November 12, 2026.

It doesn't name the payment, so its alert takes the name Discover's export gives an online
payment, which the rules already categorize and the posted row matches.

Other emails from Discover (statements, offers) are not alerts and give None.
"""

import re
from decimal import Decimal

from tally.alerts import (
    Alert,
    AlertError,
    check_sender,
    html_lines,
    message_id,
    parse_amount,
    parse_date,
    read,
    received,
)

ACCOUNT = "discover"
DOMAIN = "discover.com"
PURCHASE = "Transaction Alert"
REFUND = "Merchant refund/credit posted to your account"
PAYMENT = "We've received your payment"
PAYMENT_NAME = "INTERNET PAYMENT - THANK YOU"
_PAYMENT_LINE = re.compile(r"Your payment of (\S+) posted to your account on (.+)\.")


def parse(raw: bytes) -> Alert | None:
    """The alert in one email from Discover, or None if it's another kind of email."""
    message = read(raw)
    check_sender(message, DOMAIN)
    subject = " ".join(str(message["Subject"] or "").split())
    if subject not in (PURCHASE, REFUND, PAYMENT):
        return None
    html = message.get_body(("html",))
    if html is None:
        raise AlertError(f"{subject!r} alert has no HTML part")
    lines = html_lines(html.get_content())
    if subject == PURCHASE:
        date = _field(lines, "Date:")
        merchant = _field(lines, "Merchant:")
        amount = Decimal(0) - parse_amount(_field(lines, "Amount:"))
    elif subject == PAYMENT:
        found = [m for m in map(_PAYMENT_LINE.fullmatch, lines) if m]
        if len(found) != 1:
            raise AlertError("expected one 'Your payment of ... posted' line in the email")
        date, merchant = found[0].group(2), PAYMENT_NAME
        amount = parse_amount(found[0].group(1))
    else:
        date = _after(lines, "Posting Date:")
        merchant = _after(lines, "Merchant:")
        amount = parse_amount(_after(lines, "Amount Credited:"))
    return Alert(
        account=ACCOUNT,
        date=parse_date(date),
        merchant=merchant,
        amount=amount,
        message_id=message_id(message),
        received=received(message),
    )


def _field(lines: list[str], label: str) -> str:
    values = [line.removeprefix(label).strip() for line in lines if line.startswith(label)]
    if len(values) != 1 or not values[0]:
        raise AlertError(f"expected one {label!r} line in the alert, found {values}")
    return values[0]


def _after(lines: list[str], label: str) -> str:
    """The line after the one that is exactly label."""
    positions = [i for i, line in enumerate(lines) if line == label]
    if len(positions) != 1 or positions[0] + 1 == len(lines):
        raise AlertError(f"expected one {label!r} line followed by its value in the alert")
    return lines[positions[0] + 1]
