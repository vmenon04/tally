import datetime
import re
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.parser import printer

from tally import rules
from tally.importers import ParseError
from tally.ledger import accounts, entries, importers

FIXTURES = Path(__file__).parent / "fixtures"
BOFA = FIXTURES / "bofa_checking.csv"
DISCOVER = FIXTURES / "discover_recent.csv"

# Made-up accounts in the shape of the real ledger's accounts.beancount.
OPENS = """
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:BofA:Checking USD
  tally-account: "bofa_checking"
2026-01-01 open Liabilities:Discover:Card USD
  tally-account: "discover"
2026-01-01 open Assets:Transfers:Discover USD
  category: "Card payment"
2026-01-01 open Assets:BofA:Savings USD
  category: "Savings transfer"
  merchant-contains: "KEEP THE CHANGE"
2026-01-01 open Assets:CapitalOne:Savings USD
  category: "Savings transfer"
2026-01-01 open Assets:Fidelity:Roth:Cash USD
  category: "Investing"
2026-01-01 open Income:Salary
  category: "Income"
2026-01-01 open Expenses:Groceries
  category: "Groceries"
2026-01-01 open Expenses:EatingOut
  category: "Eating out / dates"
2026-01-01 open Expenses:Personal
  category: "Personal / other"
2026-01-01 open Expenses:Uncategorized
  category: "Uncategorized"

2026-02-28 * "Opening balance"
  Assets:BofA:Checking  1200.00 USD
  Equity:Opening-Balances
"""

RULES = """
categories = ["Groceries", "Eating out / dates", "Personal / other"]

[[rule]]
match = "MAPLE MARKET"
category = "Groceries"

[[rule]]
match = "NOODLE HOUSE"
category = "Eating out / dates"

[[rule]]
merchant = "DISCOVER"
category = "Card payment"

[[rule]]
match = "INTERNET PAYMENT - THANK YOU"
category = "Card payment"

[[rule]]
match = "KEEP THE CHANGE TRANSFER"
category = "Savings transfer"

[[rule]]
merchant = "CAPITAL ONE"
category = "Savings transfer"

[[rule]]
merchant = "FID BKG SVC LLC"
category = "Investing"

[[rule]]
merchant = "EXAMPLE CORP PAYROLL"
category = "Income"
"""


def setup() -> tuple[accounts.Accounts, rules.Rules]:
    opens, errors, _ = loader.load_string(OPENS)
    assert not errors
    found = accounts.from_entries(opens)
    parsed = rules.parse(RULES)
    found.check(parsed.categories)
    return found, parsed


def extract(path: Path) -> list[data.Directive]:
    found, parsed = setup()
    importer = importers.for_text(path.read_text(newline=""), found, parsed)
    return list(importer.extract(str(path), []))


def check(*extracted: list[data.Directive]) -> list[str]:
    """bean-check's errors for the skeleton plus these entries, as written to a file."""
    text = OPENS + "\n".join(printer.format_entry(e) for es in extracted for e in es)
    _, errors, _ = loader.load_string(text)
    return [e.message for e in errors]


def transactions(es: list[data.Directive]) -> list[data.Transaction]:
    return [e for e in es if isinstance(e, data.Transaction)]


def number(posting: data.Posting) -> Decimal | None:
    assert posting.units is not None
    return posting.units.number


def text(t: data.Transaction) -> str:
    return f"{t.payee or ''} {t.narration}"


def test_identify_by_first_line() -> None:
    found, parsed = setup()
    bofa, discover = importers.all_importers(found, parsed)
    assert bofa.identify(str(BOFA)) and not bofa.identify(str(DISCOVER))
    assert discover.identify(str(DISCOVER)) and not discover.identify(str(BOFA))
    assert bofa.account(str(BOFA)) == "Assets:BofA:Checking"


def test_unknown_file_is_refused() -> None:
    found, parsed = setup()
    with pytest.raises(ParseError, match="not a Discover or BofA export"):
        importers.for_text("Date,Amount\n", found, parsed)


def test_bofa_file_passes_bean_check() -> None:
    assert check(extract(BOFA)) == []


def test_bofa_asserts_balance_at_both_ends() -> None:
    balances = [e for e in extract(BOFA) if isinstance(e, data.Balance)]
    assert [(b.date, b.amount.number) for b in balances] == [
        (datetime.date(2026, 3, 1), Decimal("1200.00")),
        (datetime.date(2026, 3, 15), Decimal("1698.53")),
    ]


def test_dated_by_posting_with_purchase_day_kept() -> None:
    maple = next(t for t in transactions(extract(BOFA)) if text(t).startswith("MAPLE"))
    assert maple.date == datetime.date(2026, 3, 9)
    assert maple.meta[entries.PURCHASED_KEY] == datetime.date(2026, 3, 7)
    zelle = transactions(extract(BOFA))[0]
    assert entries.PURCHASED_KEY not in zelle.meta


