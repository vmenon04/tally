"""Discover "Transaction Alert" emails, sent for purchases over the limit you set.

The details are only in the HTML part (the plain-text part has the labels with nothing
after them). Its text reads:

    A transaction above the limit you set has been initiated.
    No action is needed.
    Merchant: MAPLE MARKET #0412
    Date: March 12, 2026
    Amount: $42.10

Other emails from Discover (statements, payments, offers) are not alerts and give None.
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

ACCOUNT = "discover"
DOMAIN = "discover.com"
SUBJECT = "Transaction Alert"


def parse(raw: bytes) -> Alert | None:
    """The alert in one email from Discover, or None if it's another kind of email."""
    message = read(raw)
    check_sender(message, DOMAIN)
    if " ".join(str(message["Subject"] or "").split()) != SUBJECT:
        return None
    html = message.get_body(("html",))
    if html is None:
        raise AlertError("transaction alert has no HTML part")
    lines = html_lines(html.get_content())
    return Alert(
        account=ACCOUNT,
        date=parse_date(_field(lines, "Date:")),
        merchant=_field(lines, "Merchant:"),
        amount=Decimal(0) - parse_amount(_field(lines, "Amount:")),
        message_id=message_id(message),
        received=received(message),
    )


def _field(lines: list[str], label: str) -> str:
    values = [line.removeprefix(label).strip() for line in lines if line.startswith(label)]
    if len(values) != 1 or not values[0]:
        raise AlertError(f"expected one {label!r} line in the alert, found {values}")
    return values[0]
