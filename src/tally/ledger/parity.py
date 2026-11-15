"""Checking the ledger holds exactly what tally imported, and that bean-check is clean.

- rows: every posted row and waiting alert tally knows has exactly one ledger entry with its
  tally-id, and the ledger has no tally entries tally doesn't know (a row lost or doubled
  on the way in);
- bean-check: the ledger has no errors (failed balances above all).

What the bank itself says each account holds is compared on each sync (see updating).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from beancount.core import data

from tally.ledger import book, entries
from tally.transaction import Transaction, identity


@dataclass(frozen=True, slots=True)
class Parity:
    rows: int  # tally's rows the ledger has
    missing: list[Transaction] = field(default_factory=list)  # in tally, not the ledger
    extra: int = 0  # tally entries in the ledger that tally doesn't have
    errors: list[str] = field(default_factory=list)  # bean-check's

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.errors)

    def problems(self) -> list[str]:
        """What's wrong, one sentence each, for the sync output and the weekly summary."""
        found = []
        if self.missing:
            found.append(f"The ledger is missing {len(self.missing)} imported transaction(s).")
        if self.extra:
            found.append(f"The ledger has {self.extra} imported entries tally doesn't know.")
        if self.errors:
            found.append(
                f"bean-check finds {len(self.errors)} problem(s) in the ledger, "
                f"first: {self.errors[0]}"
            )
        return found

    def details(self) -> list[str]:
        lines = [f"  missing: {t.date}  {t.amount:>10}  {t.merchant}" for t in self.missing]
        lines += [f"  bean-check: {message}" for message in self.errors]
        return lines


def compare(ledger: book.Ledger, counted: Sequence[Transaction]) -> Parity:
    """counted: the posted rows and waiting alerts tally knows."""
    ours = Counter(identity(t) for t in counted)
    theirs = Counter(
        e.meta[entries.ID_KEY]
        for e in ledger.entries
        if isinstance(e, data.Transaction) and entries.ID_KEY in e.meta
    )
    short = ours - theirs
    missing = []
    for t in counted:
        key = identity(t)
        if short[key] > 0:
            short[key] -= 1
            missing.append(t)
    extra = sum((theirs - ours).values())
    return Parity(sum(theirs.values()) - extra, missing, extra, list(ledger.errors))
