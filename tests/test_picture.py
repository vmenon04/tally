import datetime
from decimal import Decimal

import pytest
from beancount import loader

from tally import plan
from tally.ledger import accounts, picture

LEDGER = """
option "operating_currency" "USD"
2026-01-01 open Assets:Checking USD
  tally-account: "bofa_checking"
2026-01-01 open Assets:Fund
2026-01-01 open Income:Salary USD
  category: "Income"
2026-01-01 open Income:Salary:401k USD
2026-01-01 open Income:Fidelity:Unreconciled USD
2026-01-01 open Expenses:Rent
  category: "Rent + fees"
  fixed: TRUE
2026-01-01 open Expenses:Groceries
  category: "Groceries"
2026-01-01 open Expenses:Uncategorized
  category: "Uncategorized"
2026-01-01 custom "budget" Expenses:Rent "monthly" 1000.00 USD
2026-01-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD

2026-02-14 * "PAYROLL" ""
  Assets:Checking  2000.00 USD
  Income:Salary
2026-02-14 * "Fidelity" "401(k)"
  Assets:Fund  10 FUND {{500.00 USD}}
  Income:Salary:401k  -500.00 USD
2026-02-20 price FUND 60.00 USD
2026-02-20 * "Fidelity" "market"
  Assets:Checking  5.00 USD
  Income:Fidelity:Unreconciled
2026-02-01 * "RENT" ""
  tally-id: "a1"
  Assets:Checking  -1000.00 USD
  Expenses:Rent  1000.00 USD
2026-02-10 * "MAPLE MARKET" ""
  tally-id: "a2"
  Assets:Checking  -200.00 USD
  Expenses:Groceries  200.00 USD
2026-03-02 * "MAPLE MARKET" ""
  tally-id: "a3"
  purchased: 2026-02-28
  Assets:Checking  -100.00 USD
  Expenses:Groceries  100.00 USD
2026-03-01 * "RENT" ""
  tally-id: "a4"
  Assets:Checking  -1000.00 USD
  Expenses:Rent  1000.00 USD
2026-03-05 * "MYSTERY SHOP" ""
  tally-id: "a5"
  Assets:Checking  -40.00 USD
  Expenses:Uncategorized  40.00 USD
2026-03-06 * "MAPLE MARKET" ""
  tally-id: "a6"
  Assets:Checking  -50.00 USD
  Expenses:Groceries  50.00 USD
"""
TODAY = datetime.date(2026, 3, 10)


@pytest.fixture
def found() -> picture.Picture:
    loaded, errors, _ = loader.load_string(LEDGER)
    assert errors == []
    mapping = accounts.from_entries(loaded)
    return picture.build(loaded, mapping, plan.read(loaded, mapping, TODAY), TODAY)


def test_pay_counts_401k_but_not_the_market(found: picture.Picture) -> None:
    feb = found.months[-2]
    assert feb.pay == Decimal("2500.00")
    # Groceries bought 02/28 count in February, though they posted in March.
    assert feb.spent == Decimal("1300.00")
    assert feb.rate == Decimal("0.48")
    assert found.this_month.pay == 0 and found.this_month.rate is None


def test_categories_against_plan_and_usual(found: picture.Picture) -> None:
    by_name = {c.name: c for c in found.categories}
    assert by_name["Groceries"].spent == Decimal("50.00")
    assert by_name["Groceries"].usual == Decimal("300.00")  # February, the only month before
    assert by_name["Rent + fees"].fixed and not by_name["Groceries"].fixed
    # Rent in full, plus 10 of March's 31 days of groceries' 300.
    assert found.pace == Decimal("1096.77")
    assert found.pace_spent == Decimal("1090.00")  # 1000 + 50 + 40 uncategorized


def test_merchants_and_net_worth(found: picture.Picture) -> None:
    assert found.merchants[0] == ("RENT", Decimal("1000.00"))
    day, worth = found.net_worth[-2]
    # End of February: 2000 + 5 - 1000 - 200 cash, 10 units at 60.00.
    assert day == datetime.date(2026, 2, 28) and worth == Decimal("1405.00")


