import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally.importers import ParseError
from tally.importers.bofa import clean_merchant, event_date, load, parse, parse_statement
from tally.transaction import Transaction

FIXTURE = Path(__file__).parent / "fixtures" / "bofa_checking.csv"


def fixture_text() -> str:
    return FIXTURE.read_text(newline="")


def make_file(rows: list[str], beginning: str = "100.00", ending: str | None = None) -> str:
    """A small export whose summary agrees with the given rows unless told otherwise."""
    amounts = [Decimal(row.split('","')[1].replace(",", "")) for row in rows]
    credits = sum((a for a in amounts if a > 0), Decimal(0))
    debits = sum((a for a in amounts if a < 0), Decimal(0))
    if ending is None:
        ending = f"{Decimal(beginning) + credits + debits:.2f}"
    return "\r\n".join(
        [
            "Description,,Summary Amt.",
            f'Beginning balance as of 03/01/2026,,"{beginning}"',
            f'Total credits,,"{credits:.2f}"',
            f'Total debits,,"{debits:.2f}"',
            f'Ending balance as of 03/31/2026,,"{ending}"',
            "",
            "Date,Description,Amount,Running Bal.",
            f'03/01/2026,Beginning balance as of 03/01/2026,,"{beginning}"',
            *rows,
            "",
        ]
    )


def test_fixture_has_crlf_line_endings() -> None:
    assert b"\r\n" in FIXTURE.read_bytes()


def test_parses_every_row() -> None:
    transactions = load(FIXTURE)
    assert len(transactions) == 13
    assert {t.account for t in transactions} == {"bofa_checking"}


def test_first_row_in_full() -> None:
    assert load(FIXTURE)[0] == Transaction(
        account="bofa_checking",
        date=datetime.date(2026, 3, 2),
        posted=datetime.date(2026, 3, 2),
        description="Zelle payment from ALEX SAMPLE Conf# QX7RT2LMP",
        merchant="Zelle payment from ALEX SAMPLE",
        amount=Decimal("250.00"),
        bank_category=None,
        balance=Decimal("1450.00"),
    )


def test_amounts_keep_bank_sign_and_thousands() -> None:
    amounts = [t.amount for t in load(FIXTURE)]
    assert Decimal("3456.78") in amounts
    assert Decimal("-1250.00") in amounts
    assert sum(amounts) == Decimal("498.53")


def test_card_purchase_uses_the_day_it_happened() -> None:
    purchase = next(t for t in load(FIXTURE) if t.merchant.startswith("MAPLE MARKET"))
    assert purchase.date == datetime.date(2026, 3, 7)
    assert purchase.posted == datetime.date(2026, 3, 9)


def test_running_balance_is_kept() -> None:
    assert load(FIXTURE)[-1].balance == Decimal("1698.53")


def test_identical_payments_are_all_kept() -> None:
    venmo = [t for t in load(FIXTURE) if t.merchant == "VENMO"]
    assert [t.amount for t in venmo] == [Decimal("-25.00"), Decimal("-25.00")]


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
    ],
)
def test_event_date(description: str, posted: datetime.date, expected: datetime.date) -> None:
    assert event_date(description, posted) == expected


def test_accepts_lf_line_endings_and_byte_order_mark() -> None:
    text = "﻿" + fixture_text().replace("\r\n", "\n")
    assert parse(text) == load(FIXTURE)


def test_no_transactions() -> None:
    assert parse(make_file([])) == []


def test_make_file_helper_builds_a_valid_file() -> None:
    [transaction] = parse(make_file(['03/02/2026,"SHOP","-1,000.00","-900.00"']))
    assert transaction.amount == Decimal("-1000.00")


def test_wrong_running_balance_is_rejected() -> None:
    text = fixture_text().replace('"-42.10","2,489.68"', '"-42.01","2,489.68"')
    with pytest.raises(ParseError, match=r"line 14: running balance 2489.68 should be 2489.77"):
        parse(text)


