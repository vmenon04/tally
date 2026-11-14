"""Keeping the ledger up to date as exports and alerts come in, and on each sync.

The ledger is the record. tally.db keeps what tally needs to read files and alerts
(deduplicating rows, matching alerts to posted rows), and every row it takes in goes to the
ledger too. Categories are the ledger's: rules.toml (yours, and Claude's) only fills in the
ones nothing has decided yet. Nothing here runs unless data/ledger/main.beancount exists.
"""

import datetime
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from beancount.core import data

from tally import categorizing, corrections, matching, pending, rules, store
from tally.alerts import Alert, bofa
from tally.importers import ParseError
from tally.importers.fidelity import Snapshot
from tally.ledger import (
    accounts,
    book,
    categories,
    history,
    importers,
    mirror,
    parity,
    positions,
)
from tally.snaptrade import SnapTradeError
from tally.sure import Client as SureClient
from tally.sure import SureError
from tally.transaction import Transaction, identity

RULES = "rules.toml"

type Ask = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class Session:
    data_dir: Path
    ledger: book.Ledger
    rules: rules.Rules
    added: int = 0


def open_session(data_dir: Path, conn: sqlite3.Connection | None = None) -> Session | None:
    """The ledger and the rules, or None if there's no ledger.

    With conn, the rules carry categories set on pending alerts over to the rows that
    posted for them. Raises LedgerError, RulesError.
    """
    if not book.exists(data_dir):
        return None
    month_rules = rules_for(data_dir, conn) if conn is not None else rules.load(data_dir / RULES)
    return Session(data_dir, book.load(book.folder(data_dir), month_rules), month_rules)


def rules_for(data_dir: Path, conn: sqlite3.Connection) -> rules.Rules:
    """rules.toml, with corrections made on pending alerts moved to the rows that posted."""
    text = _rules_text(data_dir)
    stored = store.transactions(conn)
    alerts = store.alerts_with_ids(conn)
    return rules.parse(corrections.carry_over(text, pending.posted(alerts, stored), _today()).text)


def extract(
    session: Session, tally_account: str, text: str, file_name: str
) -> list[data.Directive]:
    """The entries a file would add to the ledger, without adding them."""
    if tally_account == "fidelity_positions":
        positions = importers.FidelityPositions(session.ledger.accounts)
        extracted = positions.extract_text(text, file_name, session.ledger.entries)
        return book.new_entries(extracted, session.ledger)
    importer = importers.for_account(tally_account, session.ledger.accounts, session.rules)
    return book.new_entries(importer.extract_text(text, file_name), session.ledger)


def add(session: Session, new: list[data.Directive]) -> None:
    book.add(session.ledger, new)
    session.ledger.entries.extend(new)
    session.added += len(new)


def refresh_pending(session: Session, conn: sqlite3.Connection) -> list[str]:
    """Keep categories you changed on pending entries, then rewrite pending.beancount.

    A change becomes a one-transaction rule in rules.toml, so it survives the rewrite and
    carries over to the posted row. Returns the messages for those changes.
    """
    messages = _keep_pending_fixes(session, conn)
    waiting = matching.match(store.alerts(conn), store.transactions(conn)).waiting
    book.write_pending(session.ledger, waiting, session.rules)
    return messages


def refresh_pending_in(data_dir: Path, conn: sqlite3.Connection, err: TextIO) -> None:
    """refresh_pending for the alerts run, reporting problems instead of stopping it."""
    try:
        session = open_session(data_dir, conn)
        if session is not None:
            refresh_pending(session, conn)
    except (book.LedgerError, rules.RulesError, accounts.AccountsError) as e:
        print(f"ledger: pending alerts not written: {e}", file=err)


class NotUpToDate(Exception):
    """The ledger has commits from elsewhere that can't be brought in."""


def bring_up_to_date(data_dir: Path, err: TextIO) -> None:
    """Before tally changes the ledger: commit edits made here, then pull ones made elsewhere.

    Raises NotUpToDate if pulling conflicts; an unreachable Forgejo is only reported.
    """
    folder = book.folder(data_dir)
    if not (book.exists(data_dir) and history.is_repo(folder)):
        return
    try:
        history.commit(folder, "Save changes made outside tally")
        history.pull(folder)
    except history.Unreachable as e:
        print(f"ledger: {e}; going on without the latest from Forgejo", file=err)
    except history.HistoryError as e:
        raise NotUpToDate(str(e)) from e


