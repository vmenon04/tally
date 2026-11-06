import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally.importers import ParseError
from tally.importers.fidelity import parse

FIXTURE = Path(__file__).parent / "fixtures" / "fidelity_positions.csv"


def text() -> str:
    return FIXTURE.read_text(encoding="utf-8", newline="")


def test_parses_every_account_and_the_download_date() -> None:
    snapshot = parse(text())
    assert snapshot.date == datetime.date(2026, 3, 20)
    assert [(a.label, a.name) for a in snapshot.accounts] == [
        ("1111", "ROTH IRA"),
        ("2222", "EXAMPLE RET SAVINGS"),
        ("3333", "Example Individual Fidelity Go"),
    ]


def test_money_market_is_cash_and_holdings_keep_cost_basis() -> None:
    roth, savings, go = parse(text()).accounts
    assert roth.cash == Decimal("12.50")
    assert [(h.symbol, h.quantity, h.price, h.cost) for h in roth.holdings] == [
        ("FXAIX", Decimal("10.5"), Decimal("200.00"), Decimal("2000.00")),
        ("ABCD", Decimal("3"), Decimal("150.25"), Decimal("500.00")),
    ]
    assert savings.cash is None
    assert go.cash == Decimal("5.00")


def test_only_the_last_four_of_an_account_number_are_kept() -> None:
    snapshot = parse(text())
    assert "Z00001111" not in repr(snapshot)


def test_no_download_date_is_refused() -> None:
    with pytest.raises(ParseError, match="Date downloaded"):
        parse(text().replace("Date downloaded", "Downloaded"))


def test_bad_cost_basis_is_refused() -> None:
    with pytest.raises(ParseError, match="bad amount"):
        parse(text().replace("$2000.00", "--"))


def test_other_csvs_are_refused() -> None:
    with pytest.raises(ParseError, match="Fidelity positions header"):
        parse("Trans. Date,Post Date,Description,Amount,Category\r\n")
