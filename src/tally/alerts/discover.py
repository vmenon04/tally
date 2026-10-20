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

import datetime
import re
import time
from decimal import Decimal

from tally.alerts import Alert, AlertError, check_sender, html_lines, message_id, read, received

ACCOUNT = "discover"
DOMAIN = "discover.com"
SUBJECT = "Transaction Alert"

_AMOUNT = re.compile(r"\$((\d{1,3}(,\d{3})+|\d+)\.\d{2})")


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
        date=_date(_field(lines, "Date:")),
        merchant=_field(lines, "Merchant:"),
        amount=Decimal(0) - _amount(_field(lines, "Amount:")),
        message_id=message_id(message),
        received=received(message),
    )


def _field(lines: list[str], label: str) -> str:
    values = [line.removeprefix(label).strip() for line in lines if line.startswith(label)]
    if len(values) != 1 or not values[0]:
        raise AlertError(f"expected one {label!r} line in the alert, found {values}")
    return values[0]


def _date(text: str) -> datetime.date:
    try:
        parsed = time.strptime(text, "%B %d, %Y")
        return datetime.date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    except ValueError:
        raise AlertError(f"bad date {text!r}") from None


def _amount(text: str) -> Decimal:
    match = _AMOUNT.fullmatch(text)
    if match is None:
        raise AlertError(f"bad amount {text!r}")
    return Decimal(match.group(1).replace(",", ""))
