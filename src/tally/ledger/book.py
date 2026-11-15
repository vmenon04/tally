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
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from beancount import loader
from beancount.core import data
from beancount.parser import printer

from tally.alerts import Alert
from tally.ledger import accounts, entries
from tally.pending import as_transaction
from tally.rules import Rules

WRITTEN_CATEGORY = "tally-category"
_TOTAL_COST = re.compile(r"\{# ([^}]+)\}")
MAIN = "main.beancount"
PENDING = "pending.beancount"
IMPORTED = "imported"

_HEADER = "; Written by tally import. Change categories freely; don't change tally-id.\n"
_PENDING_HEADER = (
    "; Rewritten by tally on every run: purchases the bank lists as pending.\n"
    "; Change a category here (in Fava) and tally keeps it, for the posted row too. Anything\n"
    "; else you change is replaced on the next run, and each entry by its posted row once it\n"
    "; posts.\n"
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


def pending_entries(
    ledger: Ledger, waiting: Sequence[Alert], rules: Rules
) -> list[data.Transaction]:
    """The alerts as flagged entries, the way pending.beancount holds them.

    An alert is dated no earlier than its account's latest balance assertion: no export
    has it, so it posts after the period they cover, and a balance mustn't count it.
    """
    floors = _latest_balances(e for e in ledger.entries if not is_pending(ledger, e))
    pending = []
    for a in waiting:
        t = as_transaction(a)
        account = ledger.accounts.source(t.account)
        entry = entries.transaction(t, ledger.accounts, rules, flag="!")
        # What tally gave it, so a category you change in Fava can be told from a rule change.
        entry.meta[WRITTEN_CATEGORY] = rules.categorize(t)
        floor = floors.get(account)
        if floor is not None and floor > entry.date:
            entry.meta[entries.PURCHASED_KEY] = entry.date
            entry = entry._replace(date=floor)
        pending.append(entry)
    pending.sort(key=lambda e: (e.date, e.payee or "", e.meta[entries.ID_KEY]))
    return pending


def pending_fixes(ledger: Ledger) -> dict[str, str]:
    """Categories you changed on pending entries since tally wrote them, by tally-id."""
    sources = set(ledger.accounts.sources.values())
    fixes = {}
    for e in ledger.entries:
        if not isinstance(e, data.Transaction) or not is_pending(ledger, e):
            continue
        written = e.meta.get(WRITTEN_CATEGORY)
        others = [p.account for p in e.postings if p.account not in sources]
        if written is None or entries.ID_KEY not in e.meta or len(others) != 1:
            continue
        now = ledger.accounts.category_of(others[0])
        if now is not None and now != written:
            fixes[e.meta[entries.ID_KEY]] = now
    return fixes


def write_pending(ledger: Ledger, waiting: Sequence[Alert], rules: Rules) -> bool:
    """Rewrite pending.beancount with these alerts. Returns whether it changed."""
    pending = pending_entries(ledger, waiting, rules)
    text = _PENDING_HEADER + "".join("\n" + _format(e) for e in pending)
    path = ledger.folder / PENDING
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    write_text(path, text)
    return True


def with_pending(ledger: Ledger, pending: Sequence[data.Directive]) -> Ledger:
    """The ledger with these pending entries in place of the ones on disk, for comparing."""
    kept = [e for e in ledger.entries if not is_pending(ledger, e)]
    return Ledger(ledger.folder, [*kept, *pending], ledger.errors, ledger.accounts)


def is_pending(ledger: Ledger, entry: data.Directive) -> bool:
    filename = (entry.meta or {}).get("filename", "")
    return bool(filename) and Path(filename).resolve() == (ledger.folder / PENDING).resolve()


def _latest_balances(loaded: Iterable[data.Directive]) -> dict[str, datetime.date]:
    latest: dict[str, datetime.date] = {}
    for e in loaded:
        if isinstance(e, data.Balance) and e.date > latest.get(e.account, datetime.date.min):
            latest[e.account] = e.date
    return latest


def _format(entry: data.Directive) -> str:
    # Drop the filename and line number beangulp-style metadata carries.
    meta = {k: v for k, v in entry.meta.items() if k not in ("filename", "lineno")}
    text = str(printer.format_entry(entry._replace(meta=meta)))
    # The printer writes a total cost as {# 10.00 USD}, which Beancount 3 doesn't read back.
    return _TOTAL_COST.sub(r"{{\1}}", text)


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
