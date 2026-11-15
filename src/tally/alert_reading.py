"""Read saved alert emails: pick the bank's parser, store the alerts, and list what's pending.

The bank is picked by the sender's domain, and its parser then checks Gmail's signature
result, so a forged From line only gets an email refused. An alert stays pending until a
posted row from a CSV export explains it (see matching).
"""

import datetime
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tally import gmail, matching, store
from tally.alerts import Alert, AlertError, bofa, discover, is_from, read
from tally.importing import private_dir
from tally.ledger import updating

_PARSERS: dict[str, Callable[[bytes], Alert | None]] = {
    discover.DOMAIN: discover.parse,
    bofa.DOMAIN: bofa.parse,
}
# The Gmail search for every email from a bank tally reads (its parsers check the rest).
QUERY = "from:(" + " OR ".join(_PARSERS) + ")"


def reread(data_dir: Path, *, dry_run: bool, out: TextIO, err: TextIO) -> bool:
    """Read again the emails already fetched from the alerts mailbox, and store their alerts.

    For emails skipped before a parser for them existed (alerts already stored are only
    counted). The files are left in place. Returns False if any email was refused.
    """
    folder = data_dir / "alerts" / "gmail"
    paths = sorted(folder.glob("*.eml")) if folder.is_dir() else []
    if not paths:
        print(f"Nothing to read: no .eml files in {folder}.", file=out)
        return True

    conn = _connect(data_dir, dry_run, out)
    try:
        counts: Counter[str] = Counter()
        for path in paths:
            try:
                raw = path.read_bytes()
            except OSError as e:
                print(f"{path.name}: refused: {e}", file=err)
                counts["refused"] += 1
                continue
            counts[_read_one(conn, path.name, raw, out, err)[0]] += 1
        _summary(conn, counts, out)
        if not dry_run:
            updating.refresh_pending_in(data_dir, conn, err)
    finally:
        conn.close()
    return counts["refused"] == 0


@dataclass(frozen=True, slots=True)
class GmailRun:
    added: list[Alert]
    refused: list[str]  # Gmail IDs of emails refused this run
    not_found: list[Alert]  # every stored alert the CSVs should show by now but don't
    failure: str | None = None  # why the mailbox couldn't be read, if it couldn't

    @property
    def ok(self) -> bool:
        return not self.refused and self.failure is None


def run_gmail(
    data_dir: Path,
    mailbox: gmail.Mailbox,
    address: str,
    *,
    dry_run: bool,
    out: TextIO,
    err: TextIO,
) -> GmailRun:
    """Fetch the bank emails tally hasn't read yet from the alerts mailbox, and store them.

    Each email read is kept in data_dir/alerts/gmail/<Gmail ID>.eml, which is also how the
    next run knows not to fetch it again. A refused email isn't kept, so every run reports
    it until it's dealt with, like a CSV that stays in the inbox.
    """
    archive = data_dir / "alerts" / "gmail"
    try:
        ids = mailbox.message_ids(QUERY)
    except gmail.GmailError as e:
        print(f"alerts: {e}", file=err)
        return GmailRun([], [], [], failure=str(e))
    unread = [i for i in reversed(ids) if not (archive / f"{i}.eml").exists()]
    print(f"Gmail {address}: {len(ids)} bank emails, {len(unread)} not read before.", file=out)

    conn = _connect(data_dir, dry_run, out)
    try:
        counts: Counter[str] = Counter()
        added: list[Alert] = []
        refused: list[str] = []
        failure = None
        for message_id in unread:
            try:
                raw = mailbox.raw(message_id)
            except gmail.GmailError as e:
                print(f"alerts: {e}", file=err)
                failure = str(e)
                break
            outcome, alert = _read_one(conn, f"gmail {message_id}", raw, out, err)
            counts[outcome] += 1
            if outcome == "added" and alert is not None:
                added.append(alert)
            if outcome == "refused":
                refused.append(message_id)
            elif not dry_run:
                _keep(data_dir, message_id, raw)
        result = _summary(conn, counts, out)
        if not dry_run:
            updating.refresh_pending_in(data_dir, conn, err)
    finally:
        conn.close()
    return GmailRun(added, refused, result.not_found, failure)


def _connect(data_dir: Path, dry_run: bool, out: TextIO) -> sqlite3.Connection:
    db_path = data_dir / "tally.db"
    if dry_run:
        print("Dry run: nothing will be saved.", file=out)
        return store.connect_copy(db_path)
    private_dir(data_dir, "alerts")
    return store.connect(db_path)


def _keep(data_dir: Path, message_id: str, raw: bytes) -> None:
    path = private_dir(data_dir, "alerts", "gmail") / f"{message_id}.eml"
    path.write_bytes(raw)
    path.chmod(0o600)


def _summary(conn: sqlite3.Connection, counts: Counter[str], out: TextIO) -> matching.Result:
    print(
        f"\n{counts['added']} added, {counts['already stored']} already stored, "
        f"{counts['skipped']} skipped, {counts['refused']} refused.",
        file=out,
    )
    result = matching.match(store.alerts(conn), store.transactions(conn), store.fetched(conn))
    _print_matching(result, out)
    return result


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
    if result.superseded:
        print(
            f"\nCovered by SimpleFIN: {len(result.superseded)} alert(s) from before its last "
            "answer, which lists what's still pending",
            file=out,
        )
    print(f"\nWaiting: {len(result.waiting)} alert(s) not in a CSV import yet", file=out)
    for a in result.waiting:
        print(f"  {_alert_line(a)}", file=out)


def _alert_line(a: Alert) -> str:
    return f"{a.date}  {a.account:13}  {a.amount:>8}  {a.merchant}"


def _read_one(
    conn: sqlite3.Connection, name: str, raw: bytes, out: TextIO, err: TextIO
) -> tuple[str, Alert | None]:
    """What happened to one email, and the alert in it if one was stored or already stored."""
    message = read(raw)
    parse = next((p for domain, p in _PARSERS.items() if is_from(message, domain)), None)
    if parse is None:
        print(f"{name}: skipped, not from Discover or Bank of America", file=out)
        return "skipped", None
    try:
        alert = parse(raw)
        stated = bofa.balance(raw) if alert is None else None
    except AlertError as e:
        print(f"{name}: refused: {e}", file=err)
        return "refused", None
    if stated is not None:
        # Kept with the others; the ledger check reads the newest (see parity).
        print(f"{name}: skipped, a balance: {stated.date}  {stated.amount}", file=out)
        return "skipped", None
    if alert is None:
        print(f"{name}: skipped, not a transaction alert", file=out)
        return "skipped", None
    new = store.save_alert(conn, alert, now=datetime.datetime.now(datetime.UTC))
    outcome = "added" if new else "already stored"
    print(
        f"{name}: {outcome}  {alert.account}  {alert.date}  {alert.amount}  {alert.merchant}",
        file=out,
    )
    return outcome, alert
