"""Make a data folder full of made-up money, to try tally or record a demo without real data.

    uv run python demo/make_demo.py /tmp/tally-demo
    export TALLY_DATA_DIR=/tmp/tally-demo TALLY_SECRETS_DIR=/tmp/tally-demo/secrets
    uv run tally summary
    uv run tally fava

and open http://localhost:5000 while Fava runs.

With --sure, it also starts a throwaway Sure in Docker at http://127.0.0.1:3201, apart from
any real one (its own containers and database, under the project name tally-demo), with a
demo sign-in, and fills it through the same sync:

    uv run python demo/make_demo.py --sure /tmp/tally-demo

It starts from example/, makes about four months of invented bank, card and brokerage
activity ending today, and runs it through tally's own sync, as if SimpleFIN had sent it. So
the database, the ledger, pending purchases, holdings and every check are what a real setup
would have. Everything is fabricated: merchants, amounts, account numbers. Claude isn't
asked, so nothing leaves your machine; a few merchants are left uncategorized to show off.
"""

import datetime
import io
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from tally import categorizing, store, sure
from tally.ledger import updating
from tally.simplefin import Account, Answer, Transaction

EXAMPLE = Path(__file__).resolve().parent.parent / "example"
DAYS = 120
CHECKING_START = Decimal("3200.00")
SAVINGS_START = Decimal("1500.00")

ROOT = Path(__file__).resolve().parent.parent
SURE_PROJECT = "tally-demo"
SURE_PORT = 3201
SURE_EMAIL = "demo@example.com"
SURE_PASSWORD = "tally-demo"
# Each ledger account Sure shows: its name in Sure, Sure's kind of account, and what it held
# when the demo starts (Sure counts on from there).
SURE_ACCOUNTS = {
    "Assets:Bank:Checking": ("Everyday Checking", "Depository", "checking", CHECKING_START),
    "Assets:Bank:Savings": ("Savings", "Depository", "savings", SAVINGS_START),
    "Liabilities:Card": ("Rewards Card", "CreditCard", None, Decimal(0)),
    "Assets:Brokerage": ("Brokerage", "Investment", "brokerage", Decimal(0)),
}
# Run in Sure (rails runner): a family, its signed-in user, the accounts and an API key.
SURE_SETUP = """
spec = JSON.parse(ENV.fetch("DEMO"))
family = Family.create!(name: "Demo", currency: "USD", locale: "en", country: "US",
                        timezone: spec["timezone"], date_format: "%m/%d/%Y")
user = family.users.create!(email: spec["email"], password: spec["password"], role: "admin",
                            first_name: "Demo", last_name: "User", onboarded_at: Time.current)
spec["accounts"].each do |a|
  attributes = { owner: user, name: a["name"], balance: a["balance"].to_d, currency: "USD",
                 accountable_type: a["type"],
                 accountable_attributes: { subtype: a["subtype"] }.compact }
  family.accounts.create_and_sync(attributes, opening_balance_date: Date.parse(a["opened"]))
end
key = ApiKey.generate_secure_key
user.api_keys.create!(name: "tally", key: key, scopes: ["read_write"], source: "web")
puts "API_KEY=#{key}"
"""

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


def main(out: Path, with_sure: bool = False) -> None:
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} isn't empty; pick a new folder")
    if with_sure:
        _no_sure_yet()
    today = datetime.datetime.now().astimezone().date()
    start = today - datetime.timedelta(days=DAYS)
    shutil.copytree(EXAMPLE, out, dirs_exist_ok=True)
    _set_up(out, start)
    answer = _answer(random.Random(4), start, today)
    client = _start_sure(out, start - datetime.timedelta(days=1)) if with_sure else None

    def no_claude(question: str, schema: dict[str, Any]) -> Any:
        raise categorizing.ClaudeError("not asked for the demo")

    conn = store.connect(out / "tally.db")
    try:
        result = updating.sync(
            out,
            conn,
            no_claude,
            dry_run=False,
            err=io.StringIO(),
            bank=lambda: answer,
            sure=client,
        )
    finally:
        conn.close()
    if (
        result is None
        or result.bank_failed
        or result.bank_differ
        or result.holdings_failed
        or result.sure_failed
    ):
        sys.exit(f"the demo didn't sync cleanly: {result}")
    print(f"Made-up data in {out}. Try:")
    print(f"  export TALLY_DATA_DIR={out} TALLY_SECRETS_DIR={out / 'secrets'}")
    print("  uv run tally summary")
    print("  uv run tally fava")
    print("Fava runs at http://localhost:5000 until you stop it (Ctrl-C).")
    if client is not None:
        print(f"\nSure is at {client.url}: sign in as {SURE_EMAIL}, password {SURE_PASSWORD}.")
        print("To remove it, with everything in it:")
        print(f"  {' '.join(_compose(out))} down --volumes")


