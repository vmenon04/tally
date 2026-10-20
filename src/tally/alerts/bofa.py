"""Bank of America "Your debit card was used" emails, for purchases on the checking debit card.

The email is HTML only, laid out as a table with each label in one cell and its value in
the next, so its text reads:

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

Other emails from Bank of America (statements, deposits, offers) are not alerts and give None.
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
SUBJECT = "Your debit card was used"


def parse(raw: bytes) -> Alert | None:
    """The alert in one email from Bank of America, or None if it's another kind of email."""
    message = read(raw)
    check_sender(message, DOMAIN)
    if " ".join(str(message["Subject"] or "").split()) != SUBJECT:
        return None
    html = message.get_body(("html",))
    if html is None:
        raise AlertError("debit card alert has no HTML part")
    lines = html_lines(html.get_content())
    return Alert(
        account=ACCOUNT,
        date=parse_date(_after(lines, "Purchase date")),
        merchant=_after(lines, "Purchase made at"),
        amount=Decimal(0) - parse_amount(_after(lines, "Amount")),
        message_id=message_id(message),
        received=received(message),
    )


def _after(lines: list[str], label: str) -> str:
    """The line after the one that is exactly label."""
    positions = [i for i, line in enumerate(lines) if line == label]
    if len(positions) != 1 or positions[0] + 1 == len(lines):
        raise AlertError(f"expected one {label!r} line followed by its value in the alert")
    return lines[positions[0] + 1]
