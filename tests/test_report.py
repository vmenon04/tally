import datetime
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from tally import report, rules
from tally.cli import main
from tally.transaction import Transaction

FIXTURES = Path(__file__).parent / "fixtures"

RULES = """
categories = ["Rent + fees", "Groceries", "Eating out / dates"]

[[rule]]
merchant = "PAYROLL"
category = "Income"

[[rule]]
match = "BROKERAGE"
category = "Investing"

[[rule]]
match = "SAVINGS"
category = "Savings transfer"

[[rule]]
match = "CARD PAYMENT"
category = "Card payment"

[[rule]]
match = "Zelle payment from ROOMMATE"
category = "Rent + fees"

[[rule]]
match = "LANDLORD"
category = "Rent + fees"

[[rule]]
match = "MARKET"
category = "Groceries"
"""


def tx(merchant: str, amount: str, month: int = 3, day: int = 2) -> Transaction:
    return Transaction(
        account="bofa_checking",
        date=datetime.date(2026, month, day),
        posted=datetime.date(2026, month, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def build(transactions: list[Transaction], month: int = 3) -> report.MonthReport:
    return report.build(transactions, rules.parse(RULES), 2026, month)


def test_spending_is_positive_and_paybacks_lower_it() -> None:
    r = build([tx("LANDLORD", "-1000.00"), tx("Zelle payment from ROOMMATE", "400.00")])
    assert r.spending["Rent + fees"] == Decimal("600.00")


def test_every_category_listed_in_order_even_when_empty() -> None:
    r = build([tx("MAPLE MARKET", "-12.34")])
    assert list(r.spending) == ["Rent + fees", "Groceries", "Eating out / dates"]
    assert r.spending == {
        "Rent + fees": Decimal(0),
        "Groceries": Decimal("12.34"),
        "Eating out / dates": Decimal(0),
    }
    assert r.total_spending == Decimal("12.34")


def test_not_spending_kinds_are_kept_out_of_spending() -> None:
    r = build(
        [
            tx("PAYROLL", "3000.00"),
            tx("BROKERAGE TRANSFER", "-500.00"),
            tx("SAVINGS TRANSFER", "-50.00"),
            tx("CARD PAYMENT", "-800.00"),
        ]
    )
    assert r.total_spending == 0
    assert (r.income, r.investing, r.savings) == (
        Decimal("3000.00"),
        Decimal("500.00"),
        Decimal("50.00"),
    )


def test_only_the_month_counts_but_investing_totals_the_year() -> None:
    r = build(
        [
            tx("BROKERAGE", "-100.00", month=1),
            tx("BROKERAGE", "-200.00", month=3),
            tx("BROKERAGE", "-400.00", month=4),
            tx("MAPLE MARKET", "-1.00", month=2),
            tx("MAPLE MARKET", "-2.00", month=3),
        ]
    )
    assert r.investing == Decimal("200.00")
    assert r.investing_year_to_date == Decimal("300.00")
    assert r.spending["Groceries"] == Decimal("2.00")


def test_previous_year_does_not_count() -> None:
    old = Transaction(
        account="bofa_checking",
        date=datetime.date(2025, 3, 2),
        posted=datetime.date(2025, 3, 2),
        description="BROKERAGE",
        merchant="BROKERAGE",
        amount=Decimal("-100.00"),
    )
    assert build([old]).investing_year_to_date == 0


def test_uncategorized_are_listed_by_date() -> None:
    late, early = tx("MYSTERY", "-3.00", day=20), tx("ODD SHOP", "-4.00", day=5)
    r = build([late, early, tx("MAPLE MARKET", "-1.00")])
    assert r.uncategorized == [early, late]


def test_format() -> None:
    r = build(
        [
            tx("LANDLORD", "-1500.00"),
            tx("MAPLE MARKET", "-12.34"),
            tx("PAYROLL", "3000.00"),
            tx("BROKERAGE", "-500.00"),
            tx("MYSTERY", "-3.00", day=20),
        ]
    )
    assert report.format_report(r) == "\n".join(
        [
            "March 2026",
            "",
            "Spending",
            "  Rent + fees              $1,500.00",
            "  Groceries                   $12.34",
            "  Eating out / dates           $0.00",
            "  Total                    $1,512.34",
            "",
            "Not spending",
            "  Income                   $3,000.00",
            "  Investing (Roth IRA)       $500.00   2026 so far: $500.00",
            "  Savings transfers            $0.00",
            "",
            "Uncategorized (1)",
            "  2026-03-20  bofa_checking       -$3.00  MYSTERY",
        ]
    )


def test_negative_spending_is_shown_with_a_minus() -> None:
    r = build([tx("Zelle payment from ROOMMATE", "400.00")])
    assert "  Rent + fees               -$400.00" in report.format_report(r)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    return data


def test_report_command(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import", str(FIXTURES / "bofa_checking.csv")]) == 0
    (data / "rules.toml").write_text(RULES)
    capsys.readouterr()
    assert main(["report", "2026-03"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("March 2026\n")
    assert "Uncategorized (" in out


def test_report_without_database(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", "2026-03"]) == 1
    assert "Nothing imported yet" in capsys.readouterr().err
    assert not (data / "tally.db").exists()


def test_report_with_bad_rules(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import", str(FIXTURES / "bofa_checking.csv")]) == 0
    assert main(["report", "2026-03"]) == 1
    assert "rules.toml: no rules file at" in capsys.readouterr().err
    shutil.copyfile(FIXTURES / "bofa_checking.csv", data / "rules.toml")
    assert main(["report", "2026-03"]) == 1
    assert "rules.toml: not valid TOML" in capsys.readouterr().err


@pytest.mark.parametrize("month", ["2026-3", "2026-13", "march", "2026-00"])
def test_report_rejects_bad_month(month: str) -> None:
    with pytest.raises(SystemExit):
        main(["report", month])
