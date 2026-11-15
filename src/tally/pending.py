"""Pending purchases, counted as provisional transactions until they post.

Reports and the ledger count the alerts (pending purchases) that matching calls *waiting*.
Each becomes a Transaction with no posted date, under the negative of its alert ID so it can
never be taken for a stored transaction. Once its posted row arrives, that counts instead.

Ones that should have posted by now but haven't (*not found*), or that SimpleFIN no longer
lists (*superseded*), aren't counted: they're declined, reversed or released.
"""

import datetime
from collections.abc import Sequence

from tally import matching
from tally.alerts import Alert
from tally.transaction import Transaction


def transactions(
    alerts: Sequence[tuple[int, Alert]],
    rows: Sequence[Transaction],
    fetched: dict[str, datetime.datetime] | None = None,
) -> list[tuple[int, Transaction]]:
    """The waiting alerts as (negative alert ID, Transaction), oldest purchase first."""
    ids = {alert: alert_id for alert_id, alert in alerts}
    result = matching.match([alert for _, alert in alerts], rows, fetched)
    return [(-ids[a], as_transaction(a)) for a in result.waiting]


def posted(
    alerts: Sequence[tuple[int, Alert]], rows: Sequence[Transaction]
) -> list[tuple[Transaction, Transaction]]:
    """Each alert a CSV has reached, as its pending transaction, with the row that replaced it."""
    result = matching.match([alert for _, alert in alerts], rows)
    return [(as_transaction(m.alert), m.transaction) for m in result.matches]


def is_pending(transaction_id: int) -> bool:
    return transaction_id < 0


def as_transaction(a: Alert) -> Transaction:
    return Transaction(
        account=a.account,
        date=a.date,
        posted=None,
        description=a.merchant,
        merchant=a.merchant,
        amount=a.amount,
    )
