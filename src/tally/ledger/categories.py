"""Keeping the categories of imported ledger entries in step with tally's rules.

During the parallel run, rules.toml (with your corrections from the Sheet and Claude's
guesses) decides every category, and the Sheet follows it on each sync. The ledger gets the
category a row had when it was imported, so `tally sync` moves the other side of any
imported entry whose category has changed since. Only the account name on that posting
line is changed, so anything else you wrote there stays.

Entries tally didn't write (no tally-id), and ones you split into more than two postings,
are left alone.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from beancount.core import data

from tally.ledger import book, entries
from tally.rules import Rules
from tally.transaction import Transaction


@dataclass(frozen=True, slots=True)
class Change:
    path: Path
    line: int  # 1-based
    old: str
    new: str
    payee: str


def plan(ledger: book.Ledger, rows: Sequence[Transaction], rules: Rules) -> list[Change]:
    """The posting lines to change so each imported entry has its row's current category."""
    by_id = {entries.identity(t): t for t in rows}
    imported = (ledger.folder / book.IMPORTED).resolve()
    changes = []
    for entry in ledger.entries:
        if not isinstance(entry, data.Transaction) or len(entry.postings) != 2:
            continue
        t = by_id.get(entry.meta.get(entries.ID_KEY, ""))
        path = Path(entry.meta.get("filename", ""))
        if t is None or path.resolve().parent != imported:
            continue
        source = ledger.accounts.source(t.account)
        others = [p for p in entry.postings if p.account != source]
        if len(others) != 1 or others[0].meta is None:
            continue
        expected = ledger.accounts.counter(rules.categorize(t), t.merchant)
        if others[0].account != expected:
            line = int(others[0].meta["lineno"])
            changes.append(Change(path, line, others[0].account, expected, entry.payee or ""))
    return changes


def apply(changes: Sequence[Change]) -> None:
    """Rename the account on each changed posting line, checking each line is the one meant."""
    by_path: dict[Path, list[Change]] = {}
    for change in changes:
        by_path.setdefault(change.path, []).append(change)
    for path, file_changes in by_path.items():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for change in file_changes:
            line = lines[change.line - 1]
            indent = line[: len(line) - len(line.lstrip())]
            rest = line.lstrip()
            if not indent or not (rest.startswith(change.old + " ") or rest.rstrip() == change.old):
                raise book.LedgerError(
                    f"{path.name}:{change.line} isn't the posting to {change.old} it was"
                )
            lines[change.line - 1] = indent + change.new + rest[len(change.old) :]
        book.write_text(path, "".join(lines))


def describe(change: Change) -> str:
    return f"{change.payee}: {_short(change.old)} → {_short(change.new)}"


def _short(account: str) -> str:
    return account.split(":", 1)[1] if ":" in account else account
