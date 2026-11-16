import datetime
from decimal import Decimal

from tally import plan, rules, summary
from tally.alerts import Alert
from tally.plan import Targets
from tally.transaction import Transaction

RULES = rules.parse(
    'categories = ["Rent", "Groceries", "Fun"]\n'
    '[[rule]]\nmatch = "LANDLORD"\ncategory = "Rent"\n'
    '[[rule]]\nmatch = "MARKET"\ncategory = "Groceries"\n'
    '[[rule]]\nmatch = "ARCADE"\ncategory = "Fun"\n'
    '[[rule]]\nmatch = "PAYROLL"\ncategory = "Income"\n'
    '[[rule]]\nmatch = "BROKERAGE"\ncategory = "Investing"\n'
)
TARGETS = Targets(
    monthly={"Rent": Decimal(1000), "Groceries": Decimal(300), "Fun": Decimal(100)},
    months={
        plan.PAYCHECKS: {"Apr 2026": Decimal(2)},
        plan.ROTH: {"Mar 2026": Decimal(500), "Apr 2026": Decimal(500)},
    },
)
NO_LIMIT = summary.Roth(None, Decimal(0))
SUNDAY = datetime.date(2026, 3, 15)


def tx(
    day: int,
    merchant: str,
    amount: str,
    month: int = 3,
    account: str = "discover",
) -> Transaction:
    date = datetime.date(2026, month, day)
    return Transaction(account, date, date, merchant, merchant, Decimal(amount))


def build(
    *transactions: Transaction,
    today: datetime.date = SUNDAY,
    targets: Targets | None = TARGETS,
    roth: summary.Roth = NO_LIMIT,
    pending: tuple[Transaction, ...] = (),
    not_found: tuple[Alert, ...] = (),
) -> summary.Summary:
    pairs = [(n, t) for n, t in enumerate(transactions, start=1)]
    pairs += [(-n, t) for n, t in enumerate(pending, start=1)]
    return summary.build(today, pairs, RULES, targets, list(not_found), roth)


def look_out(s: summary.Summary, start: str) -> list[str]:
    return [line for line in s.look_out if line.startswith(start)]


def test_week_is_the_seven_days_to_today_newest_first() -> None:
    s = build(
        tx(8, "MARKET A", "-1.00"),  # 8 days before: last week
        tx(9, "MARKET B", "-2.00"),
        tx(15, "MARKET C", "-3.00"),
        tx(16, "MARKET D", "-4.00"),  # after today
        tx(12, "PAYROLL", "900.00"),  # not spending
        tx(12, "BROKERAGE", "-500.00"),
        pending=(tx(14, "ARCADE", "-5.00"),),
    )
    assert [(i.transaction.merchant, i.pending) for i in s.week] == [
        ("MARKET C", False),
        ("ARCADE", True),
        ("MARKET B", False),
    ]
    assert s.week_total == Decimal("10.00")
    assert {c.name: c.spent for c in s.month}["Groceries"] == Decimal("6.00")


def test_over_plan_is_one_line_biggest_first() -> None:
    s = build(tx(2, "LANDLORD", "-1050.00"), tx(3, "MARKET", "-400.00"))
    assert look_out(s, "Over plan") == [
        "Over plan this month: Groceries by $100.00, Rent by $50.00."
    ]


def test_ahead_of_pace_needs_several_purchases() -> None:
    # Day 15 of 31: 48% of March gone. 80% of the Fun plan used.
    three = build(tx(2, "ARCADE", "-30.00"), tx(5, "ARCADE", "-30.00"), tx(9, "ARCADE", "-20.00"))
    assert look_out(three, "Fun is ahead of pace") == [
        "Fun is ahead of pace: 80% of its plan used with 48% of the month gone. "
        "$20.00 left, about $1 a day."
    ]
    two = build(tx(2, "ARCADE", "-40.00"), tx(9, "ARCADE", "-40.00"))
    assert look_out(two, "Fun") == []


def test_big_purchase_needs_company_in_its_category() -> None:
    s = build(
        tx(1, "MARKET A", "-20.00"), tx(13, "MARKET B", "-160.00"), tx(14, "ARCADE", "-90.00")
    )
    assert look_out(s, "MARKET B") == [
        "MARKET B ($160.00) was 53% of the monthly Groceries plan on its own."
    ]
    assert look_out(s, "ARCADE") == []  # the month's only Fun purchase: a bill, not a splurge


def test_uncategorized_this_month() -> None:
    s = build(tx(4, "MYSTERY SHOP", "-12.00"), tx(4, "OLD MYSTERY", "-1.00", month=2))
    assert look_out(s, "1 uncategorized") == [
        "1 uncategorized this month, not counted in the totals: MYSTERY SHOP (-$12.00). "
        "Set a category in Fava or Sure, or add a rule."
    ]


