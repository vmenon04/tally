import datetime
from decimal import Decimal
from pathlib import Path

from test_entries import OPENS, RULES

from tally import rules
from tally.alerts import Alert
from tally.ledger import book

MAIN = 'include "accounts.beancount"\ninclude "pending.beancount"\n'
# The bank says checking held this at the start of 03/12: nothing before can still post.
ASSERTED = "2026-03-12 balance Assets:BofA:Checking 1200.00 USD\n"


def alert(day: int, merchant: str, amount: str) -> Alert:
    return Alert(
        account="bofa_checking",
        date=datetime.date(2026, 3, day),
        merchant=merchant,
        amount=Decimal(amount),
        message_id=f"simplefin:a:{day}",
        received=datetime.datetime(2026, 3, 20, tzinfo=datetime.UTC),
    )


def ledger(tmp_path: Path) -> book.Ledger:
    (tmp_path / "main.beancount").write_text(MAIN)
    (tmp_path / "accounts.beancount").write_text(OPENS + ASSERTED)
    (tmp_path / "pending.beancount").write_text("")
    return book.load(tmp_path, rules.parse(RULES))


def test_a_pending_purchase_goes_after_the_last_balance_assertion(tmp_path: Path) -> None:
    waiting = [alert(11, "SNACK BAR", "-4.00"), alert(18, "MAPLE MARKET", "-9.00")]
    book.write_pending(ledger(tmp_path), waiting, rules.parse(RULES))
    pending = (tmp_path / "pending.beancount").read_text()
    assert pending.count(" ! ") == 2
    assert '2026-03-12 ! "SNACK BAR"' in pending
    assert "purchased: 2026-03-11" in pending
    assert '2026-03-18 ! "MAPLE MARKET"' in pending
    assert book.check(tmp_path) == []


def test_pending_is_rewritten_only_when_it_changes(tmp_path: Path) -> None:
    found = ledger(tmp_path)
    waiting = [alert(18, "MAPLE MARKET", "-9.00")]
    book.write_pending(found, waiting, rules.parse(RULES))
    pending_file = tmp_path / "pending.beancount"
    before = pending_file.stat().st_mtime_ns
    book.write_pending(book.load(tmp_path, rules.parse(RULES)), waiting, rules.parse(RULES))
    assert pending_file.stat().st_mtime_ns == before
