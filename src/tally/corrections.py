"""Turning category corrections made on the Sheet into rules in rules.toml.

A correction becomes a rule pinned to that one transaction (account, date, amount and the
exact merchant), in a marked section at the top of the rules file so it wins over the
general rules. tally only ever edits inside that section; the rest of the file is yours.
Clearing a category on the Sheet removes its correction. Correcting a transaction that one
of Claude's rules categorized changes that rule instead, so the merchant's later purchases
follow the correction too (see categorizing).

A pending row (a card alert no CSV reaches yet) can be corrected too; its rule is pinned to
the alert's merchant, date and amount. The bank's row for the purchase may be named, dated
and priced differently, so once a CSV brings it in, carry_over moves the correction from the
alert to the posted row.
"""

import datetime
import json
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tally import categorizing, rules
from tally.transaction import Transaction

SECTION_START = (
    "# ---- Corrections from the Sheet (tally sync adds these; edit or delete freely) ----"
)
SECTION_END = "# ---- End of corrections ----"

_RULE_START = re.compile(r"^\[\[rule\]\]", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Result:
    text: str  # the whole new rules file
    messages: list[str]
    suggestions: list[str]


def apply(text: str, corrections: list[tuple[Transaction, str]], today: datetime.date) -> Result:
    """Add, replace or (for an empty category) remove the correction rule of each transaction."""
    text = _with_section(text)
    messages = []
    for t, category in corrections:
        text, removed = _remove(text, t)
        label = f"{t.merchant} ({t.date:%m/%d/%Y}, {t.amount})"
        if not category:
            messages.append(f"Removed correction: {label}" if removed else f"No change: {label}")
            continue
        if category not in _allowed(text):
            raise rules.RulesError(f"{label}: {category!r} is not a category")
        text, followed = categorizing.follow_correction(text, t, category)
        if followed:
            messages.append(f"Changed Claude's rule for {t.merchant} to {category}: {label}")
            continue
        if rules.parse(text).categorize(t) == category:
            messages.append(f"No rule needed, the rules already say {category}: {label}")
            continue
        text = _insert(text, _rule_text(t, category, today))
        messages.append(f"Added rule: {label} → {category}")
    rules.parse(text)  # never hand back a file that doesn't load
    return Result(text, messages, _suggestions(text))


def carry_over(
    text: str, posted_alerts: list[tuple[Transaction, Transaction]], today: datetime.date
) -> Result:
    """Move corrections made on pending rows to the posted rows that took their place.

    posted_alerts pairs each alert (as its pending transaction) with the posted row matched
    to it. A posted row corrected on its own keeps its own correction.
    """
    rules.parse(text)  # a broken file is reported as such, not as a broken correction
    if SECTION_START not in text:
        return Result(text, [], [])
    messages = []
    for alert, row in posted_alerts:
        category = _pinned_category(text, alert)
        if category is None:
            continue
        text, _ = _remove(text, alert)
        label = f"{row.merchant} ({row.date:%m/%d/%Y}, {row.amount})"
        if _pinned_category(text, row) is not None:
            messages.append(f"Kept the posted row's own correction: {label}")
        elif rules.parse(text).categorize(row) == category:
            messages.append(f"Posted, and the rules already say {category}: {label}")
        else:
            text = _insert(text, _rule_text(row, category, today, "carried over from its alert"))
            messages.append(f"Posted, correction carried over: {label} → {category}")
    if messages:
        rules.parse(text)
    return Result(text, messages, _suggestions(text))


def save(path: Path, text: str) -> None:
    """Replace the rules file in one step, readable only by you."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _with_section(text: str) -> str:
    if SECTION_START in text:
        if SECTION_END not in text.split(SECTION_START, 1)[1]:
            raise rules.RulesError(f"rules.toml has {SECTION_START!r} but not {SECTION_END!r}")
        return text
    section = f"{SECTION_START}\n\n{SECTION_END}\n\n"
    first_rule = _RULE_START.search(text)
    if first_rule is None:
        return text.rstrip("\n") + "\n\n" + section
    # Go above the comments and blank lines leading into the first rule (like a heading).
    lines = text[: first_rule.start()].split("\n")
    while len(lines) > 1 and (lines[-2].strip() == "" or lines[-2].lstrip().startswith("#")):
        lines.pop(-2)
    head = "\n".join(lines[:-1]).rstrip("\n") + "\n\n"
    return head + section + text[len(head.rstrip("\n")) :].lstrip("\n")


def _section(text: str) -> tuple[str, str, str]:
    """(before, inside, after) the corrections section."""
    before, rest = text.split(SECTION_START, 1)
    inside, after = rest.split(SECTION_END, 1)
    return before + SECTION_START, inside, SECTION_END + after


def _blocks(inside: str) -> list[str]:
    starts = [m.start() for m in _RULE_START.finditer(inside)]
    ends = [*starts[1:], len(inside)] if starts else []
    return [inside[a:b] for a, b in zip(starts, ends, strict=True)]


def _matches(block: str, t: Transaction) -> bool:
    data = tomllib.loads(block, parse_float=Decimal)["rule"][0]
    pinned = (data.get("account"), data.get("date"), data.get("amount"), data.get("merchant"))
    return pinned == (t.account, t.date, t.amount, t.merchant)


def _pinned_category(text: str, t: Transaction) -> str | None:
    """The category of the correction pinned to this transaction, if there is one."""
    _, inside, _ = _section(text)
    for block in _blocks(inside):
        if _matches(block, t):
            category: str = tomllib.loads(block)["rule"][0]["category"]
            return category
    return None


def _remove(text: str, t: Transaction) -> tuple[str, bool]:
    before, inside, after = _section(text)
    kept = [b for b in _blocks(inside) if not _matches(b, t)]
    removed = len(kept) != len(_blocks(inside))
    return before + "\n\n" + "".join(kept) + after, removed


def _insert(text: str, block: str) -> str:
    before, inside, after = _section(text)
    return before + "\n\n" + "".join(_blocks(inside)) + block + after


def _rule_text(
    t: Transaction, category: str, today: datetime.date, how: str = "corrected on the Sheet"
) -> str:
    return (
        f"[[rule]]  # {how} {today.isoformat()}\n"
        f"account = {json.dumps(t.account)}\n"
        f"date = {t.date.isoformat()}\n"
        f"amount = {t.amount}\n"
        f"merchant = {json.dumps(t.merchant)}\n"
        f"category = {json.dumps(category)}\n\n"
    )


def _allowed(text: str) -> tuple[str, ...]:
    return (*rules.parse(text).categories, *rules.NOT_SPENDING)


def _suggestions(text: str) -> list[str]:
    """A merchant-wide rule for each merchant corrected to the same category twice or more."""
    _, inside, _ = _section(text)
    counts: Counter[tuple[str, str]] = Counter()
    for block in _blocks(inside):
        data = tomllib.loads(block)["rule"][0]
        if "merchant" in data and "date" in data:
            counts[(data["merchant"], data["category"])] += 1
    return [
        f"You've corrected {merchant} to {category} {n} times. To make that the rule for every "
        f"purchase there, add this to rules.toml (below the corrections section):\n"
        f"  [[rule]]\n  merchant = {json.dumps(merchant)}\n  category = {json.dumps(category)}"
        for (merchant, category), n in sorted(counts.items())
        if n >= 2
    ]
