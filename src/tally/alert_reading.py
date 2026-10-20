"""Read saved alert emails: pick the bank's parser, store the alerts, and list what's pending.

The bank is picked by the sender's domain, and its parser then checks Gmail's signature
result, so a forged From line only gets an email refused. An alert stays pending until a
posted row from a CSV export explains it.
"""

import datetime
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from tally import store
from tally.alerts import Alert, AlertError, bofa, discover, is_from, read
from tally.importing import private_dir
from tally.transaction import Transaction

_PARSERS: dict[str, Callable[[bytes], Alert | None]] = {
    discover.DOMAIN: discover.parse,
    bofa.DOMAIN: bofa.parse,
}

# How far a posted row may be from its alert and still explain it. Tips and holds change
# the amount, and a purchase can post a few days after it is authorized.
MATCH_DAYS = 5
MATCH_DOLLARS = Decimal(5)


def pending(alerts: Sequence[Alert], transactions: Sequence[Transaction]) -> list[Alert]:
    """Alerts that no posted transaction explains yet.

    A transaction explains an alert on the same account within MATCH_DAYS days and
    MATCH_DOLLARS of its amount. Each transaction explains at most one alert: the first
    one (oldest first) it is close to, picking the closest amount, then the closest date.
    """
    unused = list(transactions)
    left = []
    for a in alerts:
        candidates = [
            t
            for t in unused
            if t.account == a.account
            and abs((t.date - a.date).days) <= MATCH_DAYS
            and abs(t.amount - a.amount) <= MATCH_DOLLARS
        ]
        if not candidates:
            left.append(a)
            continue
        unused.remove(
            min(candidates, key=lambda t: (abs(t.amount - a.amount), abs((t.date - a.date).days)))
        )
    return left


def run(data_dir: Path, *, dry_run: bool, out: TextIO, err: TextIO) -> bool:
    """Read every .eml file in data_dir/alerts and store the alerts in them.

    The files are left in place. Returns False if any email was refused.
    """
    folder = data_dir / "alerts"
    paths = sorted(folder.glob("*.eml")) if folder.is_dir() else []
    if not paths:
        print(f"Nothing to read: no .eml files in {folder}.", file=out)
        return True

    db_path = data_dir / "tally.db"
    if dry_run:
        print("Dry run: nothing will be saved.", file=out)
        conn = store.connect_copy(db_path)
    else:
        private_dir(data_dir, "alerts")
        conn = store.connect(db_path)
    try:
        counts: Counter[str] = Counter()
        for path in paths:
            outcome = _read_one(conn, path, out, err)
            counts[outcome] += 1
        print(
            f"\n{counts['added']} added, {counts['already stored']} already stored, "
            f"{counts['skipped']} skipped, {counts['refused']} refused.",
            file=out,
        )
        left = pending(store.alerts(conn), store.transactions(conn))
        print(f"\nPending: {len(left)} alert(s) not in a CSV import yet", file=out)
        for a in left:
            print(f"  {a.date}  {a.account:13}  {a.amount:>8}  {a.merchant}", file=out)
    finally:
        conn.close()
    return counts["refused"] == 0


def _read_one(conn: sqlite3.Connection, path: Path, out: TextIO, err: TextIO) -> str:
    try:
        raw = path.read_bytes()
    except OSError as e:
        print(f"{path.name}: refused: {e}", file=err)
        return "refused"
    message = read(raw)
    parse = next((p for domain, p in _PARSERS.items() if is_from(message, domain)), None)
    if parse is None:
        print(f"{path.name}: skipped, not from Discover or Bank of America", file=out)
        return "skipped"
    try:
        alert = parse(raw)
    except AlertError as e:
        print(f"{path.name}: refused: {e}", file=err)
        return "refused"
    if alert is None:
        print(f"{path.name}: skipped, not a transaction alert", file=out)
        return "skipped"
    new = store.save_alert(conn, alert, now=datetime.datetime.now(datetime.UTC))
    outcome = "added" if new else "already stored"
    print(
        f"{path.name}: {outcome}  {alert.account}  {alert.date}  {alert.amount}  {alert.merchant}",
        file=out,
    )
    return outcome
