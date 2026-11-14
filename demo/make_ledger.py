"""Write a made-up ledger to try tally without real data.

    uv run python demo/make_ledger.py /tmp/tally-demo
    TALLY_DATA_DIR=/tmp/tally-demo uv run tally fava

Everything in it is fabricated: merchants, amounts, accounts. It's dated around today, so
there's always a current month to show.
"""

import datetime
import hashlib
import random
import sys
from pathlib import Path

ACCOUNTS = """\
2025-01-01 open Equity:Opening-Balances
2025-01-01 open Assets:Bank:Checking USD
  tally-account: "bofa_checking"
2025-01-01 open Liabilities:Card USD
  tally-account: "discover"
2025-01-01 open Assets:Bank:Savings USD
  category: "Savings transfer"
  merchant-contains: "TRAVEL"
2025-01-01 open Assets:HighYield:Savings USD
  category: "Savings transfer"
2025-01-01 open Assets:Transfers:Card USD
  category: "Card payment"
2025-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1111"
2025-01-01 open Assets:Fidelity:Roth:Cash USD
  category: "Investing"
2025-01-01 open Assets:Fidelity:401k
  fidelity-account: "0000"
  contributions-from: "Income:Salary:401k"
2025-01-01 open Income:Salary USD
  category: "Income"
2025-01-01 open Income:Salary:401k USD
2025-01-01 open Income:Fidelity:Unreconciled USD
2025-01-01 open Expenses:Rent
  category: "Rent + fees"
  fixed: TRUE
  fixed: TRUE
2025-01-01 open Expenses:Utilities
  category: "Utilities"
  fixed: TRUE
  fixed: TRUE
2025-01-01 open Expenses:Groceries
  category: "Groceries"
2025-01-01 open Expenses:EatingOut
  category: "Eating out / dates"
2025-01-01 open Expenses:Transport
  category: "Gas / transport"
2025-01-01 open Expenses:Subscriptions
  category: "Subscriptions"
  fixed: TRUE
  fixed: TRUE
2025-01-01 open Expenses:Personal
  category: "Personal / other"
2025-01-01 open Expenses:Travel
  category: "Travel"
2025-01-01 open Expenses:Uncategorized
  category: "Uncategorized"
"""
PLAN = """\
2025-01-01 custom "budget" Expenses:Rent "monthly" 1100.00 USD
2025-01-01 custom "budget" Expenses:Utilities "monthly" 90.00 USD
2025-01-01 custom "budget" Expenses:Groceries "monthly" 300.00 USD
2025-01-01 custom "budget" Expenses:EatingOut "monthly" 200.00 USD
2025-01-01 custom "budget" Expenses:Transport "monthly" 120.00 USD
2025-01-01 custom "budget" Expenses:Subscriptions "monthly" 40.00 USD
2025-01-01 custom "budget" Expenses:Personal "monthly" 150.00 USD
2025-01-01 custom "goal" "Emergency fund" Assets:HighYield:Savings "months" 3
2025-01-01 custom "goal" "Travel" Assets:Bank:Savings
2025-01-01 custom "roth-plan" 300.00 USD
"""
MAIN = """\
option "title" "Demo ledger (made up)"
option "operating_currency" "USD"
include "accounts.beancount"
include "plan.beancount"
include "imported/*.beancount"
include "pending.beancount"
"""
SHOPS = {
    "Expenses:Groceries": (["MAPLE MARKET", "CORNER GROCER", "BULK BIN CO"], 25, 70, 5),
    "Expenses:EatingOut": (
        ["NOODLE HOUSE", "TACO STAND", "BLUE DOOR CAFE", "PIZZA BAR"],
        12,
        45,
        6,
    ),
    "Expenses:Transport": (["CITY TRANSIT", "GAS-N-GO"], 15, 45, 3),
    "Expenses:Personal": (["BOOK NOOK", "OUTFITTERS", "PHARMACY PLUS"], 10, 60, 3),
}


