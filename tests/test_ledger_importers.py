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


def importer_for(path: Path) -> importers.BofaChecking | importers.Discover:
    found, parsed = setup()
    bofa, discover = importers.all_importers(found, parsed)
    return bofa if path == BOFA else discover


def extract(path: Path) -> list[data.Directive]:
    return list(importer_for(path).extract(str(path), []))


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


def test_unknown_account_has_no_importer() -> None:
    found, parsed = setup()
    with pytest.raises(ParseError, match="no ledger importer"):
        importers.for_account("capital_one", found, parsed)


def test_bofa_file_passes_bean_check() -> None:
    assert check(extract(BOFA)) == []


def test_bofa_asserts_the_start_and_a_settled_end() -> None:
    # The fixture ends 03/14; three days before that, after the 03/11 row, it held 1873.78.
    balances = [e for e in extract(BOFA) if isinstance(e, data.Balance)]
    assert [(b.date, b.amount.number) for b in balances] == [
        (datetime.date(2026, 3, 1), Decimal("1200.00")),
        (datetime.date(2026, 3, 12), Decimal("1873.78")),
    ]


def test_rows_posted_late_into_the_last_days_pass_the_settled_balance() -> None:
    # An export as of 03/14 taken before a row dated 03/14 posted, then a later one with it.
    early = make_bofa_file(['03/02/2026,"SHOP A 03/02 PURCHASE","-10.00","90.00"'], "03/14")
    late = make_bofa_file(
        [
            '03/02/2026,"SHOP A 03/02 PURCHASE","-10.00","90.00"',
            '03/14/2026,"SHOP B 03/14 PURCHASE","-5.00","85.00"',
        ],
        "03/20",
    )
    found, parsed = setup()
    bofa, _ = importers.all_importers(found, parsed)
    first = list(bofa.extract_text(early, "early.csv"))
    second = list(bofa.extract_text(late, "late.csv"))
    bofa.deduplicate(second, first)
    fresh = [e for e in second if not e.meta.get(entries.DUPLICATE_KEY)]
    opening = (
        '2026-02-28 * "Opening"\n  Assets:BofA:Checking  100.00 USD\n  Equity:Opening-Balances\n'
    )
    text = OPENS.split("2026-02-28 *")[0] + opening
    text += "\n".join(printer.format_entry(e) for e in [*first, *fresh])
    _, errors, _ = loader.load_string(text)
    assert [e.message for e in errors] == []


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
    importer = importer_for(BOFA)
    first = list(importer.extract(str(BOFA), []))
    again = list(importer.extract(str(BOFA), []))
    importer.deduplicate(again, first)
    assert all(e.meta.get(entries.DUPLICATE_KEY) for e in again)


def test_identical_rows_count_like_tally_db() -> None:
    # The fixture has two identical 8.25 NOODLE HOUSE rows. A ledger holding one of them
    # gets exactly one more.
    importer = importer_for(DISCOVER)
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
    # A row before the settled balance (03/11); later ones are checked by the next export.
    dropped = [e for e in bofa if not (isinstance(e, data.Transaction) and "MAPLE" in text(e))]
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


def make_bofa_file(rows: list[str], end: str) -> str:
    amounts = [Decimal(row.split('","')[1]) for row in rows]
    ending = Decimal("100.00") + sum(amounts, Decimal(0))
    debits = sum((a for a in amounts if a < 0), Decimal(0))
    return "\r\n".join(
        [
            "Description,,Summary Amt.",
            'Beginning balance as of 03/01/2026,,"100.00"',
            'Total credits,,"0.00"',
            f'Total debits,,"{debits:.2f}"',
            f'Ending balance as of {end}/2026,,"{ending:.2f}"',
            "",
            "Date,Description,Amount,Running Bal.",
            '03/01/2026,Beginning balance as of 03/01/2026,,"100.00"',
            *rows,
            "",
        ]
    )