def test_not_found_alerts() -> None:
    alert = Alert(
        "discover",
        datetime.date(2026, 3, 2),
        "CORNER CAFE",
        Decimal("-9.99"),
        "<a@example.com>",
        datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC),
    )
    s = build(not_found=(alert,))
    assert look_out(s, "Not in the bank export") == [
        "Not in the bank export: Mar 2  CORNER CAFE  $9.99. "
        "Declined or refunded? If not, check your bank's website."
    ]


def test_monthly_import_is_due_from_the_third() -> None:
    rows = (
        tx(20, "MARKET", "-1.00", month=2),
        tx(28, "MARKET", "-1.00", month=2, account="bofa_checking"),
    )
    assert look_out(build(*rows, today=datetime.date(2026, 3, 2)), "Time to catch up") == []
    assert look_out(build(*rows, today=datetime.date(2026, 3, 3)), "Time to catch up") == [
        "Time to catch up Discover: nothing posted since Feb 20. Check SimpleFIN, or for an "
        "account it doesn't fill, import February's statement CSV."
    ]


def test_pending_alerts_never_count_as_imported() -> None:
    s = build(
        tx(28, "MARKET", "-1.00", month=2),
        today=datetime.date(2026, 4, 5),
        pending=(tx(30, "MARKET", "-1.00"),),
    )
    assert look_out(s, "Time to catch up") == [
        "Time to catch up Discover: nothing posted since Feb 28. Check SimpleFIN, or for an "
        "account it doesn't fill, import March's statement CSV."
    ]


def test_last_month_is_closed_out_in_the_first_week() -> None:
    rows = (tx(2, "LANDLORD", "-1000.00"), tx(20, "MARKET", "-350.00"), tx(21, "ARCADE", "-150.00"))
    first_week = build(*rows, today=datetime.date(2026, 4, 5))
    assert first_week.last_month == (
        "March ended at $1,500.00 of a $1,400.00 plan; over in Groceries by $50.00, Fun by $50.00."
    )
    assert build(*rows, today=datetime.date(2026, 4, 7)).last_month is None


def test_next_month_from_the_budget_tab() -> None:
    assert build().coming_up[0] == (
        "April: 2 paycheck(s), $1,400.00 of spending planned, $500.00 to the Roth IRA."
    )
    assert build(targets=None).coming_up == ["Roth IRA 2026: $0.00 in so far."]


def roth_line(limit: str, elsewhere: str, *transactions: Transaction) -> str:
    s = build(*transactions, roth=summary.Roth(Decimal(limit), Decimal(elsewhere)))
    return s.coming_up[-1]


def test_roth_counts_what_this_month_still_has_planned() -> None:
    # March plans 500 and 200 went in, so 300 of it is still to come, plus April's 500.
    line = roth_line("7500", "1000", tx(10, "BROKERAGE", "-200.00"))
    assert line.startswith("Roth IRA 2026: $1,200.00 in so far, $800.00 more planned. ")
    assert line.endswith("$5,500.00 of the $7,500.00 limit would be left unused.")


def test_roth_limit_exactly_reached_or_overshot() -> None:
    assert roth_line("2000", "1000").endswith("which reaches the $2,000.00 limit exactly.")
    assert "would go $500.00 over the $1,000.00 limit" in roth_line("1000", "500")
    assert "$100.00 over the $900.00 limit: ask the brokerage" in roth_line("900", "1000")


def test_format() -> None:
    text = summary.format_summary(
        build(tx(13, "MARKET", "-12.50"), pending=(tx(14, "ARCADE", "-5.00"),))
    )
    assert text.startswith("tally: week of Mar 9 – Mar 15, 2026\n\nTHIS WEEK\n")
    assert "  Spent $17.50 on 2 purchase(s) (your plan works out to about $316 a week)" in text
    assert "  Sat Mar 14  ARCADE      $5.00  Fun  (pending)" in text
    assert "MARCH SO FAR (day 15 of 31)" in text
    assert "  Groceries     $12.50 of   $300.00     4%" in text
    assert "  Total         $17.50 of $1,400.00  ($86 a day left for the rest of the month)" in text
    assert "LOOK OUT FOR\n  Nothing this week." in text
    assert "COMING UP\n  - April:" in text


def test_format_without_the_plan() -> None:
    s = summary.build(SUNDAY, [(1, tx(13, "MARKET", "-12.50"))], RULES, None, [], NO_LIMIT, "(x)")
    text = summary.format_summary(s)
    assert "\n\n(x)\n\n" in text
    assert "a week)" not in text
    assert "  Groceries     $12.50\n" in text
