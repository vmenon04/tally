"""Keeping the ledger in step with tally.db as exports and alerts come in.

tally.db stays the record the Sheet is built from during the parallel run; the ledger gets
the same rows from the same files. Nothing here runs unless data/ledger/main.beancount
exists, so tally works as before without a ledger.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from beancount.core import data

from tally import matching, rules, store
from tally.alerts import Alert
from tally.ledger import accounts, book, categories, history, importers, parity
from tally.transaction import Transaction


@dataclass(slots=True)
class Session:
    ledger: book.Ledger
    rules: rules.Rules
    added: int = 0


def open_session(data_dir: Path) -> Session | None:
    """The ledger and rules, or None if there's no ledger. Raises LedgerError, RulesError."""
    if not book.exists(data_dir):
        return None
    month_rules = rules.load(data_dir / "rules.toml")
    return Session(book.load(book.folder(data_dir), month_rules), month_rules)


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


def refresh_pending(session: Session, conn: sqlite3.Connection) -> bool:
    """Rewrite pending.beancount from the alerts no export has reached. Returns if it changed."""
    alerts = store.alerts(conn)
    waiting = matching.match(alerts, store.transactions(conn)).waiting
    return book.write_pending(session.ledger, waiting, session.rules)


def refresh_pending_in(data_dir: Path, conn: sqlite3.Connection, err: TextIO) -> None:
    """refresh_pending for the alerts run, reporting problems instead of stopping it."""
    try:
        session = open_session(data_dir)
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


@dataclass(frozen=True, slots=True)
class SyncResult:
    changes: list[categories.Change]
    parity: parity.Parity


def sync(
    data_dir: Path,
    counted: list[Transaction],
    waiting: Sequence[Alert],
    month_rules: rules.Rules,
    *,
    dry_run: bool,
    err: TextIO,
) -> SyncResult | None:
    """For `tally sync`: move imported entries to their current categories, then compare.

    counted: posted rows and waiting alerts as tally counts them; waiting: those alerts.
    Pending entries are rebuilt from the current rules first, rather than trusting the last
    alerts run. A dry run compares the ledger as the moves would leave it.

    None if there's no ledger. Raises LedgerError, AccountsError or NotUpToDate.
    """
    if not book.exists(data_dir):
        return None
    if not dry_run:
        bring_up_to_date(data_dir, err)
    ledger = book.load(book.folder(data_dir), month_rules)
    changes = categories.plan(ledger, counted, month_rules)
    if dry_run:
        after = categories.applied(ledger, changes)
        after = book.with_pending(after, book.pending_entries(after, waiting, month_rules))
        return SyncResult(changes, parity.compare(after, counted, month_rules))
    if changes:
        categories.apply(changes)
        save(ledger.folder, f"Recategorize {len(changes)} entries", err)
    book.write_pending(ledger, waiting, month_rules)
    ledger = book.load(ledger.folder, month_rules)
    return SyncResult(changes, parity.compare(ledger, counted, month_rules))


def check(
    data_dir: Path,
    counted: list[Transaction],
    waiting: Sequence[Alert],
    month_rules: rules.Rules,
) -> parity.Parity | None:
    """parity.compare without changing anything, with pending entries rebuilt in memory."""
    if not book.exists(data_dir):
        return None
    ledger = book.load(book.folder(data_dir), month_rules)
    ledger = book.with_pending(ledger, book.pending_entries(ledger, waiting, month_rules))
    return parity.compare(ledger, counted, month_rules)
