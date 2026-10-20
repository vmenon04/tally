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

from tally import store
from tally.importers import ParseError, bofa, discover
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
    return ParsedFile(path, hashlib.sha256(data).hexdigest(), source.account, source.parse(text))


def archive(parsed: ParsedFile, raw_dir: Path, today: datetime.date) -> Path:
    """Copy the original file to raw_dir/<account>/<date>-<hash>.csv, once per content."""
    folder = raw_dir / parsed.account
    name = parsed.sha256[:16]
    existing = sorted(folder.glob(f"*-{name}.csv"))
    if existing:
        return existing[0]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = folder / f"{today.isoformat()}-{name}.csv"
    shutil.copyfile(parsed.path, dest)
    return dest


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
            inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = sorted(p for p in inbox.glob("*") if p.is_file() and not p.name.startswith("."))
        if not paths:
            print(f"Nothing to import: {inbox} is empty.", file=out)
            return True

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
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = store.connect(db_path)
    try:
        for p in parsed:
            ok = _import_one(conn, p, data_dir, dry_run, from_inbox, today, out, err) and ok
    finally:
        conn.close()
    return ok


def _import_one(
    conn: sqlite3.Connection,
    p: ParsedFile,
    data_dir: Path,
    dry_run: bool,
    from_inbox: bool,
    today: datetime.date,
    out: TextIO,
    err: TextIO,
) -> bool:
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
    if dry_run:
        for t in result.new:
            print(f"  {t.date}  {t.amount:>10}  {t.merchant}", file=out)
        return True

    archive(p, data_dir / "raw", today)
    if from_inbox:
        p.path.unlink()
    return True
