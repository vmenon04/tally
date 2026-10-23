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
"""

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

ACCOUNT = "bofa_checking"
DOMAIN = "bankofamerica.com"
PURCHASE = "Your debit card was used"
DEPOSIT = "A direct deposit was credited to your account"


def parse(raw: bytes) -> Alert | None:
    """The alert in one email from Bank of America, or None if it's another kind of email."""
    message = read(raw)
    check_sender(message, DOMAIN)
    subject = " ".join(str(message["Subject"] or "").split())
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
    """The line after the one that is exactly label."""
    positions = [i for i, line in enumerate(lines) if line == label]
    if len(positions) != 1 or positions[0] + 1 == len(lines):
        raise AlertError(f"expected one {label!r} line followed by its value in the alert")
    return lines[positions[0] + 1]
