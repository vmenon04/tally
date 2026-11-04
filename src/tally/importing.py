"""Import export files: recognize the bank, parse, store, and archive the original.

Files are recognized by their first line, never their name (BofA calls every export
stmt.csv). After a file is stored, a copy goes to raw/<account>/, so the database can always
be rebuilt from the downloads.
"""

import csv
import datetime
import hashlib
import io
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from beancount.core import data

from tally import rules, store
from tally.importers import ParseError, bofa, discover
from tally.ledger import accounts, book, updating
from tally.transaction import Transaction


@dataclass(frozen=True, slots=True)
class _Source:
    account: str
    parse: Callable[[str], list[Transaction]]


_SOURCES = {
    tuple(discover.HEADER): _Source(discover.ACCOUNT, discover.parse),
    tuple(bofa.SUMMARY_HEADER): _Source(bofa.ACCOUNT, bofa.parse),
}


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: Path
    sha256: str
    account: str
    transactions: list[Transaction]
    text: str


def read(path: Path) -> ParsedFile:
    """Parse a Discover or BofA export, whichever it is."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ParseError(f"not a text file ({e.reason})") from e
    first_line = next(csv.reader(io.StringIO(text.removeprefix("﻿"))), [])
    source = _SOURCES.get(tuple(first_line))
    if source is None:
        raise ParseError(f"not a Discover or BofA export; first line is {first_line}", line=1)
    return ParsedFile(
        path, hashlib.sha256(data).hexdigest(), source.account, source.parse(text), text
    )


def archive(parsed: ParsedFile, data_dir: Path, today: datetime.date) -> Path:
    """Copy the original file to data_dir/raw/<account>/<date>-<hash>.csv, once per content."""
    folder = data_dir / "raw" / parsed.account
    name = parsed.sha256[:16]
    existing = sorted(folder.glob(f"*-{name}.csv"))
    if existing:
        return existing[0]
    private_dir(data_dir, "raw", parsed.account)
    dest = folder / f"{today.isoformat()}-{name}.csv"
    shutil.copyfile(parsed.path, dest)
    dest.chmod(0o600)
    return dest


def private_dir(data_dir: Path, *parts: str) -> Path:
    """data_dir/<parts>, with data_dir and each part created if needed and owner-only (700).

    Folders that already exist are tightened too. mkdir's mode alone isn't enough: it is
    reduced by the umask and isn't applied to parents created along the way.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    path = data_dir
    for part in parts:
        path = path / part
        path.mkdir(exist_ok=True)
        path.chmod(0o700)
    return path


def run(
    paths: Sequence[Path],
    data_dir: Path,
    *,
    dry_run: bool,
    out: TextIO,
    err: TextIO,
    today: datetime.date | None = None,
) -> bool:
    """Import the given files, or everything in data_dir/inbox if none are given.

    Each file is stored completely or not at all. Files from the inbox are removed once
    stored and archived; files named explicitly are left where they are. Returns False if
    any file failed.
    """
    today = today or datetime.datetime.now().astimezone().date()
    inbox = data_dir / "inbox"
    from_inbox = not paths
    if from_inbox:
        if not dry_run:
            private_dir(data_dir, "inbox")
        paths = sorted(p for p in inbox.glob("*") if p.is_file() and not p.name.startswith("."))
        if not paths:
            print(f"Nothing to import: {inbox} is empty.", file=out)
            return True

    if not dry_run:
        try:
            updating.bring_up_to_date(data_dir, err)
        except updating.NotUpToDate as e:
            print(f"Nothing imported: the ledger can't be brought up to date: {e}", file=err)
            return False
    try:
        ledger = updating.open_session(data_dir)
    except (book.LedgerError, rules.RulesError) as e:
        print(f"Nothing imported: the ledger can't be read: {e}", file=err)
        return False

    ok = True
    parsed: list[ParsedFile] = []
    for path in paths:
        try:
            parsed.append(read(path))
        except (OSError, ParseError) as e:
            print(f"{path.name}: not imported: {e}", file=err)
            ok = False
    # Oldest first, so each BofA file can be checked against the history before it.
    parsed.sort(key=lambda p: min((t.date for t in p.transactions), default=datetime.date.min))

    db_path = data_dir / "tally.db"
    if dry_run:
        print("Dry run: nothing will be saved.", file=out)
    if dry_run:
        conn = store.connect_copy(db_path)
    else:
        private_dir(data_dir)
        conn = store.connect(db_path)
    try:
        for p in parsed:
            ok = _import_one(conn, p, data_dir, ledger, dry_run, from_inbox, today, out, err) and ok
        if ledger is not None and not dry_run:
            ok = _finish_ledger(ledger, conn, err) and ok
    finally:
        conn.close()
    return ok


def _finish_ledger(ledger: updating.Session, conn: sqlite3.Connection, err: TextIO) -> bool:
    """Bring pending alerts up to date, then report anything bean-check finds."""
    try:
        updating.refresh_pending(ledger, conn)
    except accounts.AccountsError as e:
        print(f"ledger: pending alerts not written: {e}", file=err)
        return False
    errors = book.check(ledger.ledger.folder)
    if errors:
        print(f"ledger: bean-check found {len(errors)} problem(s):", file=err)
        for message in errors[:10]:
            print(f"  {message}", file=err)
    if ledger.added:
        updating.save(ledger.ledger.folder, f"Import {ledger.added} entries", err)
    return True


def _import_one(
    conn: sqlite3.Connection,
    p: ParsedFile,
    data_dir: Path,
    ledger: updating.Session | None,
    dry_run: bool,
    from_inbox: bool,
    today: datetime.date,
    out: TextIO,
    err: TextIO,
) -> bool:
    new_entries: list[data.Directive] = []
    if ledger is not None:
        try:
            new_entries = updating.extract(ledger, p.account, p.text, p.path.name)
        except (ParseError, accounts.AccountsError) as e:
            print(f"{p.path.name}: not imported: ledger: {e}", file=err)
            return False
    try:
        result = store.save(
            conn,
            p.transactions,
            account=p.account,
            file_name=p.path.name,
            file_sha256=p.sha256,
        )
    except store.StoreError as e:
        print(f"{p.path.name}: not imported: {e}", file=err)
        return False

    dates = [t.date for t in p.transactions]
    span = f"{min(dates):%m/%d}–{max(dates):%m/%d}" if dates else "no rows"
    print(
        f"{p.path.name}  {p.account}  {span}  {result.rows_in_file} rows: "
        f"{result.rows_new} new, {result.rows_in_file - result.rows_new} already imported",
        file=out,
    )
    if ledger is not None:
        print(f"  ledger: {len(new_entries)} new entries", file=out)
    if dry_run:
        for t in result.new:
            print(f"  {t.date}  {t.amount:>10}  {t.merchant}", file=out)
        return True

    if ledger is not None:
        updating.add(ledger, new_entries)
    archive(p, data_dir, today)
    if from_inbox:
        p.path.unlink()
    return True
