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
from pathlib import Path
from typing import Any, TextIO

from beancount.core import data

from tally import categorizing, corrections, matching, pending, rules, store
from tally.alerts import Alert
from tally.ledger import accounts, book, categories, history, importers, parity
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
    """Commit and push what tally changed. A failure is reported; the next run retries."""
    if not history.is_repo(folder):
        return
    try:
        history.commit(folder, message)
        history.push(folder)
    except history.HistoryError as e:
        print(f"ledger: {e}; the next run tries again", file=err)


@dataclass(slots=True)
class SyncResult:
    kept: list[str] = field(default_factory=list)  # pending categories you set, now rules
    carried: list[str] = field(default_factory=list)  # moved to the rows that posted
    guessed: list[str] = field(default_factory=list)  # Claude's new merchant rules
    claude_failed: str | None = None
    changes: list[categories.Change] = field(default_factory=list)
    check: parity.Parity | None = None


def sync(
    data_dir: Path, conn: sqlite3.Connection, ask: Ask, *, dry_run: bool, err: TextIO
) -> SyncResult | None:
    """`tally sync`: bring the ledger's categories up to date, then check it.

    1. categories you set on pending entries become rules (they'd be lost otherwise);
    2. rules made on pending alerts move to the rows that have posted since;
    3. Claude categorizes merchants nothing covers yet;
    4. imported entries still in Uncategorized move to the category the rules now give;
    5. pending.beancount is rewritten, the ledger committed and pushed, and checked.

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
        ledger = book.with_pending(ledger, book.pending_entries(ledger, waiting, month_rules))
        result.check = parity.compare(ledger, counted)
        return result

    if text != _rules_text(data_dir):
        corrections.save(data_dir / RULES, text)
    categories.apply(result.changes)
    book.write_pending(ledger, waiting, month_rules)
    if result.changes:
        save(ledger.folder, f"Categorize {len(result.changes)} entries", err)
    result.check = parity.compare(book.load(ledger.folder, month_rules), counted)
    return result


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
    return parity.compare(ledger, [*stored, *(pending.as_transaction(a) for a in waiting)])


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
