"""How Bank of America words checking account rows, so a merchant reads the same each time.

Its CSV export and SimpleFIN word the same purchase differently ("SHOP 03/07 PURCHASE
SPRINGFIELD IL" and "CHECKCARD 0307 SHOP SPRINGFIELD IL XXXXX9462XXXXXXXXXX3166"); both are
read into the same purchase date and the same merchant name, so rules keep working.
"""

import datetime
import re

ACCOUNT = "bofa_checking"

# Debit card purchases and ATM/branch withdrawals include the day they happened, which
# can be earlier than the posting date: "SHOP 03/07 PURCHASE ..." or "ATM 03/08 #0001 WITHDRWL".
# SimpleFIN gives a card purchase as "CHECKCARD 1018 SHOP RALEIGH NC XXXXX9462XXXXXXXXXX3166",
# and masks the reference numbers: "#XXXXX2887 WITHDRWL".
_EVENT_DATE = re.compile(
    r" (\d{2})/(\d{2}) (?=PURCHASE\b|#[\dX]+ WITHDRWL\b)|^(?:CHECKCARD|PMNT SENT) (\d{2})(\d{2}) "
)
# Per-row noise, removed to get a merchant name that stays the same across rows.
_NOISE = [
    (re.compile(r" \d{2}/\d{2} PURCHASE\b"), ""),
    (re.compile(r" \d{2}/\d{2} #[\dX]+ WITHDRWL\b"), " WITHDRWL"),
    (re.compile(r"^(?:CHECKCARD|PMNT SENT) \d{4} "), ""),
    (re.compile(r" X+\d+X+\d+$"), ""),  # SimpleFIN's masked card and reference numbers
    (re.compile(r" Conf# \S+$"), ""),
    # Transfers between your own BofA accounts: "Online Banking transfer to SAV 1234
    # Confirmation# 5527284505".
    (re.compile(r" Confirmation# ?\S+$"), ""),
    (re.compile(r" FOR \d{2}/\d{2}/\d{2}$"), ""),
]


def event_date(description: str, posted: datetime.date) -> datetime.date:
    """The day a card purchase or withdrawal happened, or the posting date if not given."""
    match = _EVENT_DATE.search(description)
    if match:
        month, day = int(match[1] or match[3]), int(match[2] or match[4])
        # The description has no year: it's the latest such day not after posting.
        for year in (posted.year, posted.year - 1):
            try:
                date = datetime.date(year, month, day)
            except ValueError:
                continue
            if date <= posted:
                return date
    return posted


def clean_merchant(description: str) -> str:
    """Remove the parts of a description that change from row to row with the same payee."""
    description = " ".join(description.split())
    if " DES:" in description:
        # Bank transfers: "VENMO DES:PAYMENT ID:... INDN:... CO ID:... WEB".
        return description.split(" DES:", 1)[0]
    for pattern, replacement in _NOISE:
        description = pattern.sub(replacement, description)
    return description
