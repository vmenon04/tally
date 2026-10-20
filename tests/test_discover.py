import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally.importers import ParseError
from tally.importers.discover import clean_merchant, load, parse
from tally.transaction import Transaction

FIXTURE = Path(__file__).parent / "fixtures" / "discover_recent.csv"
HEADER = "Trans. Date,Post Date,Description,Amount,Category\r\n"


def test_fixture_has_crlf_line_endings() -> None:
    # Guards the fixture itself: the real export uses CRLF and we want to test against that.
    assert b"\r\n" in FIXTURE.read_bytes()


def test_parses_every_row() -> None:
    transactions = load(FIXTURE)
    assert len(transactions) == 9
    assert {t.account for t in transactions} == {"discover"}


def test_first_row_in_full() -> None:
    assert load(FIXTURE)[0] == Transaction(
        account="discover",
        date=datetime.date(2026, 3, 12),
        posted=datetime.date(2026, 3, 12),
        description="MAPLE MARKET #0412 SPRINGFIELD ILAPPLE PAY ENDING IN 123409876X",
        merchant="MAPLE MARKET #0412 SPRINGFIELD IL",
        amount=Decimal("-12.34"),
        bank_category="Supermarkets",
    )


def test_purchases_are_negative_and_payments_positive() -> None:
    transactions = load(FIXTURE)
    payment = next(t for t in transactions if t.description == "INTERNET PAYMENT - THANK YOU")
    assert payment.amount == Decimal("1250.00")
    assert all(t.amount < 0 for t in transactions if t is not payment)


def test_amounts_are_exact() -> None:
    purchases = [t.amount for t in load(FIXTURE) if t.amount < 0]
    assert sum(purchases) == Decimal("-122.43")


def test_weekend_purchase_keeps_both_dates() -> None:
    bistro = next(t for t in load(FIXTURE) if t.merchant.startswith("TST*HARBOR BISTRO"))
    assert bistro.date == datetime.date(2026, 3, 7)
    assert bistro.posted == datetime.date(2026, 3, 9)


def test_identical_rows_are_all_kept() -> None:
    noodles = [t for t in load(FIXTURE) if t.merchant == "NOODLE HOUSE 118 SPRINGFIELD IL"]
    assert [t.amount for t in noodles] == [Decimal("-8.25"), Decimal("-8.25"), Decimal("-19.10")]


def test_bank_category_kept_as_written() -> None:
    categories = {t.bank_category for t in load(FIXTURE)}
    assert "Travel/ Entertainment" in categories


@pytest.mark.parametrize(
    ("description", "merchant"),
    [
        (
            "CORNER GROCER #77 LAKEVIEW ORAPPLE PAY ENDING IN 1234",
            "CORNER GROCER #77 LAKEVIEW OR",
        ),
        (
            "CORNER GROCER #77 LAKEVIEW ORAPPLE PAY ENDING IN 123401786R",
            "CORNER GROCER #77 LAKEVIEW OR",
        ),
        (
            "SQ *BLUE KETTLE SPRINGFIELD ILAPPLE PAY ENDING IN 12340009876543210",
            "SQ *BLUE KETTLE SPRINGFIELD IL",
        ),
        (
            "TST*HARBOR BISTRO SPRINGFIELD IL00012345ABCDEFGH1JKL",
            "TST*HARBOR BISTRO SPRINGFIELD IL",
        ),
        ("CAFE BADGEPAY 555-010-0199 MA", "CAFE BADGEPAY 555-010-0199 MA"),
        ("TACO STAND 729283 555-010-0123 NC", "TACO STAND 729283 555-010-0123 NC"),
        ("INTERNET PAYMENT - THANK YOU", "INTERNET PAYMENT - THANK YOU"),
        ("  EXTRA   SPACES  CA ", "EXTRA SPACES CA"),
    ],
)
def test_clean_merchant(description: str, merchant: str) -> None:
    assert clean_merchant(description) == merchant


def test_accepts_lf_line_endings_and_byte_order_mark() -> None:
    text = "﻿" + FIXTURE.read_text().replace("\r\n", "\n")
    assert parse(text) == load(FIXTURE)


def test_header_only_means_no_transactions() -> None:
    assert parse(HEADER) == []


def test_blank_lines_are_skipped() -> None:
    text = HEADER + '03/01/2026,03/02/2026,"SHOP",1.00,"Merchandise"\r\n\r\n\r\n'
    assert len(parse(text)) == 1


def test_empty_category_becomes_none() -> None:
    [transaction] = parse(HEADER + '03/01/2026,03/02/2026,"SHOP",1.00,""\r\n')
    assert transaction.bank_category is None


def test_zero_amount_has_no_negative_sign() -> None:
    [transaction] = parse(HEADER + '03/01/2026,03/02/2026,"SHOP",0.00,"Merchandise"\r\n')
    assert str(transaction.amount) == "0.00"


def test_quoted_amount_with_thousands_separator() -> None:
    [transaction] = parse(HEADER + '03/01/2026,03/02/2026,"SHOP","1,234.56","Merchandise"\r\n')
    assert transaction.amount == Decimal("-1234.56")


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ParseError, match="empty"):
        parse("")


def test_wrong_header_is_rejected() -> None:
    with pytest.raises(ParseError, match="line 1: expected header"):
        parse("Date,Description,Amount,Running Bal.\r\n")


def test_wrong_field_count_reports_line() -> None:
    text = HEADER + '03/01/2026,03/02/2026,"SHOP",1.00,"Merchandise"\r\n03/01/2026,"SHOP",1.00\r\n'
    with pytest.raises(ParseError, match="line 3: expected 5 fields, got 3"):
        parse(text)


@pytest.mark.parametrize("bad", ["2026-03-01", "3/1/2026", "13/01/2026", "02/30/2026", ""])
def test_bad_date_is_rejected(bad: str) -> None:
    with pytest.raises(ParseError, match="line 2: bad date"):
        parse(HEADER + f'{bad},03/02/2026,"SHOP",1.00,"Merchandise"\r\n')


@pytest.mark.parametrize("bad", ["", "1", "1.0", "$1.00", "1.000", "12,34.00", "abc"])
def test_bad_amount_is_rejected(bad: str) -> None:
    with pytest.raises(ParseError, match="line 2: bad amount"):
        parse(HEADER + f'03/01/2026,03/02/2026,"SHOP","{bad}","Merchandise"\r\n')