def save(folder: Path, message: str, err: TextIO) -> None:
    """Commit what tally changed, and push it to Forgejo and the backup.

    A failure is reported and the next run retries. Each push is tried on its own, so the
    backup still gets the commit when Forgejo is down.
    """
    if not history.is_repo(folder):
        return
    try:
        history.commit(folder, message)
    except history.HistoryError as e:
        print(f"ledger: {e}; the next run tries again", file=err)
        return
    for push in (history.push, history.push_backup):
        try:
            push(folder)
        except history.HistoryError as e:
            print(f"ledger: {e}; the next run tries again", file=err)


@dataclass(slots=True)
class SyncResult:
    kept: list[str] = field(default_factory=list)  # pending categories you set, now rules
    carried: list[str] = field(default_factory=list)  # moved to the rows that posted
    guessed: list[str] = field(default_factory=list)  # Claude's new merchant rules
    claude_failed: str | None = None
    changes: list[categories.Change] = field(default_factory=list)
    holdings: list[str] = field(default_factory=list)  # per account: cost and market value
    holdings_added: int = 0  # ledger entries from the brokerage snapshot
    holdings_failed: str | None = None
    check: parity.Parity | None = None
    sure: mirror.Plan | None = None  # what Sure needed to match the ledger
    sure_failed: str | None = None


def sync(
    data_dir: Path,
    conn: sqlite3.Connection,
    ask: Ask,
    *,
    dry_run: bool,
    err: TextIO,
    brokerage: Callable[[], Snapshot] | None = None,
    sure: SureClient | None = None,
) -> SyncResult | None:
    """`tally sync`: bring the ledger's categories up to date, then check it.

    1. categories you set on pending entries become rules (they'd be lost otherwise);
    2. rules made on pending alerts move to the rows that have posted since;
    3. Claude categorizes merchants nothing covers yet;
    4. imported entries still in Uncategorized move to the category the rules now give;
    5. pending.beancount is rewritten;
    6. with brokerage (SnapTrade), today's holdings are recorded, as a positions export would;
    7. the ledger is committed and pushed, and checked;
    8. with sure, Sure's transactions are made to match the ledger's.

    A dry run works it all out and saves nothing. None if there's no ledger.
    Raises LedgerError, AccountsError, RulesError or NotUpToDate.
    """
    if not book.exists(data_dir):
        return None
    if not dry_run:
        bring_up_to_date(data_dir, err)
    today = _today()
    text = _rules_text(data_dir)
    result = SyncResult()

    stored = store.transactions(conn)
    alerts = store.alerts_with_ids(conn)
    fixes = _pending_fixes(book.load(book.folder(data_dir), rules.parse(text)), alerts)
    if fixes:
        kept = corrections.apply(text, fixes, today)
        text, result.kept = kept.text, kept.messages
    carried = corrections.carry_over(text, pending.posted(alerts, stored), today)
    text, result.carried = carried.text, carried.messages
    waiting = matching.match([a for _, a in alerts], stored).waiting
    counted = [*stored, *(pending.as_transaction(a) for a in waiting)]
    try:
        guessed = categorizing.add(text, counted, ask, today)
        text, result.guessed = guessed.text, guessed.messages
    except categorizing.ClaudeError as e:
        result.claude_failed = str(e)
    month_rules = rules.parse(text)

    ledger = book.load(book.folder(data_dir), month_rules)
    result.changes = categories.plan(ledger, counted, month_rules)
    if dry_run:
        if brokerage is not None:
            _record_holdings(ledger, brokerage, result, dry_run=True)
        ledger = book.with_pending(ledger, book.pending_entries(ledger, waiting, month_rules))
        result.check = _compare(data_dir, ledger, counted)
        if sure is not None:
            _mirror(ledger, sure, result, dry_run=True)
        return result

    if text != _rules_text(data_dir):
        corrections.save(data_dir / RULES, text)
    categories.apply(result.changes)
    book.write_pending(ledger, waiting, month_rules)
    if brokerage is not None:
        _record_holdings(book.load(ledger.folder, month_rules), brokerage, result, dry_run=False)
    if result.changes or result.holdings_added:
        save(ledger.folder, "Bring the ledger up to date", err)
    ledger = book.load(ledger.folder, month_rules)
    result.check = _compare(data_dir, ledger, counted)
    if sure is not None:
        _mirror(ledger, sure, result, dry_run=False)
    return result