def _compose(out: Path) -> list[str]:
    """The docker compose command for the demo's own Sure."""
    return [
        "docker",
        "compose",
        "--project-name",
        SURE_PROJECT,
        "--file",
        str(ROOT / "deploy" / "sure" / "compose.yml"),
        "--env-file",
        str(out / "sure.env"),
    ]


def _start_sure(out: Path, opened: datetime.date) -> sure.Client:
    """Start a Sure of its own in Docker, with a demo user, the accounts and an API key."""
    (out / "sure-chat").mkdir()
    settings = {
        "SECRET_KEY_BASE": secrets.token_hex(64),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        # Sure's chat isn't answered in the demo, but the compose file wants these.
        "CHAT_TOKEN": secrets.token_hex(32),
        "MCP_TOKEN": secrets.token_hex(32),
        "MCP_USER_EMAIL": SURE_EMAIL,
        "SURE_PORT": str(SURE_PORT),
        "SURE_BRAND": str(ROOT / "deploy" / "sure" / "brand"),
        "TALLY_CHAT_DIR": str(out / "sure-chat"),
        # Plain http on this machine, with nothing in front to do HTTPS.
        "RAILS_ASSUME_SSL": "false",
    }
    (out / "sure.env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
    print("Starting Sure in Docker (the first time downloads it, about 2 GB)...", flush=True)
    subprocess.run([*_compose(out), "up", "--detach", "--wait"], check=True)
    url = f"http://127.0.0.1:{SURE_PORT}"
    _wait_for(url)

    accounts = [
        {
            "name": name,
            "type": kind,
            "subtype": subtype,
            "balance": str(balance),
            "opened": opened.isoformat(),
        }
        for name, kind, subtype, balance in SURE_ACCOUNTS.values()
    ]
    spec = {
        "email": SURE_EMAIL,
        "password": SURE_PASSWORD,
        "timezone": _timezone(),
        "accounts": accounts,
    }
    made = subprocess.run(
        [*_compose(out), "exec", "--no-TTY", "--env", "DEMO", "web"]
        + ["./bin/rails", "runner", "eval(STDIN.read)"],
        input=SURE_SETUP,
        env={**os.environ, "DEMO": json.dumps(spec)},
        capture_output=True,
        text=True,
    )
    key = next(
        (
            line.removeprefix("API_KEY=")
            for line in made.stdout.splitlines()
            if line.startswith("API_KEY=")
        ),
        None,
    )
    if made.returncode or key is None:
        sys.exit(f"couldn't set up the demo's Sure:\n{made.stderr[-2000:]}")
    _link_sure(out, url, key)
    return sure.Client(url, key)


def _link_sure(out: Path, url: str, key: str) -> None:
    """Name each account's Sure account in the ledger, and save where Sure is and its key, so
    later syncs (`tally sync` with this folder) keep Sure up to date too."""
    path = out / "ledger" / "accounts.beancount"
    text = path.read_text()
    for account, (name, *_) in SURE_ACCOUNTS.items():
        line = next(
            f" open {account}{end}\n" for end in (" USD", "") if f" open {account}{end}\n" in text
        )
        text = text.replace(line, f'{line}  sure-account: "{name}"\n')
    path.write_text(text)
    with (out / "config.toml").open("a") as f:
        f.write(f'\nsure_url = "{url}"\n')
    (out / "secrets").mkdir(mode=0o700, exist_ok=True)
    (out / "secrets" / sure.API_KEY_FILE).write_text(key + "\n")
    (out / "secrets" / sure.API_KEY_FILE).chmod(0o600)


def _no_sure_yet() -> None:
    """Stop if an earlier demo's Sure is still there: its database has that demo's user."""
    left = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={SURE_PROJECT}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if left:
        sys.exit(
            "An earlier demo's Sure is still there. Remove it first with the command that demo"
            f" printed, or: docker compose --project-name {SURE_PROJECT} down --volumes"
        )


def _wait_for(url: str) -> None:
    """Wait until Sure answers: it sets up its database on the first start."""
    for _ in range(120):
        try:
            if requests.get(f"{url}/up", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    sys.exit(f"Sure didn't start at {url}; see `docker compose -p {SURE_PROJECT} logs web`")


def _timezone() -> str:
    """This machine's time zone (like America/New_York), so Sure's days are yours."""
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


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
    args = sys.argv[1:]
    with_sure = "--sure" in args
    folders = [a for a in args if a != "--sure"]
    if len(folders) != 1:
        sys.exit(__doc__)
    main(Path(folders[0]), with_sure)
