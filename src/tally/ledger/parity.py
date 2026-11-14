"""Checking the ledger holds exactly what tally imported, and that bean-check is clean.

- rows: every posted row and waiting alert tally knows has exactly one ledger entry with its
  tally-id, and the ledger has no tally entries tally doesn't know (a row lost or doubled
  on the way in);
- bean-check: the ledger has no errors (failed balances above all);
- bank balances: each account's newest BofA balance email agrees with the ledger. The email
  comes early in the day it names, so it's compared with the ledger before that day, with
  each purchase on the day it was made. Money that moved without an alert (a transfer, a
  card payment) shows up here until a CSV import brings it in.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from beancount.core import data

from tally.alerts.bofa import Balance
from tally.ledger import book, entries
from tally.transaction import Transaction, identity


@dataclass(frozen=True, slots=True)
class Parity:
    rows: int  # tally's rows the ledger has
    missing: list[Transaction] = field(default_factory=list)  # in tally, not the ledger
    extra: int = 0  # tally entries in the ledger that tally doesn't have
    errors: list[str] = field(default_factory=list)  # bean-check's
    balances: list[str] = field(default_factory=list)  # where the bank disagrees, a sentence each

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.errors or self.balances)

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
        return found + self.balances

    def details(self) -> list[str]:
        lines = [f"  missing: {t.date}  {t.amount:>10}  {t.merchant}" for t in self.missing]
        lines += [f"  bean-check: {message}" for message in self.errors]
        return lines


def compare(
    ledger: book.Ledger, counted: Sequence[Transaction], stated: Sequence[Balance] = ()
) -> Parity:
    """counted: the posted rows and waiting alerts tally knows; stated: the newest balance
    email for each account."""
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
    return Parity(
        sum(theirs.values()) - extra,
        missing,
        extra,
        list(ledger.errors),
        [problem for b in stated if (problem := _disagrees(ledger, b))],
    )


def _disagrees(ledger: book.Ledger, stated: Balance) -> str | None:
    account = ledger.accounts.bofa_accounts.get(stated.label)
    if account is None:
        return (
            f"Bank of America sent a balance for the account ending {stated.label}, but no "
            f'open directive has bofa-account: "{stated.label}".'
        )
    held = sum(
        (
            p.units.number
            for e in ledger.entries
            if isinstance(e, data.Transaction)
            and e.meta.get(entries.PURCHASED_KEY, e.date) < stated.date
            for p in e.postings
            if p.account == account and p.units is not None and p.units.number is not None
        ),
        Decimal(0),
    )
    if held == stated.amount:
        return None
    gap = stated.amount - held
    return (
        f"Bank of America said {account} had ${stated.amount:,.2f} on {stated.date:%b %d}, "
        f"but the ledger has ${held:,.2f} by then (${abs(gap):,.2f} "
        f"{'more' if gap > 0 else 'less'} at the bank): a CSV import should explain it."
    )
