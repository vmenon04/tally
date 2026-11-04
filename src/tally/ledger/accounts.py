"""Which ledger account each tally account and category goes to, read from the ledger itself.

The mapping lives in the ledger's open directives, so the ledger stays the one place that
names accounts and bean-check sees every account tally writes to:

    2026-09-19 open Assets:BofA:Checking USD
      tally-account: "bofa_checking"        # rows from this export post here

    2026-09-01 open Expenses:Groceries
      category: "Groceries"                 # rows in this category post their other side here

    2026-09-01 open Assets:BofA:Savings USD
      category: "Savings transfer"
      merchant-contains: "KEEP THE CHANGE"  # only for merchants containing this (any case)

A category can have several accounts told apart by merchant-contains, plus at most one
without it, which takes every other merchant.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from beancount.core import data

from tally.rules import NOT_SPENDING, UNCATEGORIZED


class AccountsError(ValueError):
    """The ledger's open directives don't give tally an account it needs."""


@dataclass(frozen=True, slots=True)
class _Choice:
    account: str
    merchant_contains: str | None


@dataclass(frozen=True, slots=True)
class Accounts:
    sources: dict[str, str] = field(default_factory=dict)  # tally account -> ledger account
    categories: dict[str, tuple[_Choice, ...]] = field(default_factory=dict)

    def source(self, tally_account: str) -> str:
        try:
            return self.sources[tally_account]
        except KeyError:
            raise AccountsError(f'no open directive has tally-account: "{tally_account}"') from None

    def counter(self, category: str, merchant: str) -> str:
        """The account a row's other side posts to."""
        choices = self.categories.get(category, ())
        folded = merchant.casefold()
        for choice in choices:
            if choice.merchant_contains and choice.merchant_contains.casefold() in folded:
                return choice.account
        for choice in choices:
            if choice.merchant_contains is None:
                return choice.account
        raise AccountsError(f'no open directive has category: "{category}" for {merchant!r}')

    def check(self, spending: Iterable[str]) -> None:
        """Every category tally can produce must have a catch-all account."""
        for category in (*spending, *NOT_SPENDING, UNCATEGORIZED):
            if not any(c.merchant_contains is None for c in self.categories.get(category, ())):
                raise AccountsError(
                    f'no open directive has category: "{category}" without merchant-contains'
                )


def from_entries(entries: Iterable[data.Directive]) -> Accounts:
    sources: dict[str, str] = {}
    categories: dict[str, list[_Choice]] = {}
    for entry in entries:
        if not isinstance(entry, data.Open):
            continue
        meta = entry.meta or {}
        tally_account = meta.get("tally-account")
        category = meta.get("category")
        contains = meta.get("merchant-contains")
        if tally_account is not None:
            _text(entry, "tally-account", tally_account)
            if tally_account in sources:
                raise AccountsError(
                    f'tally-account: "{tally_account}" is on both {sources[tally_account]} '
                    f"and {entry.account}"
                )
            sources[tally_account] = entry.account
        if contains is not None and category is None:
            raise AccountsError(f"{entry.account}: merchant-contains needs a category")
        if category is not None:
            _text(entry, "category", category)
            if contains is not None:
                _text(entry, "merchant-contains", contains)
            choices = categories.setdefault(category, [])
            if contains is None and any(c.merchant_contains is None for c in choices):
                raise AccountsError(
                    f'category: "{category}" has two accounts without merchant-contains'
                )
            choices.append(_Choice(entry.account, contains))
    return Accounts(sources, {k: tuple(v) for k, v in categories.items()})


def _text(entry: data.Open, key: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AccountsError(f"{entry.account}: {key} must be non-empty text")
