import datetime
from decimal import Decimal
from typing import Any

import pytest

from tally import rules, transactions_tab
from tally.transaction import Transaction
from tally.transactions_tab import HEADER, Row

RULES = rules.parse(
    'categories = ["Groceries"]\n[[rule]]\nmatch = "MARKET"\ncategory = "Groceries"\n'
)


def tx(merchant: str, day: int, amount: str = "-1.00") -> Transaction:
    return Transaction(
        account="discover",
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def test_rows_newest_first_with_categories() -> None:
    stored = [(1, tx("MARKET", 2)), (2, tx("ODD SHOP", 5)), (3, tx("MARKET", 5))]
    tab_rows = transactions_tab.rows(stored, RULES)
    assert [(r.id, r.category) for r in tab_rows] == [
        (3, "Groceries"),
        (2, "Uncategorized"),
        (1, "Groceries"),
    ]


def written_grid() -> list[list[object]]:
    return [
        list(HEADER),
        [46083, "Discover", "MARKET", -1, "Groceries", 1],
        [46086, "Discover", "ODD SHOP", -1, "Uncategorized", 2],
    ]


WRITTEN = {1: "Groceries", 2: "Uncategorized"}


def test_untouched_tab_has_no_edits() -> None:
    assert transactions_tab.find_edits(written_grid(), WRITTEN) == []
    assert transactions_tab.find_edits([], {}) == []


def test_changed_and_cleared_categories_are_edits() -> None:
    grid = written_grid()
    grid[1][4] = "Uncategorized"
    grid[2] = grid[2][:4]  # the API drops trailing empty cells
    edits = transactions_tab.find_edits(grid, WRITTEN)
    assert [(e.row_number, e.was, e.now) for e in edits] == [
        (2, "Groceries", "Uncategorized"),
        (3, None, ""),
    ]


def test_row_without_a_known_id_is_an_edit() -> None:
    grid: list[list[object]] = [
        *written_grid(),
        [46090, "Cash", "LEMONADE", -3, "Groceries"],
        ["", "", ""],
    ]
    [edit] = transactions_tab.find_edits(grid, WRITTEN)
    assert (edit.row_number, edit.merchant, edit.was) == (4, "LEMONADE", None)


def test_wrong_header_is_refused() -> None:
    grid = written_grid()
    grid[0][0] = "When"
    with pytest.raises(transactions_tab.TransactionsTabError, match="header row"):
        transactions_tab.find_edits(grid, WRITTEN)


def test_format_edits() -> None:
    grid: list[list[object]] = [*written_grid(), [46090, "Cash", "LEMONADE", -3, "Groceries"]]
    text = transactions_tab.format_edits(transactions_tab.find_edits(grid, WRITTEN))
    assert "1 row(s) tally didn't write:\n  row 4: LEMONADE" in text


def tab_rows() -> list[Row]:
    return [
        Row(9, tx("ODD SHOP", 5, "-12.34"), "Uncategorized"),
        Row(3, tx("MARKET", 2), "Groceries"),
    ]


def kinds(requests: list[dict[str, Any]]) -> list[str]:
    return [next(iter(r)) for r in requests]


def test_new_tab_is_added_with_the_given_id() -> None:
    requests = transactions_tab.requests(None, 42, tab_rows(), RULES)
    assert kinds(requests)[:3] == ["addSheet", "updateCells", "setDataValidation"]
    properties = requests[0]["addSheet"]["properties"]
    assert properties["sheetId"] == 42
    assert properties["title"] == "Transactions"
    assert properties["gridProperties"] == {"rowCount": 3, "columnCount": 6, "frozenRowCount": 1}


def test_existing_tab_is_resized_to_fit_exactly() -> None:
    requests = transactions_tab.requests(7, 42, tab_rows(), RULES)
    resize = requests[0]["updateSheetProperties"]
    assert resize["properties"] == {
        "sheetId": 7,
        "gridProperties": {"rowCount": 3, "columnCount": 6, "frozenRowCount": 1},
    }
    assert all(r[next(iter(r))].get("range", {}).get("sheetId", 7) == 7 for r in requests[1:])


def test_cells() -> None:
    update = transactions_tab.requests(7, 42, tab_rows(), RULES)[1]["updateCells"]
    assert update["range"]["endRowIndex"] == 3
    header, first, second = update["rows"]
    assert [c["userEnteredValue"]["stringValue"] for c in header["values"]] == HEADER
    date, account, merchant, amount, category, tid = first["values"]
    assert date["userEnteredValue"] == {"numberValue": 46086}  # 2026-03-05 as a Sheets date
    assert account["userEnteredValue"] == {"stringValue": "Discover"}
    assert merchant["userEnteredValue"] == {"stringValue": "ODD SHOP"}
    assert amount["userEnteredValue"] == {"numberValue": -12.34}
    assert category["userEnteredValue"] == {"stringValue": "Uncategorized"}
    assert tid["userEnteredValue"] == {"numberValue": 9}
    assert "backgroundColor" in category["userEnteredFormat"]
    assert "backgroundColor" not in second["values"][4]["userEnteredFormat"]
    assert "textFormat" not in category["userEnteredFormat"]


def test_pending_rows_are_marked() -> None:
    rows = [Row(-4, tx("MARKET", 6), "Groceries"), *tab_rows()]
    update = transactions_tab.requests(7, 42, rows, RULES)[1]["updateCells"]
    pending, posted = update["rows"][1]["values"], update["rows"][2]["values"]
    assert pending[1]["userEnteredValue"] == {"stringValue": "Discover (pending)"}
    assert pending[5]["userEnteredValue"] == {"numberValue": -4}
    assert all(c["userEnteredFormat"]["textFormat"]["italic"] for c in pending)
    assert pending[4]["userEnteredFormat"].get("backgroundColor") is None
    assert posted[1]["userEnteredValue"] == {"stringValue": "Discover"}


def test_format_pending_edits() -> None:
    grid: list[list[object]] = [
        list(HEADER),
        [46087, "Discover (pending)", "ODD SHOP", -1, "Groceries", -4],
    ]
    edits = transactions_tab.find_edits(grid, {-4: "Uncategorized"})
    text = transactions_tab.format_pending_edits(edits)
    assert "row 2: ODD SHOP (Uncategorized → Groceries)" in text
    assert "once they post" in text


def test_category_dropdown_lists_every_choice() -> None:
    validation = transactions_tab.requests(7, 42, tab_rows(), RULES)[2]["setDataValidation"]
    values = [v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]]
    assert values == [
        "Groceries",
        "Income",
        "Card payment",
        "Savings transfer",
        "Investing",
        "Uncategorized",
    ]
    assert validation["rule"]["strict"] is True
    assert (validation["range"]["startColumnIndex"], validation["range"]["endRowIndex"]) == (4, 3)


def test_id_column_is_hidden() -> None:
    requests = transactions_tab.requests(7, 42, tab_rows(), RULES)
    columns = [r["updateDimensionProperties"] for r in requests if "updateDimensionProperties" in r]
    hidden = [c["range"]["startIndex"] for c in columns if c["properties"].get("hiddenByUser")]
    assert hidden == [5]


def test_empty_tab_has_no_dropdown() -> None:
    requests = transactions_tab.requests(None, 42, [], RULES)
    assert "setDataValidation" not in kinds(requests)
    assert requests[0]["addSheet"]["properties"]["gridProperties"]["rowCount"] == 2
