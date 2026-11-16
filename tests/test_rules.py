import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally import rules
from tally.transaction import Transaction

CATEGORIES = 'categories = ["Groceries", "Eating out / dates", "Home / apartment stuff"]\n'


def tx(
    merchant: str, amount: str = "-5.00", day: int = 2, account: str = "discover"
) -> Transaction:
    return Transaction(
        account=account,
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def parse(rule_text: str) -> rules.Rules:
    return rules.parse(CATEGORIES + rule_text)


def test_contains_match_ignores_case() -> None:
    r = parse('[[rule]]\nmatch = "noodle"\ncategory = "Eating out / dates"\n')
    assert r.categorize(tx("NOODLE HOUSE 118 SPRINGFIELD IL")) == "Eating out / dates"
    assert r.categorize(tx("MAPLE MARKET")) == "Uncategorized"


def test_exact_merchant_match() -> None:
    r = parse('[[rule]]\nmerchant = "venmo"\ncategory = "Groceries"\n')
    assert r.categorize(tx("VENMO")) == "Groceries"
    assert r.categorize(tx("VENMO CASHOUT")) == "Uncategorized"


def test_first_matching_rule_wins() -> None:
    r = parse(
        "[[rule]]\n"
        'match = "Zelle payment to"\ndate = 2026-03-09\namount = -50.00\n'
        'category = "Home / apartment stuff"\n'
        '[[rule]]\nmatch = "Zelle"\ncategory = "Eating out / dates"\n'
    )
    assert r.categorize(tx("Zelle payment to SAM", "-50.00", day=9)) == "Home / apartment stuff"
    assert r.categorize(tx("Zelle payment to SAM", "-50.00", day=10)) == "Eating out / dates"
    assert r.categorize(tx("Zelle payment to SAM", "-50.01", day=9)) == "Eating out / dates"


def test_account_narrows_a_rule() -> None:
    r = parse('[[rule]]\nmatch = "SHOP"\naccount = "bofa_checking"\ncategory = "Groceries"\n')
    assert r.categorize(tx("SHOP", account="bofa_checking")) == "Groceries"
    assert r.categorize(tx("SHOP", account="discover")) == "Uncategorized"


def test_whole_number_amount() -> None:
    r = parse('[[rule]]\nmatch = "SHOP"\namount = -50\ncategory = "Groceries"\n')
    assert r.categorize(tx("SHOP", "-50.00")) == "Groceries"


def test_amount_is_exact_not_float() -> None:
    r = parse('[[rule]]\nmatch = "SHOP"\namount = -0.10\ncategory = "Groceries"\n')
    assert r.rules[0].amount == Decimal("-0.10")
    assert r.categorize(tx("SHOP", "-0.10")) == "Groceries"


def test_not_spending_kinds_are_always_allowed() -> None:
    r = parse('[[rule]]\nmerchant = "PAYROLL"\ncategory = "Income"\n')
    assert r.categorize(tx("PAYROLL", "100.00")) == "Income"


def test_categories_keep_their_order() -> None:
    assert parse("").categories == ("Groceries", "Eating out / dates", "Home / apartment stuff")


def test_load_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(rules.RulesError, match="no rules file at"):
        rules.load(tmp_path / "rules.toml")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("categories = [", "not valid TOML"),
        ('[[rule]]\nmatch = "A"\ncategory = "Groceries"\n', "categories must be a non-empty"),
        ('categories = ["A"]\ncolour = "red"\n', "unknown top-level keys"),
        ('categories = ["A", "A"]\n', "duplicates"),
        ('categories = ["Income"]\n', "'Income' is built in"),
        ('categories = [""]\n', "non-empty text"),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\ncategory = "Grocery"\n',
            "rule 1: category 'Grocery'",
        ),
        (CATEGORIES + '[[rule]]\ncategory = "Groceries"\n', "rule 1: needs exactly one of"),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\nmerchant = "A"\ncategory = "Groceries"\n',
            "needs exactly one of",
        ),
        (CATEGORIES + '[[rule]]\nmatch = " "\ncategory = "Groceries"\n', "non-empty text"),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\ncategory = "Groceries"\nacount = "x"\n',
            "rule 1: unknown keys",
        ),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\naccount = 5\ncategory = "Groceries"\n',
            "account must be a tally-account name",
        ),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\ndate = "2026-03-09"\ncategory = "Groceries"\n',
            "date must be written like",
        ),
        (
            CATEGORIES
            + '[[rule]]\nmatch = "A"\ndate = 2026-03-09T10:00:00\ncategory = "Groceries"\n',
            "date must be written like",
        ),
        (
            CATEGORIES + '[[rule]]\nmatch = "A"\namount = "-5"\ncategory = "Groceries"\n',
            "amount must be a number",
        ),
        (
            CATEGORIES
            + '[[rule]]\nmatch = "A"\ncategory = "Groceries"\n'
            + '[[rule]]\nmatch = "B"\ncategory = "Nope"\n',
            "rule 2:",
        ),
    ],
)
def test_bad_rules_file_is_rejected(text: str, error: str) -> None:
    with pytest.raises(rules.RulesError, match=error):
        rules.parse(text)
