import datetime

import pytest

from tally.importers.bofa import clean_merchant, event_date


@pytest.mark.parametrize(
    ("description", "merchant"),
    [
        (
            "VENMO DES:PAYMENT ID:1000000000001 INDN:JORDAN EXAMPLE CO ID:2000000002 WEB",
            "VENMO",
        ),
        (
            "FID BKG SVC LLC DES:MONEYLINE ID:400000000 AB1CD INDN:JORDAN EXAMPLE "
            "CO ID:4000000004 PPD",
            "FID BKG SVC LLC",
        ),
        ("Zelle payment to SAM PLACEHOLDER Conf# zz91kq0ab", "Zelle payment to SAM PLACEHOLDER"),
        (
            "Online Banking transfer to SAV 1234 Confirmation# 1000000001",
            "Online Banking transfer to SAV 1234",
        ),
        ("MAPLE MARKET 03/07 PURCHASE SPRINGFIELD IL", "MAPLE MARKET SPRINGFIELD IL"),
        (
            "BKOFAMERICA ATM 03/08 #000005678 WITHDRWL SPRINGFIELD MAIN SPRINGFIELD IL",
            "BKOFAMERICA ATM WITHDRWL SPRINGFIELD MAIN SPRINGFIELD IL",
        ),
        (
            "BKOFAMERICA BC 03/03 #000001234 WITHDRWL 100 Main St Springfield IL",
            "BKOFAMERICA BC WITHDRWL 100 Main St Springfield IL",
        ),
        (
            "KEEP THE CHANGE TRANSFER TO ACCT 1111 FOR 03/09/26",
            "KEEP THE CHANGE TRANSFER TO ACCT 1111",
        ),
        ("STORE #123  03/07 PURCHASE  IL", "STORE #123 IL"),
        # The same purchase and withdrawal as SimpleFIN gives them.
        (
            "CHECKCARD 0307 MAPLE MARKET SPRINGFIELD IL XXXXX9462XXXXXXXXXX3166",
            "MAPLE MARKET SPRINGFIELD IL",
        ),
        (
            "BKOFAMERICA ATM 03/08 #XXXXX5678 WITHDRWL SPRINGFIELD MAIN SPRINGFIELD IL",
            "BKOFAMERICA ATM WITHDRWL SPRINGFIELD MAIN SPRINGFIELD IL",
        ),
    ],
)
def test_clean_merchant(description: str, merchant: str) -> None:
    assert clean_merchant(description) == merchant


@pytest.mark.parametrize(
    ("description", "posted", "expected"),
    [
        ("SHOP 03/07 PURCHASE IL", datetime.date(2026, 3, 9), datetime.date(2026, 3, 7)),
        ("SHOP 12/31 PURCHASE IL", datetime.date(2027, 1, 2), datetime.date(2026, 12, 31)),
        ("ATM 03/08 #0001 WITHDRWL IL", datetime.date(2026, 3, 9), datetime.date(2026, 3, 8)),
        ("VENMO DES:PAYMENT ID:1", datetime.date(2026, 3, 9), datetime.date(2026, 3, 9)),
        ("SHOP 13/45 PURCHASE IL", datetime.date(2026, 3, 9), datetime.date(2026, 3, 9)),
        ("SHOP 02/29 PURCHASE IL", datetime.date(2029, 3, 1), datetime.date(2028, 2, 29)),
        ("CHECKCARD 0307 SHOP IL XX1X2", datetime.date(2026, 3, 9), datetime.date(2026, 3, 7)),
        ("ATM 03/08 #XXX01 WITHDRWL IL", datetime.date(2026, 3, 9), datetime.date(2026, 3, 8)),
    ],
)
def test_event_date(description: str, posted: datetime.date, expected: datetime.date) -> None:
    assert event_date(description, posted) == expected
