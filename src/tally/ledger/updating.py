"""Keeping the ledger in step with tally.db as exports and alerts come in.

tally.db stays the record the Sheet is built from during the parallel run; the ledger gets
the same rows from the same files. Nothing here runs unless data/ledger/main.beancount
exists, so tally works as before without a ledger.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from beancount.core import data

from tally import matching, rules, store
from tally.ledger import accounts, book, importers


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