def _mirror(ledger: book.Ledger, sure: SureClient, result: SyncResult, *, dry_run: bool) -> None:
    """Make Sure match the ledger. A failure is noted, never raised: the next run finishes."""
    try:
        result.sure = mirror.plan(mirror.rows(ledger), sure)
        if not dry_run:
            mirror.apply(result.sure, sure)
    except SureError as e:
        result.sure_failed = str(e)


def _record_holdings(
    ledger: book.Ledger, brokerage: Callable[[], Snapshot], result: SyncResult, *, dry_run: bool
) -> None:
    """Add today's brokerage holdings to the ledger. A failure is noted, never raised."""
    try:
        snap = brokerage()
        extracted = positions.entries(snap, ledger.entries, ledger.accounts, "snaptrade")
    except (SnapTradeError, ParseError, accounts.AccountsError) as e:
        result.holdings_failed = str(e)
        return
    for account in snap.accounts:
        if not account.holdings and not account.cash:
            continue
        cost = sum((h.cost or 0 for h in account.holdings), Decimal(0))
        market = sum((h.quantity * h.price for h in account.holdings), Decimal(0))
        market += account.cash or 0
        known = (
            "" if all(h.cost is not None for h in account.holdings) else " (no cost basis given)"
        )
        result.holdings.append(
            f"{account.name} …{account.label}: cost {cost:,.2f}{known}, market {market:,.2f}"
        )
    new = book.new_entries(extracted, ledger)
    result.holdings_added = len(new)
    if not dry_run:
        book.add(ledger, new)


def check(data_dir: Path, conn: sqlite3.Connection) -> parity.Parity | None:
    """parity.compare without changing anything, with pending entries rebuilt in memory."""
    session = open_session(data_dir, conn)
    if session is None:
        return None
    stored = store.transactions(conn)
    waiting = matching.match(store.alerts(conn), stored).waiting
    ledger = book.with_pending(
        session.ledger, book.pending_entries(session.ledger, waiting, session.rules)
    )
    return _compare(data_dir, ledger, [*stored, *(pending.as_transaction(a) for a in waiting)])


def _compare(data_dir: Path, ledger: book.Ledger, counted: list[Transaction]) -> parity.Parity:
    """parity.compare, with the newest balance email the alerts mailbox has for each account."""
    emails = sorted((data_dir / "alerts" / "gmail").glob("*.eml"))
    return parity.compare(ledger, counted, bofa.latest_balances(emails))


def _keep_pending_fixes(session: Session, conn: sqlite3.Connection) -> list[str]:
    fixes = _pending_fixes(session.ledger, store.alerts_with_ids(conn))
    if not fixes:
        return []
    text = _rules_text(session.data_dir)
    kept = corrections.apply(text, fixes, _today())
    corrections.save(session.data_dir / RULES, kept.text)
    session.rules = rules_for(session.data_dir, conn)
    return kept.messages


def _pending_fixes(
    ledger: book.Ledger, alerts: Sequence[tuple[int, Alert]]
) -> list[tuple[Transaction, str]]:
    """(pending transaction, category) for each category you changed on a pending entry."""
    by_id = {identity(t): t for t in (pending.as_transaction(a) for _, a in alerts)}
    return [
        (by_id[key], category)
        for key, category in book.pending_fixes(ledger).items()
        if key in by_id
    ]


def _rules_text(data_dir: Path) -> str:
    try:
        return (data_dir / RULES).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise rules.RulesError(f"no rules file at {data_dir / RULES}") from None


def _today() -> datetime.date:
    return datetime.datetime.now().astimezone().date()
