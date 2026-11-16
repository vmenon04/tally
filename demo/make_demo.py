"""Make a data folder full of made-up money, to try tally or record a demo without real data.

    uv run python demo/make_demo.py /tmp/tally-demo
    export TALLY_DATA_DIR=/tmp/tally-demo TALLY_SECRETS_DIR=/tmp/tally-demo/secrets
    uv run tally fava
    uv run tally summary

It starts from example/, makes about four months of invented bank, card and brokerage
activity ending today, and runs it through tally's own sync, as if SimpleFIN had sent it. So
the database, the ledger, pending purchases, holdings and every check are what a real setup
would have. Everything is fabricated: merchants, amounts, account numbers. Claude isn't
asked, so nothing leaves your machine; a few merchants are left uncategorized to show off.
"""

import datetime
import io
import random
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from tally import categorizing, store
from tally.ledger import updating
from tally.simplefin import Account, Answer, Transaction

EXAMPLE = Path(__file__).resolve().parent.parent / "example"
DAYS = 120
CHECKING_START = Decimal("3200.00")
SAVINGS_START = Decimal("1500.00")

EXTRA_ACCOUNTS = """
; Added by demo/make_demo.py: a made-up brokerage account, with holdings from the bridge.
{start} open Assets:Brokerage
  fidelity-account: "9012"
{start} open Assets:Brokerage:Cash USD
{start} open Income:Fidelity:Unreconciled USD
"""
EXTRA_PLAN = """
; Added by demo/make_demo.py.
{start} custom "goal" "Emergency fund" Assets:Bank:Savings "months" 3
{start} custom "about" "Saving for a trip in the spring"
"""
EXTRA_RULES = """
[[rule]]
match = "OAKWOOD APARTMENTS"
category = "Rent"

[[rule]]
match = "CITY POWER"
category = "Utilities"

[[rule]]
match = "CORNER GROCER"
category = "Groceries"

[[rule]]
match = "NOODLE HOUSE"
category = "Eating out"

[[rule]]
match = "BLUE DOOR CAFE"
category = "Eating out"

[[rule]]
match = "TACO STAND"
category = "Eating out"

[[rule]]
match = "CITY TRANSIT"
category = "Transport"

[[rule]]
match = "STREAMBOX"
category = "Subscriptions"

[[rule]]
match = "OUTFITTERS"
category = "Shopping"

[[rule]]
match = "BROKERAGE TRANSFER"
category = "Investing"
"""
# Card purchases: merchants, the range of one purchase, and how many a month.
SHOPS: list[tuple[list[str], int, int, int]] = [
    (["MAPLE MARKET", "CORNER GROCER"], 20, 75, 7),
    (["NOODLE HOUSE", "BLUE DOOR CAFE", "TACO STAND"], 9, 45, 8),
    (["SHELL OIL 4471", "CITY TRANSIT"], 15, 50, 4),
    (["OUTFITTERS", "BOOK NOOK"], 12, 80, 2),
    (["SQ *PINE STREET MKT", "PAYPAL *GADGETHUT"], 8, 60, 1),
]


def main(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} isn't empty; pick a new folder")
    today = datetime.datetime.now().astimezone().date()
    start = today - datetime.timedelta(days=DAYS)
    shutil.copytree(EXAMPLE, out, dirs_exist_ok=True)
    _set_up(out, start)
    answer = _answer(random.Random(4), start, today)

    def no_claude(question: str, schema: dict[str, Any]) -> Any:
        raise categorizing.ClaudeError("not asked for the demo")

    conn = store.connect(out / "tally.db")
    try:
        result = updating.sync(
            out, conn, no_claude, dry_run=False, err=io.StringIO(), bank=lambda: answer
        )
    finally:
        conn.close()
    if result is None or result.bank_failed or result.bank_differ or result.holdings_failed:
        sys.exit(f"the demo didn't sync cleanly: {result}")
    print(f"Made-up data in {out}. Try:")
    print(f"  export TALLY_DATA_DIR={out} TALLY_SECRETS_DIR={out / 'secrets'}")
    print("  uv run tally fava        # http://localhost:5000")
    print("  uv run tally summary")


