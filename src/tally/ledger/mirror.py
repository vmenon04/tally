"""Keeping Sure's transactions the same as the ledger's.

Sure shows what the ledger holds; it is never the record. An account takes part once its
open directive names the Sure account it fills:

    2026-08-31 open Assets:BofA:Checking USD
      tally-account: "bofa_checking"
      sure-account: "BofA Checking"

Every posting tally imported to such an account (posted rows and pending alerts alike) is
one Sure transaction, on the day of the purchase, named after the merchant and in the
entry's category. tally marks the ones it makes (source "tally", and an external ID from
the entry's tally-id), and only ever changes or deletes those.

Each run compares the two and makes Sure match: missing transactions are created, changed
ones corrected, and ones the ledger no longer has (a pending alert that posted) deleted.
A category already set in Sure is left alone, since that is where you correct them.

It also compares each account's balance in Sure with the ledger's, so a difference the
transactions can't show is caught: Sure works out balances from an opening balance it
keeps itself, and ignores transactions dated before it. Only accounts this run leaves
alone are compared, since Sure works out balances again some time after a change.
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from beancount.core import data

from tally.ledger import book, entries
from tally.rules import UNCATEGORIZED
from tally.sure import Client, SureError

SOURCE = "tally"
ACCOUNT_KEY = "sure-account"


@dataclass(frozen=True, slots=True)
class Row:
    """One transaction Sure should have. amount is Sure's way round: money out is positive."""

    account: str  # the Sure account's name
    external_id: str
    date: str
    amount: Decimal
    name: str
    category: str | None


@dataclass(slots=True)
class Plan:
    create: list[Row] = field(default_factory=list)
    # Sure's transaction ID, what it should say, and whether to fill in its category.
    update: list[tuple[str, Row, bool]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    same: int = 0
    kept: int = 0  # categories set in Sure that differ from the ledger's, left alone
    accounts: dict[str, str] = field(default_factory=dict)  # Sure account name -> ID
    # Sure account name, its balance in Sure, and the ledger's (Sure's way round).
    differ: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)  # Sure category name -> ID

    @property
    def changes(self) -> int:
        return len(self.create) + len(self.update) + len(self.delete)

    def summary(self) -> str:
        return (
            f"{len(self.create)} to add, {len(self.update)} to correct, "
            f"{len(self.delete)} to remove, {self.same} already right"
            + (f"; {self.kept} with a category you set in Sure, kept" if self.kept else "")
        )


def _names(ledger: book.Ledger) -> dict[str, str]:
    """Each ledger account that fills a Sure account, and that Sure account's name."""
    names = {
        entry.account: str(entry.meta[ACCOUNT_KEY])
        for entry in ledger.entries
        if isinstance(entry, data.Open) and ACCOUNT_KEY in (entry.meta or {})
    }
    if not names:
        raise SureError(f"no open directive in the ledger has a {ACCOUNT_KEY} line")
    return names


def rows(ledger: book.Ledger) -> list[Row]:
    """What Sure should hold, from the ledger's imported entries. Raises SureError."""
    names = _names(ledger)
    seen: Counter[tuple[str, str]] = Counter()
    found = []
    for entry in ledger.entries:
        if not isinstance(entry, data.Transaction) or entries.ID_KEY not in entry.meta:
            continue
        date = entry.meta.get(entries.PURCHASED_KEY, entry.date)
        for posting in entry.postings:
            if posting.account not in names or posting.units is None or not posting.units.number:
                continue
            others = [p.account for p in entry.postings if p is not posting]
            category = next(
                (
                    c
                    for c in map(ledger.accounts.category_of, (*others, posting.account))
                    if c is not None
                ),
                None,
            )
            # Two equal coffees on one day share a tally-id; the second gets "-2".
            key = (names[posting.account], entry.meta[entries.ID_KEY])
            seen[key] += 1
            found.append(
                Row(
                    account=key[0],
                    external_id=key[1] if seen[key] == 1 else f"{key[1]}-{seen[key]}",
                    date=date.isoformat(),
                    amount=-posting.units.number,
                    name=entry.payee or entry.narration or "",
                    category=None if category == UNCATEGORIZED else category,
                )
            )
    return found


