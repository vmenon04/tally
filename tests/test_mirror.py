import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tally import rules, sure
from tally.ledger import book, mirror

ACCOUNTS = """
2026-01-01 open Assets:BofA:Checking USD
  tally-account: "bofa_checking"
  sure-account: "Checking"
2026-01-01 open Liabilities:Discover:Card USD
  tally-account: "discover"
  sure-account: "Card"
2026-01-01 open Assets:BofA:Savings USD
  category: "Savings transfer"
  sure-account: "Savings"
2026-01-01 open Assets:Transfers:Discover USD
  category: "Card payment"
2026-01-01 open Assets:Elsewhere USD
  category: "Investing"
2026-01-01 open Income:Salary
  category: "Income"
2026-01-01 open Expenses:Groceries
  category: "Groceries"
2026-01-01 open Expenses:Uncategorized
  category: "Uncategorized"
"""
ENTRIES = """
2026-03-03 * "MAPLE MARKET" "MAPLE MARKET 03/02 PURCHASE"
  tally-id: "id-market"
  purchased: 2026-03-02
  Assets:BofA:Checking  -45.10 USD
  Expenses:Groceries     45.10 USD
2026-03-05 ! "NEW PLACE" "NEW PLACE"
  tally-id: "id-new"
  Liabilities:Discover:Card  -30.00 USD
  Expenses:Uncategorized      30.00 USD
2026-03-06 * "KEEP THE CHANGE" "KEEP THE CHANGE"
  tally-id: "id-change"
  Assets:BofA:Checking  -0.55 USD
  Assets:BofA:Savings    0.55 USD
2026-03-07 * "CAFE" "CAFE"
  tally-id: "id-cafe"
  Liabilities:Discover:Card  -4.00 USD
  Expenses:Uncategorized      4.00 USD
2026-03-07 * "CAFE" "CAFE"
  tally-id: "id-cafe"
  Liabilities:Discover:Card  -4.00 USD
  Expenses:Uncategorized      4.00 USD
2026-03-08 * "Written by hand"
  Assets:BofA:Checking  -1.00 USD
  Assets:Elsewhere       1.00 USD
"""