def main(out: Path) -> None:
    rng = random.Random(7)
    today = datetime.datetime.now().astimezone().date()
    ledger = out / "ledger"
    (ledger / "imported").mkdir(parents=True, exist_ok=True)
    (ledger / "main.beancount").write_text(MAIN)
    (ledger / "accounts.beancount").write_text(ACCOUNTS)
    (ledger / "plan.beancount").write_text(PLAN)
    (ledger / "pending.beancount").write_text("")
    (out / "rules.toml").write_text(
        'categories = ["Rent + fees", "Utilities", "Groceries", "Eating out / dates", '
        '"Gas / transport", "Subscriptions", "Personal / other", "Travel"]\n'
    )
    lines = []
    start = (today.replace(day=1) - datetime.timedelta(days=160)).replace(day=1)
    lines.append(
        f'{start} * "Opening balance"\n  Assets:Bank:Checking  1800.00 USD\n'
        f"  Equity:Opening-Balances\n"
    )
    units, price, index = 0.0, 60.0, 100.0
    day = start
    while day <= today:
        if day.day == 1:
            lines.append(entry(day, "RENT PORTAL", "Expenses:Rent", 1100, "bofa_checking"))
            lines.append(
                entry(day, "ROTH TRANSFER", "Assets:Fidelity:Roth:Cash", 300, "bofa_checking")
            )
            index *= 1 + rng.uniform(-0.03, 0.05)
            lines.append(
                f'{day} * "Fidelity" "Roth IRA purchase" #fidelity-positions\n'
                f"  Assets:Fidelity:Roth  {300 / index:.3f} INDEX {{{{300.00 USD}}}}\n"
                f"  Assets:Fidelity:Roth:Cash  -300.00 USD\n"
                f"{day} price INDEX {index:.2f} USD\n"
            )
            lines.append(
                entry(day, "TO HIGH YIELD", "Assets:HighYield:Savings", 500, "bofa_checking")
            )
            lines.append(entry(day, "TO TRAVEL", "Assets:Bank:Savings", 150, "bofa_checking"))
        if day.day == 12:
            lines.append(
                entry(
                    day, "CITY POWER", "Expenses:Utilities", rng.randint(60, 110), "bofa_checking"
                )
            )
            lines.append(entry(day, "STREAMFLIX", "Expenses:Subscriptions", 15.99, "discover"))
        if day.day in (14, 28):
            lines.append(entry(day, "ACME CORP PAYROLL", "Income:Salary", -2290, "bofa_checking"))
            contribution = 980.88
            price *= 1 + rng.uniform(-0.03, 0.04)
            bought = round(contribution / price, 3)
            units += bought
            lines.append(
                f'{day} * "Fidelity" "401(k) contribution"\n'
                f"  Assets:Fidelity:401k  {bought} FFUND {{{{{contribution} USD}}}}\n"
                f"  Income:Salary:401k  -{contribution} USD\n"
                f"{day} price FFUND {price:.2f} USD\n"
            )
        for account, (names, low, high, per_month) in SHOPS.items():
            if rng.random() < per_month / 30:
                lines.append(
                    entry(
                        day,
                        rng.choice(names),
                        account,
                        round(rng.uniform(low, high), 2),
                        "discover",
                    )
                )
        day += datetime.timedelta(days=1)
    trip = today.replace(day=1) - datetime.timedelta(days=40)
    for name, amount in (
        ("BUDGET AIR", 186.40),
        ("HOSTEL CENTRAL", 142.00),
        ("STREET FOOD MKT", 38.25),
    ):
        lines.append(entry(trip, name, "Expenses:Travel", amount, "discover"))
    for name, amount in (("SQ *MYSTERY SHOP", 23.50), ("PAYPAL *ONLINE", 61.00)):
        lines.append(
            entry(
                today - datetime.timedelta(days=rng.randint(1, 5)),
                name,
                "Expenses:Uncategorized",
                amount,
                "discover",
            )
        )
    (ledger / "imported" / "demo.beancount").write_text("\n".join(lines))
    print(f"wrote {ledger}")


def entry(day: datetime.date, payee: str, other: str, amount: float, source: str) -> str:
    source_account = "Assets:Bank:Checking" if source == "bofa_checking" else "Liabilities:Card"
    tally_id = hashlib.sha256(f"{source}|{day}|{payee}|{amount}".encode()).hexdigest()[:16]
    return (
        f'{day} * "{payee}" "{payee}"\n'
        f'  tally-id: "{tally_id}"\n'
        f"  {source_account}  {-amount:.2f} USD\n"
        f"  {other}  {amount:.2f} USD\n"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
