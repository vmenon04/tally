"""Card alerts the bank exports don't reach yet, counted as provisional transactions.

Between imports, alerts are the only sign of recent purchases, so reports and the ledger
count the alerts that matching calls *waiting*. Each becomes a Transaction with no posted
date, under the negative of its alert ID so it can never be taken for a stored transaction.
Once a CSV reaches an alert, its posted row counts instead.

Alerts the CSVs should show but don't (*not found*) aren't counted: they're usually declined
or reversed purchases, and `tally alerts` lists them.
"""

from collections.abc import Sequence

from tally import matching
from tally.alerts import Alert
from tally.transaction import Transaction


def transactions(
    alerts: Sequence[tuple[int, Alert]], rows: Sequence[Transaction]
) -> list[tuple[int, Transaction]]:
    """The waiting alerts as (negative alert ID, Transaction), oldest purchase first."""
    ids = {alert: alert_id for alert_id, alert in alerts}
    result = matching.match([alert for _, alert in alerts], rows)
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
