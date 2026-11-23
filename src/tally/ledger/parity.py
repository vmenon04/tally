"""Checking the ledger holds exactly what tally imported, and that bean-check is clean.

- rows: every posted row and waiting alert tally knows has exactly one ledger entry with its
  tally-id, and the ledger has no tally entries tally doesn't know (a row lost or doubled
  on the way in);
- bean-check: the ledger has no errors (failed balances above all);
- transfers: every Assets:Transfers account is back to zero within a week. Money waits there
  until the other side shows up (a card payment, a transfer Fidelity's holdings will show),
  so one that stays means a side that never came: an account tally doesn't know, a transfer
  between two Fidelity accounts, a row lost on the way in. Only posted entries count.

What the bank itself says each account holds is compared on each sync (see updating).
"""

import datetime
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from beancount.core import data

from tally.ledger import book, entries
from tally.transaction import Transaction, identity


@dataclass(frozen=True, slots=True)
class Parity:
    rows: int  # tally's rows the ledger has
    missing: list[Transaction] = field(default_factory=list)  # in tally, not the ledger
    extra: int = 0  # tally entries in the ledger that tally doesn't have
    errors: list[str] = field(default_factory=list)  # bean-check's
    # Assets:Transfers accounts that haven't been zero for a week: (account, held, since)
    stuck: list[tuple[str, Decimal, datetime.date]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.errors or self.stuck)

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
        for account, held, since in self.stuck:
            found.append(
                f"{account} has held {held:,.2f} USD since {since:%b %d}: one side of a "
                "transfer never showed up."
            )
        return found

    def details(self) -> list[str]:
        lines = [f"  missing: {t.date}  {t.amount:>10}  {t.merchant}" for t in self.missing]
        lines += [f"  bean-check: {message}" for message in self.errors]
        return lines


def compare(
    ledger: book.Ledger, counted: Sequence[Transaction], today: datetime.date | None = None
) -> Parity:
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
    return Parity(
        sum(theirs.values()) - extra,
        missing,
        extra,
        list(ledger.errors),
        stuck_transfers(ledger, today or datetime.datetime.now().astimezone().date()),
    )


TRANSFERS = "Assets:Transfers:"
PATIENCE = datetime.timedelta(days=7)
_CENT = Decimal("0.005")


def stuck_transfers(
    ledger: book.Ledger, today: datetime.date
) -> list[tuple[str, Decimal, datetime.date]]:
    """Assets:Transfers accounts holding money since over a week ago, and since when."""
    held: dict[str, Decimal] = defaultdict(Decimal)
    since: dict[str, datetime.date] = {}
    posted = sorted(
        (
            e
            for e in ledger.entries
            if isinstance(e, data.Transaction)
            and e.date <= today
            and not book.is_pending(ledger, e)
        ),
        key=lambda e: e.date,
    )
    for e in posted:
        for p in e.postings:
            if not p.account.startswith(TRANSFERS) or p.units is None:
                continue
            was_zero = abs(held[p.account]) < _CENT
            held[p.account] += p.units.number or Decimal(0)
            if abs(held[p.account]) < _CENT:
                since.pop(p.account, None)
            elif was_zero:
                since[p.account] = e.date
    return sorted(
        (account, held[account], day) for account, day in since.items() if day <= today - PATIENCE
    )
