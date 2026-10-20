"""Read saved alert emails: pick the bank's parser, store the alerts, and list what's pending.

The bank is picked by the sender's domain, and its parser then checks Gmail's signature
result, so a forged From line only gets an email refused. An alert stays pending until a
posted row from a CSV export explains it (see matching).
"""

import datetime
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from tally import matching, store
from tally.alerts import Alert, AlertError, bofa, discover, is_from, read
from tally.importing import private_dir

_PARSERS: dict[str, Callable[[bytes], Alert | None]] = {
    discover.DOMAIN: discover.parse,
    bofa.DOMAIN: bofa.parse,
}


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
        _print_matching(matching.match(store.alerts(conn), store.transactions(conn)), out)
    finally:
        conn.close()
    return counts["refused"] == 0


def _print_matching(result: matching.Result, out: TextIO) -> None:
    loose = [m for m in result.matches if m.by_amount_only]
    print(
        f"\nMatched to posted rows: {len(result.matches)} alert(s), {len(loose)} by amount only",
        file=out,
    )
    for m in loose:
        print(
            f"  {_alert_line(m.alert)}  ->  {m.transaction.date}  {m.transaction.merchant}",
            file=out,
        )
    if result.not_found:
        print(
            f"\nNot found: {len(result.not_found)} alert(s) the CSVs should show by now "
            "(declined, reversed, or named differently?)",
            file=out,
        )
        for a in result.not_found:
            print(f"  {_alert_line(a)}", file=out)
    print(f"\nWaiting: {len(result.waiting)} alert(s) not in a CSV import yet", file=out)
    for a in result.waiting:
        print(f"  {_alert_line(a)}", file=out)


def _alert_line(a: Alert) -> str:
    return f"{a.date}  {a.account:13}  {a.amount:>8}  {a.merchant}"


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