def balances(ledger: book.Ledger) -> dict[str, Decimal]:
    """Each Sure account's balance in the ledger, as the ledger signs it."""
    names = _names(ledger)
    found: dict[str, Decimal] = {name: Decimal(0) for name in names.values()}
    for entry in ledger.entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                if posting.account in names and posting.units is not None:
                    found[names[posting.account]] += posting.units.number or Decimal(0)
    return found


def plan(
    wanted: list[Row], client: Client, ledger_balances: dict[str, Decimal] | None = None
) -> Plan:
    """Compare with what Sure has. Reads only. Raises SureError."""
    theirs_accounts = list(client.each("/api/v1/accounts", "accounts"))
    result = Plan(accounts={str(a["name"]): str(a["id"]) for a in theirs_accounts})
    unknown = sorted({row.account for row in wanted} - set(result.accounts))
    if unknown:
        raise SureError(f"Sure has no account named {', '.join(unknown)}")
    result.categories = {
        str(c["name"]): str(c["id"]) for c in client.each("/api/v1/categories", "categories")
    }
    ours = {
        (str(t["account"]["name"]), str(t["external_id"])): t
        for t in client.each("/api/v1/transactions", "transactions")
        if t.get("source") == SOURCE
    }
    for row in wanted:
        theirs = ours.pop((row.account, row.external_id), None)
        if theirs is None:
            result.create.append(row)
            continue
        category = (theirs.get("category") or {}).get("name")
        fill = row.category is not None and category is None
        if row.category is not None and category not in (None, row.category):
            result.kept += 1
        if _same(row, theirs) and not fill:
            result.same += 1
        else:
            result.update.append((str(theirs["id"]), row, fill))
    result.delete = [str(t["id"]) for t in ours.values()]
    changed = {row.account for row in result.create} | {row.account for _, row, _ in result.update}
    changed |= {str(t["account"]["name"]) for t in ours.values()}
    for account in theirs_accounts:
        name = str(account["name"])
        if ledger_balances is None or name not in ledger_balances or name in changed:
            continue
        if "balance_cents" not in account:
            continue
        theirs = Decimal(int(account["balance_cents"])) / 100
        mine = ledger_balances[name]
        mine = -mine if account.get("classification") == "liability" else mine
        if theirs != mine:
            result.differ.append((name, theirs, mine))
    return result


def apply(found: Plan, client: Client) -> None:
    """Make Sure match. Raises SureError; what was done so far stays, and a rerun finishes."""
    for name in sorted({row.category for row in found.create if row.category}):
        _category(found, client, name)
    for row in found.create:
        body = {
            **_fields(row),
            "account_id": found.accounts[row.account],
            "external_id": row.external_id,
            "source": SOURCE,
        }
        if row.category:
            body["category_id"] = found.categories[row.category]
        client.request("POST", "/api/v1/transactions", body={"transaction": body})
    for sure_id, row, fill in found.update:
        body = _fields(row)
        if fill and row.category:
            body["category_id"] = _category(found, client, row.category)
        client.request("PATCH", f"/api/v1/transactions/{sure_id}", body={"transaction": body})
    for sure_id in found.delete:
        client.request("DELETE", f"/api/v1/transactions/{sure_id}")


def _fields(row: Row) -> dict[str, Any]:
    return {"date": row.date, "amount": float(row.amount), "name": row.name}


def _category(found: Plan, client: Client, name: str) -> str:
    if name not in found.categories:
        made = client.request("POST", "/api/v1/categories", body={"category": {"name": name}})
        found.categories[name] = str(made["id"])
    return found.categories[name]


def _same(row: Row, theirs: dict[str, Any]) -> bool:
    try:
        amount = Decimal(str(theirs["amount_decimal"]))
    except (KeyError, InvalidOperation):
        return False
    return (
        amount == row.amount and theirs.get("date") == row.date and theirs.get("name") == row.name
    )
