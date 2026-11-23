"""Categories in the ledger: the ledger decides them, tally fills in the ones nobody has.

A category you set on an entry (in Fava, or by editing the file) stays. tally only moves
entries still in Uncategorized, once rules.toml has an answer for them (usually Claude's,
at the next sync). Only the account name on that posting line changes, so anything else you
wrote there stays.

Entries tally didn't write (no tally-id), and ones you split into more than two postings,
are left alone.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from beancount.core import data

from tally.ledger import book, entries
from tally.rules import UNCATEGORIZED, Rules
from tally.transaction import Transaction, identity


@dataclass(frozen=True, slots=True)
class Change:
    path: Path
    line: int  # 1-based
    old: str
    new: str
    payee: str


def in_ledger(ledger: book.Ledger) -> dict[str, str]:
    """Each tally entry's category as the ledger has it, by tally-id.

    A pending entry counts only where you changed its category: the rest of
    pending.beancount is tally's own output, which can be a few minutes behind the rules.
    """
    found = {}
    for entry, other in _tally_entries(ledger):
        category = ledger.accounts.category_of(other.account)
        # Uncategorized means nobody has decided yet: the rules may still have an answer.
        if category not in (None, UNCATEGORIZED) and not book.is_pending(ledger, entry):
            found[entry.meta[entries.ID_KEY]] = category
    return found | book.pending_fixes(ledger)


def plan(ledger: book.Ledger, rows: Sequence[Transaction], rules: Rules) -> list[Change]:
    """The imported entries still in Uncategorized that the rules now have a category for."""
    by_id = {identity(t): t for t in rows}
    imported = (ledger.folder / book.IMPORTED).resolve()
    only_rules = Rules(rules.categories, rules.rules)
    changes = []
    for entry, other in _tally_entries(ledger):
        t = by_id.get(entry.meta[entries.ID_KEY])
        path = Path(entry.meta.get("filename", ""))
        if t is None or path.resolve().parent != imported or other.meta is None:
            continue
        if ledger.accounts.category_of(other.account) != UNCATEGORIZED:
            continue
        category = only_rules.categorize(t)
        if category == UNCATEGORIZED:
            continue
        expected = ledger.accounts.counter(category, t.merchant, t.description)
        changes.append(
            Change(path, int(other.meta["lineno"]), other.account, expected, entry.payee or "")
        )
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


def _tally_entries(ledger: book.Ledger) -> list[tuple[data.Transaction, data.Posting]]:
    """Two-posting tally entries, each with its posting on the other side of its account."""
    sources = set(ledger.accounts.sources.values())
    found = []
    for e in ledger.entries:
        if not isinstance(e, data.Transaction) or entries.ID_KEY not in e.meta:
            continue
        others = [p for p in e.postings if p.account not in sources]
        if len(e.postings) == 2 and len(others) == 1:
            found.append((e, others[0]))
    return found


def _short(account: str) -> str:
    return account.split(":", 1)[1] if ":" in account else account
