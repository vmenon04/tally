import dataclasses
import datetime
from decimal import Decimal

import pytest

from tally.alerts import Alert
from tally.matching import Match, match, name_words, names_agree
from tally.transaction import Transaction


def alert(
    day: int, amount: str, merchant: str = "MAPLE MARKET #0412", account: str = "discover"
) -> Alert:
    return Alert(
        account=account,
        date=datetime.date(2026, 3, day),
        merchant=merchant,
        amount=Decimal(amount),
        message_id=f"<{day}{amount}{merchant}@example.com>",
        received=datetime.datetime(2026, 3, day, 12, tzinfo=datetime.UTC),
    )


def tx(
    day: int,
    amount: str,
    merchant: str = "MAPLE MARKET #0412 SPRINGFIELD IL",
    account: str = "discover",
    posted_after: int = 0,
) -> Transaction:
    return Transaction(
        account=account,
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day + posted_after),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def matched(alerts: list[Alert], rows: list[Transaction]) -> list[tuple[Alert, Transaction]]:
    return [(m.alert, m.transaction) for m in match(alerts, rows).matches]


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("WAL-MART #1234", ["WALMART"]),
        ("WALMART STORE 01234 SPRINGFIELD IL", ["WALMART", "STORE", "SPRINGFIELD", "IL"]),
        ("SQ *JOE'S CAFE", ["SQ", "JOES", "CAFE"]),
        ("SPO*MAPLECAFE", ["SPO", "MAPLECAFE"]),
        ("TICKETS -5551234567 ,IL", ["TICKETS", "IL"]),
        ("#1234 5678", []),
        ("MBTA-550012345678-1234", ["MBTA"]),
        ("7-ELEVEN 34908", ["ELEVEN"]),
    ],
)
def test_name_words(name: str, words: list[str]) -> None:
    assert name_words(name) == words


@pytest.mark.parametrize(
    ("alert_name", "posted_name", "agree"),
    [
        ("MAPLE MARKET #0412", "MAPLE MARKET #0412 SPRINGFIELD IL", True),
        ("WAL-MART #1234", "WALMART STORE 01234 SPRINGFIELD IL", True),
        ("MAPLE GRILL ONLINE", "MAPLE GRILL 0412 SPRINGFIELD IL", True),
        ("SQ *MAPLE CAFE", "SQ *MAPLE CAFE SPRINGFIELD IL", True),
        ("SQ *MAPLE CAFE", "SQ *OAK BAKERY SPRINGFIELD IL", False),
        ("SPO*MAPLECAFE", "MAPLECAFE SPRINGFIELD IL", True),
        ("PIZZA PLACE", "CAFE BADGEPAY 555-123-4567 MA", False),
        ("#0412", "#0412 SPRINGFIELD IL", False),
        ("MBTA-550012345678-1234", "MBTA-550012345678 BOSTON MA", True),
        ("7-ELEVEN 34908", "7-ELEVEN 34908 SPRINGFIELD IL", True),
    ],
)
def test_names_agree(alert_name: str, posted_name: str, agree: bool) -> None:
    assert names_agree(alert_name, posted_name) is agree


def test_posted_row_explains_its_alert() -> None:
    a, t = alert(12, "-42.10"), tx(12, "-42.10")
    assert match([a], [t]).matches == [Match(a, t, by_amount_only=False)]


def test_same_amount_at_another_merchant_days_earlier_is_not_a_match() -> None:
    # The case that showed the first version was too loose.
    a = alert(18, "-10.00", "PIZZA PLACE")
    assert matched([a], [tx(13, "-10.00", "CAFE BADGEPAY 555-123-4567 MA")]) == []


def test_tip_is_matched() -> None:
    a, t = alert(12, "-11.80"), tx(12, "-13.45")
    assert matched([a], [t]) == [(a, t)]


def test_more_than_a_tip_is_not() -> None:
    assert matched([alert(12, "-10.00")], [tx(12, "-13.01")]) == []


def test_posted_amount_lower_than_alert_is_not_a_match() -> None:
    assert matched([alert(12, "-10.00")], [tx(12, "-9.99")]) == []


def test_a_round_hold_matches_what_it_settled_for() -> None:
    a = alert(10, "-200.00", "MAPLE GAS #0469 Outside")
    t = tx(10, "-35.04", "MAPLE GAS #0469 OUTSIDE SPRINGFIELD IL")
    assert matched([a], [t]) == [(a, t)]


def test_a_hold_needs_its_merchant_and_whole_dollars_from_50() -> None:
    assert matched([alert(12, "-200.00")], [tx(12, "-35.04", "OAK BAKERY SPRINGFIELD IL")]) == []
    assert matched([alert(12, "-49.00")], [tx(12, "-35.04")]) == []
    assert matched([alert(12, "-200.50")], [tx(12, "-35.04")]) == []