GOALS = """
2026-01-01 open Assets:Fund2
  fidelity-account: "2222"
  contributions-from: "Income:Salary:401k"
2026-01-01 open Assets:HighYield USD
2026-01-01 open Assets:Savings USD
2026-01-01 custom "goal" "Emergency fund" Assets:HighYield "months" 3
2026-01-01 custom "goal" "Travel" Assets:Savings
2026-03-01 custom "roth-plan" 100.00 USD
2026-03-03 * "TO HYSA" ""
  Assets:Checking  -200.00 USD
  Assets:HighYield  200.00 USD
"""


def build_with(
    extra: str, roth_limit: Decimal | None = None, roth_elsewhere: Decimal = Decimal(0)
) -> picture.Picture:
    loaded, errors, _ = loader.load_string(LEDGER + extra)
    assert errors == []
    mapping = accounts.from_entries(loaded)
    return picture.build(
        loaded,
        mapping,
        plan.read(loaded, mapping, TODAY),
        TODAY,
        roth_limit=roth_limit,
        roth_elsewhere=roth_elsewhere,
    )


def test_goals_share_whats_left() -> None:
    found = build_with(GOALS)
    # February is the one whole month with pay: 2000 take-home (the 500 to the 401(k) is
    # saved already) less 1300 spent, less this month's 100 planned for the Roth IRA.
    assert found.spare is not None and found.spare.left == Decimal("600.00")
    emergency, travel = found.goals
    assert emergency.target == Decimal("3900")  # 3 x 1300, to the nearest hundred
    assert emergency.balance == emergency.added == Decimal("200.00")
    assert emergency.suggested == Decimal("420")  # 70% of 600
    assert travel.target is None and travel.suggested == Decimal("180")


def test_a_full_emergency_fund_leaves_it_all_to_the_rest() -> None:
    found = build_with(
        GOALS.replace("Assets:HighYield  200.00", "Assets:HighYield  4000.00").replace(
            "Assets:Checking  -200.00", "Assets:Checking  -4000.00"
        )
    )
    emergency, travel = found.goals
    assert emergency.suggested == 0 and travel.suggested == Decimal("600")


def test_a_goal_needs_a_name_and_an_account() -> None:
    loaded, _, _ = loader.load_string(
        LEDGER + '2026-01-01 custom "goal" "Travel" Assets:Checking "weeks" 3\n'
    )
    with pytest.raises(plan.PlanError, match='"months" or "target"'):
        plan.read(loaded, accounts.from_entries(loaded), TODAY)


INVESTING = """
2025-01-01 open Equity:Opening-Balances
2025-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1111"
2025-01-01 open Assets:Fidelity:Roth:Cash USD
2025-01-01 open Assets:Fidelity:Go
  fidelity-account: "3333"
2025-12-20 * "Last year's contribution"
  Assets:Fidelity:Roth:Cash  1000.00 USD
  Equity:Opening-Balances
2026-01-05 * "Snapshot" #fidelity-positions
  Assets:Fidelity:Roth  4 IDX {{400.00 USD}}
  Assets:Fidelity:Roth:Cash  -400.00 USD
2026-01-05 * "Snapshot" #fidelity-positions
  Assets:Fidelity:Go  2 ABC {{300.00 USD}}
  Equity:Opening-Balances
2026-02-15 * "To the Roth IRA"
  Assets:Fidelity:Roth:Cash  500.00 USD
  Assets:Checking
2026-03-01 price IDX 110.00 USD
"""


def test_investments_show_worth_paid_and_gain() -> None:
    found = build_with(INVESTING, roth_limit=Decimal("7500"), roth_elsewhere=Decimal("140"))
    roth, go = found.investments
    # 4 IDX at today's 110, and 1,100 in cash; paid 400 for the IDX, and the cash.
    assert (roth.name, roth.worth, roth.paid) == ("Roth", Decimal("1540.00"), Decimal("1500.00"))
    assert roth.gain == Decimal("40.00") and roth.account == "Assets:Fidelity:Roth"
    # No price for ABC: worth what it cost.
    assert (go.name, go.worth, go.gain) == ("Go", Decimal("300.00"), Decimal(0))
    # This year's money in from the bank, and what went in elsewhere; not last year's.
    assert found.roth is not None
    assert (found.roth.put_in, found.roth.left) == (Decimal("640.00"), Decimal("6860.00"))
    assert found.roth.deadline == datetime.date(2027, 4, 15)
    assert build_with(INVESTING).roth is None  # without a limit
