"""The ledger folder on disk: loading it, adding imported entries, and rewriting pending.

    ledger/
      main.beancount        options, and includes for everything below
      accounts.beancount    open directives, with tally's mapping in their metadata
      opening.beancount     opening balances
      statements.beancount  balance assertions typed from statements
      imported/2026.beancount   added to by tally import, one file per year
      pending.beancount     rewritten by tally: alerts no export has reached yet (not in git)

tally only appends to imported/ and rewrites pending.beancount; every other file is yours.
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from beancount import loader
from beancount.core import data
from beancount.parser import printer

from tally.alerts import Alert
from tally.ledger import accounts, entries
from tally.pending import as_transaction
from tally.rules import Rules

MAIN = "main.beancount"
PENDING = "pending.beancount"
IMPORTED = "imported"

_HEADER = "; Written by tally import. Change categories freely; don't change tally-id.\n"
_PENDING_HEADER = (
    "; Rewritten by tally on every run: alert emails no bank export has reached yet.\n"
    "; Don't edit; each entry is replaced by its posted row once the export arrives.\n"
)


class LedgerError(ValueError):
    """The ledger can't be read, or doesn't give tally what it needs."""


@dataclass(frozen=True, slots=True)
class Ledger:
    folder: Path
    entries: list[data.Directive]
    errors: list[str]  # bean-check's errors as loaded
    accounts: accounts.Accounts

    @property
    def main(self) -> Path:
        return self.folder / MAIN


def folder(data_dir: Path) -> Path:
    return data_dir / "ledger"


def exists(data_dir: Path) -> bool:
    return (folder(data_dir) / MAIN).is_file()


def load(path: Path, rules: Rules) -> Ledger:
    """Load the ledger in path, creating pending.beancount if it's missing."""
    ensure_pending(path)
    loaded, errors, _ = loader.load_file(str(path / MAIN))
    try:
        found = accounts.from_entries(loaded)
        found.check(rules.categories)
    except accounts.AccountsError as e:
        raise LedgerError(f"{path / MAIN}: {e}") from e
    return Ledger(path, loaded, [_message(e) for e in errors], found)


def ensure_pending(path: Path) -> None:
    """pending.beancount isn't in git, but main.beancount includes it, so a clone needs one."""
    if not (path / PENDING).exists():
        write_text(path / PENDING, _PENDING_HEADER)


def check(path: Path) -> list[str]:
    """bean-check's errors for the ledger as it is on disk now."""
    _, errors, _ = loader.load_file(str(path / MAIN))
    return [_message(e) for e in errors]


def new_entries(extracted: Sequence[data.Directive], ledger: Ledger) -> list[data.Directive]:
    """The extracted entries the ledger doesn't have yet."""
    entries.mark_duplicates(extracted, ledger.entries)
    return [e for e in extracted if not e.meta.pop(entries.DUPLICATE_KEY, False)]


def add(ledger: Ledger, new: Sequence[data.Directive]) -> list[Path]:
    """Append entries to imported/<year>.beancount by their date. Returns the files changed."""
    by_year: dict[int, list[data.Directive]] = {}
    for entry in sorted(new, key=data.entry_sortkey):
        by_year.setdefault(entry.date.year, []).append(entry)
    changed = []
    for year, year_entries in sorted(by_year.items()):
        path = ledger.folder / IMPORTED / f"{year}.beancount"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        text = text if text.strip() else _HEADER
        text = text.rstrip("\n") + "\n\n" + "\n".join(_format(e) for e in year_entries)
        write_text(path, text)
        changed.append(path)
    return changed


def write_pending(ledger: Ledger, waiting: Sequence[Alert], rules: Rules) -> bool:
    """Rewrite pending.beancount with these alerts. Returns whether it changed.

    An alert is dated no earlier than its account's latest balance assertion: no export
    has it, so it posts after the period they cover, and a balance mustn't count it.
    """
    floors = _latest_balances(ledger.entries)
    pending = []
    for a in waiting:
        t = as_transaction(a)
        account = ledger.accounts.source(t.account)
        entry = entries.transaction(t, ledger.accounts, rules, flag="!")
        floor = floors.get(account)
        if floor is not None and floor > entry.date:
            entry.meta[entries.PURCHASED_KEY] = entry.date
            entry = entry._replace(date=floor)
        pending.append(entry)
    pending.sort(key=lambda e: (e.date, e.payee or "", e.meta[entries.ID_KEY]))
    text = _PENDING_HEADER + "".join("\n" + _format(e) for e in pending)
    path = ledger.folder / PENDING
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    write_text(path, text)
    return True


def _latest_balances(loaded: Sequence[data.Directive]) -> dict[str, datetime.date]:
    latest: dict[str, datetime.date] = {}
    for e in loaded:
        if isinstance(e, data.Balance) and e.date > latest.get(e.account, datetime.date.min):
            latest[e.account] = e.date
    return latest


def _format(entry: data.Directive) -> str:
    # Drop the filename and line number beangulp-style metadata carries.
    meta = {k: v for k, v in entry.meta.items() if k not in ("filename", "lineno")}
    return str(printer.format_entry(entry._replace(meta=meta)))


def _message(error: object) -> str:
    source = getattr(error, "source", None) or {}
    where = (
        f"{Path(source.get('filename', '')).name}:{source.get('lineno', '')}: " if source else ""
    )
    return f"{where}{getattr(error, 'message', error)}"


def write_text(path: Path, text: str) -> None:
    """Replace a file in one step, owner-only."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