@dataclass(frozen=True, slots=True)
class FakeSure(sure.Client):
    """Sure's API in memory: accounts, categories and transactions, two per page."""

    url: str = "http://sure.test"
    api_key: str = "key"
    accounts: tuple[str, ...] = ("Checking", "Card", "Savings")
    categories: dict[str, str] = field(default_factory=dict)  # ID -> name
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        self.calls.append(f"{method} {path}")
        name = path.removeprefix("/api/v1/")
        if method == "GET":
            items: list[dict[str, Any]] = {
                "accounts": [{"id": f"a-{n}", "name": n} for n in self.accounts],
                "categories": [{"id": i, "name": n} for i, n in self.categories.items()],
                "transactions": list(self.transactions.values()),
            }[name]
            page = int((params or {})["page"])
            return {
                name: items[(page - 1) * 2 : page * 2],
                "pagination": {"total_pages": max(1, -(-len(items) // 2))},
            }
        if (method, name) == ("POST", "categories"):
            new_id = f"c-{len(self.categories) + 1}"
            self.categories[new_id] = body["category"]["name"]
            return {"id": new_id, "name": self.categories[new_id]}
        if (method, name) == ("POST", "transactions"):
            sent = body["transaction"]
            new_id = f"t-{len(self.calls)}"
            self.transactions[new_id] = {
                "id": new_id,
                "account": {"name": sent["account_id"].removeprefix("a-")},
                "external_id": sent["external_id"],
                "source": sent["source"],
                "category": None,
            }
            self._write(new_id, sent)
            return self.transactions[new_id]
        found = re.fullmatch(r"transactions/(t-\d+|theirs)", name)
        assert found, f"unexpected {method} {path}"
        if method == "DELETE":
            del self.transactions[found[1]]
            return None
        assert method == "PATCH"
        self._write(found[1], body["transaction"])
        return self.transactions[found[1]]

    def _write(self, key: str, sent: dict[str, Any]) -> None:
        t = self.transactions[key]
        t.update(date=sent["date"], amount_decimal=str(sent["amount"]), name=sent["name"])
        if "category_id" in sent:
            t["category"] = {"name": self.categories[sent["category_id"]]}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "main.beancount").write_text(
        'include "accounts.beancount"\ninclude "e.beancount"\n'
    )
    (tmp_path / "accounts.beancount").write_text(ACCOUNTS)
    (tmp_path / "e.beancount").write_text(ENTRIES)
    return tmp_path


def load(folder: Path) -> book.Ledger:
    ledger = book.load(folder, rules.parse('categories = ["Groceries"]\n'))
    assert not ledger.errors
    return ledger


def push(folder: Path, fake: FakeSure) -> mirror.Plan:
    found = mirror.plan(mirror.rows(load(folder)), fake)
    mirror.apply(found, fake)
    return found


def test_rows_are_the_imported_postings_on_accounts_sure_has(folder: Path) -> None:
    rows = mirror.rows(load(folder))
    assert rows[0] == mirror.Row(
        account="Checking",
        external_id="id-market",
        date="2026-03-02",  # the purchase, not the posting
        amount=Decimal("45.10"),  # money out is positive in Sure
        name="MAPLE MARKET",
        category="Groceries",
    )
    assert [(r.account, r.external_id, r.amount, r.category) for r in rows[1:]] == [
        ("Card", "id-new", Decimal("30.00"), None),  # pending, and Uncategorized is no category
        ("Checking", "id-change", Decimal("0.55"), "Savings transfer"),
        ("Savings", "id-change", Decimal("-0.55"), "Savings transfer"),  # both sides of a transfer
        ("Card", "id-cafe", Decimal("4.00"), None),
        ("Card", "id-cafe-2", Decimal("4.00"), None),  # an identical row keeps its own ID
    ]  # and nothing for the entry written by hand


def test_first_push_creates_everything_and_the_second_finds_nothing(folder: Path) -> None:
    fake = FakeSure()
    first = push(folder, fake)
    assert (len(first.create), len(first.update), len(first.delete)) == (6, 0, 0)
    assert sorted(fake.categories.values()) == ["Groceries", "Savings transfer"]
    assert {t["source"] for t in fake.transactions.values()} == {"tally"}

    fake.calls.clear()
    second = push(folder, fake)
    assert (second.changes, second.same) == (0, 6)
    assert all(call.startswith("GET ") for call in fake.calls)
    assert second.summary() == "0 to add, 0 to correct, 0 to remove, 6 already right"


def test_changes_in_the_ledger_reach_sure(folder: Path) -> None:
    fake = FakeSure()
    push(folder, fake)
    text = ENTRIES.replace("Expenses:Uncategorized      30.00", "Expenses:Groceries      30.00")
    text = text.replace("-45.10", "-46.10").replace(" 45.10", " 46.10")  # a tip was added
    text = text[: text.index('2026-03-06 * "KEEP')] + text[text.index('2026-03-07 * "CAFE"') :]
    (folder / "e.beancount").write_text(text)

    found = push(folder, fake)
    assert (len(found.create), len(found.update), len(found.delete)) == (0, 2, 2)
    by_id = {t["external_id"]: t for t in fake.transactions.values()}
    assert by_id["id-market"]["amount_decimal"] == "46.1"
    assert by_id["id-new"]["category"] == {"name": "Groceries"}
    assert "id-change" not in by_id
    assert push(folder, fake).changes == 0


def test_a_category_set_in_sure_is_kept(folder: Path) -> None:
    fake = FakeSure()
    push(folder, fake)
    market = next(t for t in fake.transactions.values() if t["external_id"] == "id-market")
    market["category"] = {"name": "Eating out"}

    found = push(folder, fake)
    assert (found.changes, found.kept) == (0, 1)
    assert market["category"] == {"name": "Eating out"}
    assert found.summary().endswith("; 1 with a category you set in Sure, kept")


def test_transactions_tally_didnt_make_are_never_touched(folder: Path) -> None:
    fake = FakeSure()
    fake.transactions["theirs"] = {
        "id": "theirs",
        "account": {"name": "Checking"},
        "external_id": None,
        "source": None,
        "date": "2026-03-01",
        "amount_decimal": "9.0",
        "name": "Typed into Sure",
        "category": None,
    }
    assert push(folder, fake).delete == []
    assert "theirs" in fake.transactions


def test_an_account_sure_lacks_is_named(folder: Path) -> None:
    with pytest.raises(sure.SureError, match="no account named Savings"):
        mirror.plan(mirror.rows(load(folder)), FakeSure(accounts=("Checking", "Card")))


def test_a_ledger_that_names_no_sure_account_says_so(folder: Path) -> None:
    accounts = folder / "accounts.beancount"
    accounts.write_text(re.sub(r"  sure-account: .*\n", "", accounts.read_text()))
    with pytest.raises(sure.SureError, match="no open directive in the ledger has a sure-account"):
        mirror.rows(load(folder))
