import datetime
from decimal import Decimal

from tally import pending
from tally.alerts import Alert
from tally.transaction import Transaction


def alert(day: int, amount: str, merchant: str = "MAPLE MARKET #0412") -> Alert:
    return Alert(
        account="discover",
        date=datetime.date(2026, 3, day),
        merchant=merchant,
        amount=Decimal(amount),
        message_id=f"<{day}{amount}@example.com>",
        received=datetime.datetime(2026, 3, day, 12, tzinfo=datetime.UTC),
    )


def tx(day: int, amount: str, merchant: str = "MAPLE MARKET #0412 SPRINGFIELD IL") -> Transaction:
    return Transaction(
        account="discover",
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def test_only_waiting_alerts_count() -> None:
    posted = alert(10, "-42.10")
    missing = alert(11, "-9.99", "CORNER CAFE")  # the CSV reaches the 20th but has no such row
    waiting = alert(19, "-12.00")
    rows = [tx(10, "-42.10"), tx(20, "-5.00", "OTHER SHOP")]
    result = pending.transactions([(1, posted), (2, missing), (3, waiting)], rows)
    assert result == [(-3, pending.as_transaction(waiting))]


def test_every_alert_waits_when_nothing_is_imported() -> None:
    alerts = [(7, alert(1, "-1.00")), (8, alert(2, "-2.00"))]
    assert [tid for tid, _ in pending.transactions(alerts, [])] == [-7, -8]


def test_as_transaction() -> None:
    t = pending.as_transaction(alert(19, "-12.00"))
    assert t == Transaction(
        account="discover",
        date=datetime.date(2026, 3, 19),
        posted=None,
        description="MAPLE MARKET #0412",
        merchant="MAPLE MARKET #0412",
        amount=Decimal("-12.00"),
    )


def test_is_pending() -> None:
    assert pending.is_pending(-1)
    assert not pending.is_pending(1)