def test_one_dollar_authorization_matches_any_amount() -> None:
    a = alert(12, "-1.00", "MAPLE GAS #0412")
    t = tx(13, "-48.17", "MAPLE GAS 0412 SPRINGFIELD IL")
    assert matched([a], [t]) == [(a, t)]


def test_refund_does_not_match_a_purchase() -> None:
    assert matched([alert(12, "-1.00")], [tx(12, "1.00")]) == []


def test_deposit_matches_its_posted_row() -> None:
    # The alert names the payer; the CSV adds the ACH details after it.
    a = alert(20, "2345.67", "Acme Payroll 123456", "bofa_checking")
    t = tx(20, "2345.67", "Acme Payroll DES:123456 ID:0000 INDN:Someone PPD", "bofa_checking")
    assert matched([a], [t]) == [(a, t)]


def test_deposit_needs_the_exact_amount() -> None:
    # No tips or holds on money in, and a $1.00 deposit is just a dollar.
    a = alert(20, "2345.67", "Acme Payroll 123456", "bofa_checking")
    for amount in ("2345.68", "2600.00"):
        t = tx(20, amount, "Acme Payroll DES:123456 PPD", "bofa_checking")
        assert matched([a], [t]) == []
    one = alert(20, "1.00", "Acme Payroll 123456", "bofa_checking")
    assert matched([one], [tx(20, "48.17", "Acme Payroll DES:123456 PPD", "bofa_checking")]) == []


def test_refund_is_matched_by_its_posting_date() -> None:
    # The alert gives the day the refund posted; the CSV dates it days earlier, by the merchant.
    a = alert(16, "16.06")
    t = tx(10, "16.06", posted_after=6)
    assert matched([a], [t]) == [(a, t)]
    assert matched([a], [tx(16, "16.06", posted_after=4)]) == []  # posted too late


def test_two_identical_charges_with_one_posted() -> None:
    first = alert(12, "-4.09")
    second = dataclasses.replace(first, message_id="<second@example.com>")
    t = tx(12, "-4.09")
    result = match([first, second], [t])
    assert [(m.alert, m.transaction) for m in result.matches] == [(first, t)]
    assert result.waiting == [second]


def test_date_window() -> None:
    a = alert(12, "-42.10")
    assert matched([a], [tx(11, "-42.10")]) != []  # an alert sent after midnight
    assert matched([a], [tx(15, "-42.10")]) != []
    assert matched([a], [tx(10, "-42.10")]) == []
    assert matched([a], [tx(16, "-42.10")]) == []


def test_other_account_does_not_match() -> None:
    assert matched([alert(12, "-42.10")], [tx(12, "-42.10", account="bofa_checking")]) == []


def test_exact_amount_with_another_name_matches_by_amount_only() -> None:
    a = alert(12, "-23.45", "MAPLE TICKETS -5551234567 ,IL")
    t = tx(12, "-23.45", "MPL*TIX 5551234567 IL")
    assert match([a], [t]).matches == [Match(a, t, by_amount_only=True)]


def test_amount_only_needs_a_single_candidate() -> None:
    a = alert(12, "-23.45", "MAPLE TICKETS")
    rows = [tx(12, "-23.45", "MPL*TIX IL"), tx(13, "-23.45", "OAK BAKERY IL")]
    assert matched([a], rows) == []


def test_amount_only_needs_the_exact_amount() -> None:
    assert matched([alert(12, "-11.80", "MAPLE TICKETS")], [tx(12, "-13.45", "MPL*TIX")]) == []


def test_agreeing_name_is_preferred_over_a_closer_amount() -> None:
    a = alert(12, "-10.00")
    named, other = tx(12, "-11.00"), tx(12, "-10.00", "OAK BAKERY SPRINGFIELD IL")
    assert matched([a], [other, named]) == [(a, named)]


def test_closest_amount_wins_across_alerts() -> None:
    small, large = alert(12, "-10.00"), alert(12, "-12.00")
    exact, tipped = tx(12, "-12.00"), tx(12, "-12.50")
    # Both rows fit both alerts. Going oldest first, the -10.00 alert would take the -12.00
    # row (its closest) and push the exact pair apart; best first keeps it.
    assert matched([small, large], [exact, tipped]) == [(small, tipped), (large, exact)]


def test_waiting_until_the_csv_covers_the_alert() -> None:
    a = alert(12, "-42.10")
    assert match([a], [tx(14, "-5.00", "OAK BAKERY")]).waiting == [a]


def test_not_found_once_the_csv_covers_three_days_past_it() -> None:
    a = alert(12, "-42.10")
    result = match([a], [tx(15, "-5.00", "OAK BAKERY")])
    assert (result.not_found, result.waiting) == ([a], [])


def test_coverage_counts_posting_dates_and_only_the_alerts_account() -> None:
    a = alert(12, "-42.10")
    assert match([a], [tx(13, "-5.00", "OAK BAKERY", posted_after=2)]).not_found == [a]
    assert match([a], [tx(20, "-5.00", "OAK BAKERY", account="bofa_checking")]).waiting == [a]