def test_other_side_follows_the_category() -> None:
    sides = {t.payee: t.postings[1].account for t in transactions(extract(BOFA))}
    assert sides["MAPLE MARKET SPRINGFIELD IL"] == "Expenses:Groceries"
    assert sides["DISCOVER"] == "Assets:Transfers:Discover"
    assert sides["KEEP THE CHANGE TRANSFER TO ACCT 1111"] == "Assets:BofA:Savings"
    assert sides["CAPITAL ONE"] == "Assets:CapitalOne:Savings"
    assert sides["FID BKG SVC LLC"] == "Assets:Fidelity:Roth:Cash"
    assert sides["EXAMPLE CORP PAYROLL"] == "Income:Salary"
    assert sides["REMITLY INC"] == "Expenses:Uncategorized"


def test_discover_purchases_raise_what_is_owed() -> None:
    es = transactions(extract(DISCOVER))
    maple = es[0]
    assert maple.postings[0].account == "Liabilities:Discover:Card"
    assert number(maple.postings[0]) == Decimal("-12.34")
    assert number(maple.postings[1]) == Decimal("12.34")
    bistro = next(t for t in es if "BISTRO" in text(t))
    assert bistro.date == datetime.date(2026, 3, 9)
    assert bistro.meta[entries.PURCHASED_KEY] == datetime.date(2026, 3, 7)


def test_card_payment_sides_cancel_out() -> None:
    zero = "2026-03-10 balance Assets:Transfers:Discover 0.00 USD\n"
    text = OPENS + "\n".join(printer.format_entry(e) for e in [*extract(BOFA), *extract(DISCOVER)])
    _, errors, _ = loader.load_string(text + zero)
    assert errors == []


def test_no_long_numbers_reach_the_ledger() -> None:
    written = "\n".join(printer.format_entry(e) for e in [*extract(BOFA), *extract(DISCOVER)])
    without_ids = re.sub(r'tally-id: "[0-9a-f]{16}"', "", written)
    assert re.search(r"\d{5,}", without_ids) is None
    assert "ID:…0001" in written


def test_mask() -> None:
    assert entries.mask("ID:1000000000001  CO ID:2000000002") == "ID:…0001 CO ID:…0002"
    assert entries.mask("ACCT 1111 FOR 03/09/26") == "ACCT 1111 FOR 03/09/26"


def test_reimporting_a_file_marks_everything_duplicate() -> None:
    found, parsed = setup()
    importer = importers.for_text(BOFA.read_text(newline=""), found, parsed)
    first = list(importer.extract(str(BOFA), []))
    again = list(importer.extract(str(BOFA), []))
    importer.deduplicate(again, first)
    assert all(e.meta.get(entries.DUPLICATE_KEY) for e in again)


def test_identical_rows_count_like_tally_db() -> None:
    # The fixture has two identical 8.25 NOODLE HOUSE rows. A ledger holding one of them
    # gets exactly one more.
    found, parsed = setup()
    importer = importers.for_text(DISCOVER.read_text(newline=""), found, parsed)
    new = list(importer.extract(str(DISCOVER), []))
    noodles = [t for t in transactions(new) if number(t.postings[1]) == Decimal("8.25")]
    assert len(noodles) == 2
    importer.deduplicate(new, [noodles[0]])
    fresh = [e for e in new if not e.meta.get(entries.DUPLICATE_KEY)]
    assert len(fresh) == len(new) - 1
    assert (
        sum(
            1
            for t in transactions(fresh)
            if t.meta[entries.ID_KEY] == noodles[0].meta[entries.ID_KEY]
        )
        == 1
    )


def test_a_row_written_twice_fails_bean_check() -> None:
    bofa = extract(BOFA)
    errors = check(bofa, [transactions(bofa)[0]])
    assert any("Balance failed" in e for e in errors)


def test_a_dropped_row_fails_bean_check() -> None:
    bofa = extract(BOFA)
    dropped = [e for e in bofa if not (isinstance(e, data.Transaction) and "REMITLY" in text(e))]
    assert any("Balance failed" in e for e in check(dropped))


def test_category_without_an_account_is_reported() -> None:
    opens, _, _ = loader.load_string(OPENS.replace('category: "Groceries"', ""))
    found = accounts.from_entries(opens)
    with pytest.raises(accounts.AccountsError, match='category: "Groceries"'):
        found.check(rules.parse(RULES).categories)


def test_tally_account_on_two_accounts_is_refused() -> None:
    opens, _, _ = loader.load_string(
        OPENS + '2026-01-01 open Assets:Other USD\n  tally-account: "discover"\n'
    )
    with pytest.raises(accounts.AccountsError, match="is on both"):
        accounts.from_entries(opens)


def test_two_catch_all_accounts_for_a_category_are_refused() -> None:
    opens, _, _ = loader.load_string(
        OPENS + '2026-01-01 open Expenses:Food\n  category: "Groceries"\n'
    )
    with pytest.raises(accounts.AccountsError, match="two accounts"):
        accounts.from_entries(opens)


def test_merchant_contains_needs_a_category() -> None:
    opens, _, _ = loader.load_string(
        OPENS + '2026-01-01 open Expenses:Food\n  merchant-contains: "X"\n'
    )
    with pytest.raises(accounts.AccountsError, match="needs a category"):
        accounts.from_entries(opens)
