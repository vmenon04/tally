"""Pending transactions: purchases the bank knows about that haven't posted yet.

SimpleFIN lists them for each account it fills, and each sync stores them as Alerts (they
were once read from the banks' alert emails, hence the name). One is provisional: its amount
can differ from what finally posts (a tip added, a gas station hold), so it counts only
until a posted row explains it (see matching) or SimpleFIN stops listing it.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Alert:
    account: str
    date: datetime.date  # the day of the purchase, or when money in posted
    merchant: str
    amount: Decimal  # negative = money out
    message_id: str  # unique per pending transaction, so seeing it again is noticed
    received: datetime.datetime  # when SimpleFIN last listed it as pending


# The start of the message_id of a pending transaction SimpleFIN reported.
BANK_PENDING = "simplefin:"