def test_dropped_row_is_rejected() -> None:
    lines = fixture_text().split("\r\n")
    del lines[12]
    with pytest.raises(ParseError, match="running balance"):
        parse("\r\n".join(lines))


def test_dropped_last_row_is_caught_by_summary() -> None:
    lines = fixture_text().rstrip("\r\n").split("\r\n")
    with pytest.raises(ParseError, match="total debits is -3133.00 but the summary says -3208.25"):
        parse("\r\n".join(lines[:-1]))


def test_ending_balance_must_match_summary() -> None:
    text = make_file(['03/02/2026,"SHOP","-1.00","99.00"'], ending="98.00")
    with pytest.raises(ParseError, match="ending balance is 99.00 but the summary says 98.00"):
        parse(text)


def test_beginning_balance_must_match_summary() -> None:
    opening = ',Beginning balance as of 03/01/2026,,"1,200.00"'
    text = fixture_text().replace(opening, opening.replace("1,200.00", "1,201.00"))
    with pytest.raises(ParseError, match="line 8: beginning balance 1201.00"):
        parse(text)


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ParseError, match="empty"):
        parse("")


def test_discover_file_is_rejected() -> None:
    with pytest.raises(ParseError, match="line 1: expected summary header"):
        parse("Trans. Date,Post Date,Description,Amount,Category\r\n")


def test_truncated_file_is_rejected() -> None:
    text = "\r\n".join(fixture_text().split("\r\n")[:3])
    with pytest.raises(ParseError, match="file ends early"):
        parse(text)


def test_unexpected_summary_row_is_rejected() -> None:
    text = fixture_text().replace("Total credits", "Total deposits")
    with pytest.raises(ParseError, match="line 3: expected summary row"):
        parse(text)


def test_missing_transaction_header_is_rejected() -> None:
    text = fixture_text().replace("Date,Description,Amount,Running Bal.", "Date,Description")
    with pytest.raises(ParseError, match="line 7: expected header"):
        parse(text)


def test_missing_beginning_balance_row_is_rejected() -> None:
    lines = fixture_text().split("\r\n")
    del lines[7]
    with pytest.raises(ParseError, match="line 8: expected the beginning balance row"):
        parse("\r\n".join(lines))


def test_wrong_field_count_reports_line() -> None:
    text = make_file(['03/02/2026,"SHOP","-1.00","99.00"']) + '03/03/2026,"SHOP"\r\n'
    with pytest.raises(ParseError, match="line 10: expected 4 fields, got 2"):
        parse(text)


@pytest.mark.parametrize("bad", ["2026-03-02", "03/32/2026", ""])
def test_bad_date_is_rejected(bad: str) -> None:
    with pytest.raises(ParseError, match="line 9: bad date"):
        parse(make_file([f'{bad},"SHOP","-1.00","99.00"']))


@pytest.mark.parametrize("bad", ["", "-1", "$1.00", "1.000"])
def test_bad_amount_is_rejected(bad: str) -> None:
    text = make_file(['03/02/2026,"SHOP","-1.00","99.00"'])
    text = text.replace(',"SHOP","-1.00"', f',"SHOP","{bad}"')
    with pytest.raises(ParseError, match="line 9: bad amount"):
        parse(text)


def test_statement_gives_the_balances_at_both_ends() -> None:
    statement = parse_statement(fixture_text())
    assert len(statement.transactions) == 13
    assert (statement.start, statement.beginning) == (datetime.date(2026, 3, 1), Decimal("1200.00"))
    assert (statement.end, statement.ending) == (datetime.date(2026, 3, 14), Decimal("1698.53"))


def test_row_outside_the_summary_dates_is_rejected() -> None:
    text = make_file(['04/02/2026,"MAPLE MARKET","-1.00","99.00"'])
    with pytest.raises(ParseError, match="outside the summary's dates"):
        parse(text)
