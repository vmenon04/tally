"""Sorting transactions into categories with rules from a TOML file.

    categories = ["Groceries", "Eating out / dates"]

    [[rule]]
    match = "CHIPOTLE"              # merchant contains this (any case)
    category = "Eating out / dates"

    [[rule]]
    merchant = "VENMO"              # merchant is exactly this (any case)
    account = "bofa_checking"       # optional narrowing: account, date, amount
    date = 2026-09-29
    amount = -50.00
    category = "Groceries"

The first matching rule wins, so one-off rules go above general ones. Categories are worked
out whenever transactions are read, so editing a rule re-sorts past transactions too.
"""

import datetime
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tally.importers import bofa, discover
from tally.transaction import Transaction

# Kinds of money movement that aren't spending, available to every rules file.
NOT_SPENDING = ("Income", "Card payment", "Savings transfer", "Investing")
UNCATEGORIZED = "Uncategorized"
ACCOUNTS = (bofa.ACCOUNT, discover.ACCOUNT)

_RULE_KEYS = {"match", "merchant", "account", "date", "amount", "category"}


class RulesError(ValueError):
    """The rules file is malformed or refers to something that doesn't exist."""


@dataclass(frozen=True, slots=True)
class Rule:
    category: str
    match: str | None = None
    merchant: str | None = None
    account: str | None = None
    date: datetime.date | None = None
    amount: Decimal | None = None

    def matches(self, t: Transaction) -> bool:
        merchant = t.merchant.casefold()
        return (
            (self.match is None or self.match.casefold() in merchant)
            and (self.merchant is None or self.merchant.casefold() == merchant)
            and (self.account is None or self.account == t.account)
            and (self.date is None or self.date == t.date)
            and (self.amount is None or self.amount == t.amount)
        )


@dataclass(frozen=True, slots=True)
class Rules:
    categories: tuple[str, ...]  # spending categories, in Budget tab order
    rules: tuple[Rule, ...]

    def categorize(self, t: Transaction) -> str:
        return next((r.category for r in self.rules if r.matches(t)), UNCATEGORIZED)


def load(path: Path) -> Rules:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RulesError(f"no rules file at {path}") from None
    return parse(text)


def parse(text: str) -> Rules:
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as e:
        raise RulesError(f"not valid TOML: {e}") from e

    unknown = set(data) - {"categories", "rule"}
    if unknown:
        raise RulesError(f"unknown top-level keys: {sorted(unknown)}")

    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise RulesError("categories must be a non-empty list of names")
    for name in categories:
        if not isinstance(name, str) or not name.strip():
            raise RulesError(f"category names must be non-empty text, got {name!r}")
        if name in NOT_SPENDING or name == UNCATEGORIZED:
            raise RulesError(f"{name!r} is built in; don't list it under categories")
    if len(set(categories)) != len(categories):
        raise RulesError("categories has duplicates")

    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise RulesError("rules must be written as [[rule]] tables")
    allowed = (*categories, *NOT_SPENDING)
    rules = tuple(_parse_rule(raw, n, allowed) for n, raw in enumerate(raw_rules, start=1))
    return Rules(tuple(categories), rules)


def _parse_rule(raw: dict[str, Any], n: int, allowed: tuple[str, ...]) -> Rule:
    def fail(message: str) -> RulesError:
        return RulesError(f"rule {n}: {message}")

    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise fail(f"unknown keys {sorted(unknown)}")

    category = raw.get("category")
    if category not in allowed:
        raise fail(f"category {category!r} is not one of {list(allowed)}")

    match, merchant = raw.get("match"), raw.get("merchant")
    if (match is None) == (merchant is None):
        raise fail("needs exactly one of match or merchant")
    text = match if match is not None else merchant
    if not isinstance(text, str) or not text.strip():
        raise fail("match and merchant must be non-empty text")

    account = raw.get("account")
    if account is not None and account not in ACCOUNTS:
        raise fail(f"account {account!r} is not one of {list(ACCOUNTS)}")

    date = raw.get("date")
    if date is not None and (
        not isinstance(date, datetime.date) or isinstance(date, datetime.datetime)
    ):
        raise fail(f"date must be written like 2026-09-29, got {date!r}")

    amount = raw.get("amount")
    if amount is not None:
        if isinstance(amount, bool) or not isinstance(amount, Decimal | int):
            raise fail(f"amount must be a number like -50.00, got {amount!r}")
        amount = Decimal(amount)

    return Rule(category, match, merchant, account, date, amount)
