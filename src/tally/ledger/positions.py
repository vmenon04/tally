"""Fidelity positions as ledger entries: holdings at their cost basis, prices and balances.

A positions export is a snapshot, not a list of trades. For each account in it, one
transaction (tagged #fidelity-positions) brings the ledger to what the snapshot says:

- every holding whose quantity or cost basis changed is closed (`{}` takes all its lots)
  and reopened at Fidelity's quantity and total cost basis, so the ledger's cost basis is
  exactly Fidelity's (where the source gives no cost basis, new units count as bought at
  that day's price instead);
- the money market balance becomes the account's `:Cash` balance;
- the rest balances against Equity:Opening-Balances the first time (what the account held
  before the ledger), and Income:Fidelity:Unreconciled after that: dividends, fees and
  contributions made outside your bank accounts (a 401(k)'s payroll deductions), which a
  snapshot can't tell apart. Those later transactions are flagged `!`.

Money moved between cash and holdings balances itself, so contributions from BofA (already
in `:Cash`) aren't counted twice. It also adds each holding's price, and balance
assertions for the day after, so a later snapshot or a hand edit that disagrees fails
bean-check. Accounts are matched by `fidelity-account:` metadata, the last four characters
of the account number.
"""

import datetime
from collections.abc import Sequence
from decimal import Decimal

from beancount.core import amount, data, position
from beancount.core.number import MISSING

from tally.importers import ParseError
from tally.importers.fidelity import Holding, Snapshot
from tally.ledger.accounts import Accounts
from tally.ledger.entries import CURRENCY

TAG = "fidelity-positions"
LABEL_KEY = "fidelity-account"
OPENING = "Equity:Opening-Balances"
UNRECONCILED = "Income:Fidelity:Unreconciled"
_CENT = Decimal("0.005")


def entries(
    snapshot: Snapshot, existing: Sequence[data.Directive], found: Accounts, filename: str
) -> list[data.Directive]:
    day = snapshot.date
    after = day + datetime.timedelta(days=1)
    result: list[data.Directive] = []
    for account in snapshot.accounts:
        if (
            not account.holdings
            and not account.cash
            and account.label not in found.fidelity_accounts
        ):
            continue  # an empty account you don't track
        holdings_account = found.fidelity(account.label)
        cash_account = f"{holdings_account}:Cash"
        earlier = _snapshots(existing, account.label)
        if any(e.date > day for e in earlier):
            raise ParseError(
                f"the ledger already has a newer Fidelity snapshot for account …{account.label}"
            )
        held = _held(existing, holdings_account, day)
        postings: list[data.Posting] = []
        wanted = {h.symbol: h for h in account.holdings}
        for symbol in sorted({*held, *wanted}):
            units, cost = held.get(symbol, (Decimal(0), Decimal(0)))
            h = wanted.get(symbol)
            if h is not None and h.cost is None:
                postings += _without_cost_basis(holdings_account, symbol, units, cost, h)
                continue
            if h is not None and units == h.quantity and abs(cost - (h.cost or 0)) < _CENT:
                continue
            if units:
                postings.append(_posting(holdings_account, -units, symbol, _ALL_LOTS))
            if h is not None and h.quantity:
                total = position.CostSpec(None, h.cost, CURRENCY, None, None, False)
                postings.append(_posting(holdings_account, h.quantity, symbol, total))
        if account.cash is not None:
            difference = account.cash - _cash(existing, cash_account, day)
            if difference:
                postings.append(_posting(cash_account, difference, CURRENCY, None))
        meta = data.new_metadata(filename, 0)
        if postings:
            first = not earlier
            postings.append(
                data.Posting(OPENING if first else UNRECONCILED, None, None, None, None, None)
            )
            result.append(
                data.Transaction(
                    {**meta, LABEL_KEY: account.label},
                    day,
                    "*" if first else "!",
                    "Fidelity",
                    f"{account.name}: holdings as of {day:%b %d, %Y}",
                    frozenset({TAG}),
                    frozenset(),
                    postings,
                )
            )
        for h in account.holdings:
            result.append(data.Price(dict(meta), day, h.symbol, amount.Amount(h.price, CURRENCY)))
            result.append(
                data.Balance(
                    dict(meta),
                    after,
                    holdings_account,
                    amount.Amount(h.quantity, h.symbol),
                    None,
                    None,
                )
            )
        if account.cash is not None:
            result.append(
                data.Balance(
                    dict(meta),
                    after,
                    cash_account,
                    amount.Amount(account.cash, CURRENCY),
                    None,
                    None,
                )
            )
    return result


def _without_cost_basis(
    account: str, symbol: str, units: Decimal, cost: Decimal, h: Holding
) -> list[data.Posting]:
    """Postings for a holding whose brokerage gives no cost basis (a 401(k) through SnapTrade).

    New units are taken as bought at today's price, and sold units take their share of what
    the holding cost, so a change in price alone is never booked as income.
    """
    if h.quantity == units:
        return []
    if h.quantity > units:
        bought = position.CostSpec(h.price, None, CURRENCY, None, None, False)
        return [_posting(account, h.quantity - units, symbol, bought)]
    postings = [_posting(account, -units, symbol, _ALL_LOTS)]
    if h.quantity:
        kept = (cost * h.quantity / units).quantize(Decimal("0.01"))
        postings.append(
            _posting(
                account,
                h.quantity,
                symbol,
                position.CostSpec(None, kept, CURRENCY, None, None, False),
            )
        )
    return postings


# `{}`: whatever lots there are. Beancount's types don't admit its own MISSING marker.
_ALL_LOTS = position.CostSpec(MISSING, None, MISSING, None, None, False)  # type: ignore[arg-type]


def _posting(
    account: str, number: Decimal, currency: str, cost: position.CostSpec | None
) -> data.Posting:
    return data.Posting(account, amount.Amount(number, currency), cost, None, None, None)


def _snapshots(existing: Sequence[data.Directive], label: str) -> list[data.Transaction]:
    return [
        e
        for e in existing
        if isinstance(e, data.Transaction) and TAG in e.tags and e.meta.get(LABEL_KEY) == label
    ]


def _held(
    existing: Sequence[data.Directive], account: str, day: datetime.date
) -> dict[str, tuple[Decimal, Decimal]]:
    """Each commodity the account holds at the end of day: (units, total cost)."""
    held: dict[str, tuple[Decimal, Decimal]] = {}
    for e in existing:
        if not isinstance(e, data.Transaction) or e.date > day:
            continue
        for p in e.postings:
            if p.account != account or p.units is None or p.units.currency == CURRENCY:
                continue
            number = p.units.number or Decimal(0)
            per = p.cost.number if isinstance(p.cost, position.Cost) else Decimal(0)
            units, cost = held.get(p.units.currency, (Decimal(0), Decimal(0)))
            held[p.units.currency] = (units + number, cost + number * per)
    return {k: v for k, v in held.items() if v[0]}


def _cash(existing: Sequence[data.Directive], account: str, day: datetime.date) -> Decimal:
    total = Decimal(0)
    for e in existing:
        if isinstance(e, data.Transaction) and e.date <= day:
            for p in e.postings:
                if p.account == account and p.units is not None and p.units.currency == CURRENCY:
                    total += p.units.number or Decimal(0)
    return total
