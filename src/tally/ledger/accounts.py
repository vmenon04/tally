"""Which ledger account each tally account and category goes to, read from the ledger itself.

The mapping lives in the ledger's open directives, so the ledger stays the one place that
names accounts and bean-check sees every account tally writes to:

    2026-09-19 open Assets:BofA:Checking USD
      tally-account: "bofa_checking"        # rows from this export post here
      simplefin-account: "1234"             # and from SimpleFIN's account ending 1234

    2026-09-01 open Assets:CapitalOne:Savings USD
      simplefin-account: "5678"             # only its balance is checked against SimpleFIN's
      interest-from: "Income:Interest"      # and the interest it's paid comes from here

    2026-09-01 open Expenses:Groceries
      category: "Groceries"                 # rows in this category post their other side here

    2026-09-01 open Assets:Fidelity:Roth
      fidelity-account: "1234"              # Fidelity positions for the account ending 1234

    2026-09-01 open Assets:Fidelity:401k
      fidelity-account: "5678"
      contributions-from: "Income:Salary:401k"  # its payroll contributions come from here

    2026-09-01 open Assets:Fidelity:Roth
      fidelity-account: "8765"
      transfers-from: "Assets:Transfers:Fidelity"  # money from the bank waits here

    2026-09-01 open Assets:BofA:Savings USD
      category: "Savings transfer"
      merchant-contains: "KEEP THE CHANGE|SAV 1234"  # only merchants containing one of these

A category whose account has `fixed: TRUE` is a bill that comes once a month (rent,
utilities): the pace by today counts it in full instead of a little each day.

transfers-from is for a Fidelity account you pay into from the bank: the bank's side posts
to that clearing account (give it the category), and a positions snapshot moves the money on
once Fidelity shows it, since Fidelity often has it days after the bank says it left.

interest-from is for a savings account whose deposits tally already sees from the other side
(transfers out of checking): of its SimpleFIN rows, sync adds only the interest. It needs
simplefin-account, and can't go with tally-account, whose rows would bring the interest in
already.

merchant-contains matches any case, and | separates alternatives. A category can have
several accounts told apart by merchant-contains, plus at most one without it, which takes
every other merchant.
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

    def takes(self, merchant: str) -> bool:
        if self.merchant_contains is None:
            return False
        folded = merchant.casefold()
        return any(
            part.strip().casefold() in folded
            for part in self.merchant_contains.split("|")
            if part.strip()
        )


@dataclass(frozen=True, slots=True)
class Accounts:
    sources: dict[str, str] = field(default_factory=dict)  # tally account -> ledger account
    categories: dict[str, tuple[_Choice, ...]] = field(default_factory=dict)
    fidelity_accounts: dict[str, str] = field(default_factory=dict)  # last four -> account
    contributions_from: dict[str, str] = field(default_factory=dict)  # last four -> income
    fixed: frozenset[str] = frozenset()  # categories that are once-a-month bills
    simplefin: dict[str, str] = field(default_factory=dict)  # last four -> tally account
    # last four -> ledger account, for every account linked to SimpleFIN
    bank_accounts: dict[str, str] = field(default_factory=dict)
    interest_from: dict[str, str] = field(default_factory=dict)  # last four -> income account
    # last four -> the clearing account money from the bank waits in until Fidelity has it
    transfers_from: dict[str, str] = field(default_factory=dict)

    def fidelity(self, label: str) -> str:
        try:
            return self.fidelity_accounts[label]
        except KeyError:
            raise AccountsError(
                f'no open directive has fidelity-account: "{label}" (the account number\'s '
                "last four)"
            ) from None

    def source(self, tally_account: str) -> str:
        try:
            return self.sources[tally_account]
        except KeyError:
            raise AccountsError(f'no open directive has tally-account: "{tally_account}"') from None

    def counter(self, category: str, merchant: str) -> str:
        """The account a row's other side posts to."""
        choices = self.categories.get(category, ())
        for choice in choices:
            if choice.takes(merchant):
                return choice.account
        for choice in choices:
            if choice.merchant_contains is None:
                return choice.account
        raise AccountsError(f'no open directive has category: "{category}" for {merchant!r}')

    def category_of(self, account: str) -> str | None:
        """The category whose rows post their other side to this account, if any."""
        for category, choices in self.categories.items():
            if any(c.account == account for c in choices):
                return category
        return None

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
    fidelity: dict[str, str] = {}
    contributions: dict[str, str] = {}
    fixed: set[str] = set()
    simplefin: dict[str, str] = {}
    bank_accounts: dict[str, str] = {}
    interest: dict[str, str] = {}
    transfers: dict[str, str] = {}
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
        linked = meta.get("simplefin-account")
        if linked is not None:
            _text(entry, "simplefin-account", linked)
            if linked in bank_accounts:
                raise AccountsError(f'simplefin-account: "{linked}" is on two accounts')
            bank_accounts[linked] = entry.account
            if tally_account is not None:
                simplefin[linked] = tally_account
        income = meta.get("interest-from")
        if income is not None:
            _text(entry, "interest-from", income)
            if linked is None:
                raise AccountsError(f"{entry.account}: interest-from needs simplefin-account")
            if tally_account is not None:
                raise AccountsError(
                    f"{entry.account}: interest-from can't go with tally-account, whose rows "
                    "bring the interest in already"
                )
            interest[linked] = income
        label = meta.get("fidelity-account")
        if label is not None:
            _text(entry, "fidelity-account", label)
            if label in fidelity:
                raise AccountsError(f'fidelity-account: "{label}" is on two accounts')
            fidelity[label] = entry.account
        source = meta.get("contributions-from")
        if source is not None:
            _text(entry, "contributions-from", source)
            if label is None:
                raise AccountsError(f"{entry.account}: contributions-from needs fidelity-account")
            contributions[label] = source
        clearing = meta.get("transfers-from")
        if clearing is not None:
            _text(entry, "transfers-from", clearing)
            if label is None:
                raise AccountsError(f"{entry.account}: transfers-from needs fidelity-account")
            transfers[label] = clearing
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
            if meta.get("fixed") is True:
                fixed.add(category)
    return Accounts(
        sources,
        {k: tuple(v) for k, v in categories.items()},
        fidelity,
        contributions,
        frozenset(fixed),
        simplefin,
        bank_accounts,
        interest,
        transfers,
    )


def _text(entry: data.Open, key: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AccountsError(f"{entry.account}: {key} must be non-empty text")