def _set_up(out: Path, start: datetime.date) -> None:
    """Date the example's accounts from the start, and add a brokerage, goals and rules."""
    opened = (start - datetime.timedelta(days=1)).isoformat()
    ledger = out / "ledger"
    for name in ("accounts", "plan"):
        path = ledger / f"{name}.beancount"
        path.write_text(path.read_text().replace("2026-01-01", opened))
    (ledger / "opening.beancount").write_text(
        f'{opened} * "Opening balances"\n'
        f"  Assets:Bank:Checking   {CHECKING_START} USD\n"
        f"  Assets:Bank:Savings    {SAVINGS_START} USD\n"
        f"  Equity:Opening-Balances\n"
    )
    with (ledger / "accounts.beancount").open("a") as f:
        f.write(EXTRA_ACCOUNTS.format(start=opened))
    with (ledger / "plan.beancount").open("a") as f:
        f.write(EXTRA_PLAN.format(start=opened))
    with (out / "rules.toml").open("a") as f:
        f.write(EXTRA_RULES)


def _answer(rng: random.Random, start: datetime.date, today: datetime.date) -> Answer:
    """What the bridge might say about a made-up checking account, card and brokerage."""
    checking: list[Transaction] = []
    card: list[Transaction] = []
    owed = Decimal(0)

    def add(rows: list[Transaction], day: datetime.date, what: str, amount: Decimal) -> None:
        pending = day > today - datetime.timedelta(days=2) and rows is card and amount < 0
        rows.append(
            Transaction(
                id=f"demo-{len(checking) + len(card)}",
                posted=None if pending else day,
                transacted=day,
                amount=amount,
                description=what,
                pending=pending,
            )
        )

    day = start
    while day <= today:
        if day.day == 1:
            add(checking, day, "OAKWOOD APARTMENTS RENT", Decimal("-1200.00"))
            add(checking, day, "TRANSFER TO SAV 8841", Decimal("-250.00"))
            add(checking, day, "BROKERAGE TRANSFER", Decimal("-200.00"))
        if day.day == 5 and owed:
            add(checking, day, "CARD PAYMENT THANK YOU", -owed)
            add(card, day, "CARD PAYMENT RECEIVED", owed)
            owed = Decimal(0)
        if day.day == 12:
            add(checking, day, "CITY POWER UTILITY", _money(rng, 70, 115))
            add(card, day, "STREAMBOX MONTHLY", Decimal("-15.99"))
            owed += Decimal("15.99")
        if day.day in (15, 30) or (day.day == 28 and day.month == 2):
            add(checking, day, "ACME CORP PAYROLL", Decimal("2150.00"))
        for names, low, high, per_month in SHOPS:
            if rng.random() < per_month / 30:
                amount = _money(rng, low, high)
                add(card, day, rng.choice(names), amount)
                if day <= today - datetime.timedelta(days=2):
                    owed -= amount
        day += datetime.timedelta(days=1)

    def balance(start_at: Decimal, rows: list[Transaction]) -> Decimal:
        return start_at + sum((t.amount for t in rows if not t.pending), Decimal(0))

    shares, price = Decimal("8.000"), Decimal("118.40")
    return Answer(
        accounts=[
            _account(
                "a1", "Everyday Checking (1234)", balance(CHECKING_START, checking), today, checking
            ),
            _account("a2", "Rewards Card (5678)", balance(Decimal(0), card), today, card),
            _account(
                "a3",
                "Brokerage (9012)",
                Decimal("1197.20"),
                today,
                [],
                holdings=[
                    {
                        "symbol": "TOTALMKT",
                        "shares": str(shares),
                        "market_value": str(shares * price),
                        "purchase_price": "102.15",
                    },
                    {
                        "symbol": "CORE",
                        "shares": "250.00",
                        "market_value": "250.00",
                        "purchase_price": "0",
                    },
                ],
            ),
        ],
        problems=[],
    )


def _account(
    id: str,
    name: str,
    balance: Decimal,
    today: datetime.date,
    rows: list[Transaction],
    holdings: list[dict[str, str]] | None = None,
) -> Account:
    return Account(
        id=id,
        name=name,
        bank="Demo Bank",
        currency="USD",
        balance=balance,
        balance_date=today,
        transactions=rows,
        unread={"holdings": holdings} if holdings is not None else {},
    )


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return -Decimal(f"{rng.uniform(low, high):.2f}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
