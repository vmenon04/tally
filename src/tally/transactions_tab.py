"""The Transactions tab: every transaction, newest first, with its category.

tally owns this tab and rewrites it on every sync. The Category column is where you correct
categories, so before rewriting, tally compares it with what it last wrote and stops if you
changed anything, rather than erase your edits.

Card alerts not yet in a bank export are listed too, in grey italics with "(pending)" after
the account, under the negative of their alert IDs (see pending.py). They're corrected like any
other row; once the purchase posts, the correction moves to the posted row (see corrections.py).
"""

import datetime
from dataclasses import dataclass
from typing import Any

from tally.pending import is_pending
from tally.rules import NOT_SPENDING, UNCATEGORIZED, Rules
from tally.sheet import Grid, Request
from tally.transaction import Transaction

TAB = "Transactions"
RANGE = f"{TAB}!A1:F"
HEADER = ["Date", "Account", "Merchant", "Amount", "Category", "ID"]
ACCOUNT_NAMES = {"discover": "Discover", "bofa_checking": "BofA checking"}

_CATEGORY_COLUMN = 4
_ID_COLUMN = 5
_WIDTHS = [95, 115, 380, 95, 200, 60]
_UNCATEGORIZED_BACKGROUND = {"red": 1.0, "green": 0.95, "blue": 0.8}
_PENDING_TEXT = {"italic": True, "foregroundColor": {"red": 0.45, "green": 0.45, "blue": 0.45}}
_SHEETS_EPOCH = datetime.date(1899, 12, 30)


class TransactionsTabError(ValueError):
    """The tab isn't what tally wrote there, so rewriting it could lose something."""


@dataclass(frozen=True, slots=True)
class Row:
    id: int  # negative for a pending alert
    transaction: Transaction
    category: str

    @property
    def pending(self) -> bool:
        return is_pending(self.id)


@dataclass(frozen=True, slots=True)
class Edit:
    row_number: int  # as shown in the Sheet, header is row 1
    id: object
    merchant: object
    was: str | None  # what tally wrote; None if tally didn't write this row
    now: object


def rows(stored: list[tuple[int, Transaction]], rules: Rules) -> list[Row]:
    """The tab's rows, newest first."""
    ordered = sorted(stored, key=lambda pair: (pair[1].date, pair[0]), reverse=True)
    return [Row(tid, t, rules.categorize(t)) for tid, t in ordered]


def find_edits(grid: Grid, written: dict[int, str]) -> list[Edit]:
    """Rows whose category differs from what tally wrote, or that tally didn't write at all."""
    if not grid:
        return []
    if list(grid[0][: len(HEADER)]) != HEADER:
        raise TransactionsTabError(f"the header row isn't {HEADER}; was the tab rearranged?")
    edits = []
    for number, row in enumerate(grid[1:], start=2):
        cells = [*row, *[""] * (len(HEADER) - len(row))]
        if not any(cell != "" for cell in cells):
            continue
        tid, category = cells[_ID_COLUMN], cells[_CATEGORY_COLUMN]
        was = written.get(tid) if isinstance(tid, int) else None
        if was is None or category != was:
            edits.append(Edit(number, tid, cells[2], was, category))
    return edits


def format_edits(edits: list[Edit]) -> str:
    """Describe rows tally can't turn into corrections: added by hand, or unknown IDs."""
    lines = [f"Transactions tab: {len(edits)} row(s) tally didn't write:"]
    lines += [f"  row {e.row_number}: {e.merchant!s}" for e in edits]
    lines.append(
        "tally won't rewrite the tab and lose them. Delete those rows (tally only reads "
        "category changes) and sync again."
    )
    return "\n".join(lines)


def requests(
    tab_id: int | None, new_tab_id: int, tab_rows: list[Row], rules: Rules
) -> list[Request]:
    """Requests that (create and) rewrite the whole tab. tab_id is None if it doesn't exist."""
    sheet_id = new_tab_id if tab_id is None else tab_id
    grid_properties = {
        "rowCount": max(len(tab_rows) + 1, 2),
        "columnCount": len(HEADER),
        "frozenRowCount": 1,
    }
    result: list[Request] = []
    if tab_id is None:
        result.append(
            {
                "addSheet": {
                    "properties": {
                        "sheetId": sheet_id,
                        "title": TAB,
                        "gridProperties": grid_properties,
                    }
                }
            }
        )
    else:
        result.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": grid_properties},
                    "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
                }
            }
        )
    header = [
        {
            "userEnteredValue": {"stringValue": h},
            "userEnteredFormat": {"textFormat": {"bold": True}},
        }
        for h in HEADER
    ]
    result.append(
        {
            "updateCells": {
                "range": _range(sheet_id, 0, len(tab_rows) + 1, 0, len(HEADER)),
                "rows": [{"values": header}] + [{"values": _cells(row)} for row in tab_rows],
                "fields": "userEnteredValue,userEnteredFormat",
            }
        }
    )
    if tab_rows:
        choices = [*rules.categories, *NOT_SPENDING, UNCATEGORIZED]
        result.append(
            {
                "setDataValidation": {
                    "range": _range(
                        sheet_id, 1, len(tab_rows) + 1, _CATEGORY_COLUMN, _CATEGORY_COLUMN + 1
                    ),
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": c} for c in choices],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
        )
    for column, width in enumerate(_WIDTHS):
        properties: dict[str, Any] = {"pixelSize": width}
        fields = "pixelSize"
        if column == _ID_COLUMN:
            properties["hiddenByUser"] = True
            fields += ",hiddenByUser"
        result.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": column,
                        "endIndex": column + 1,
                    },
                    "properties": properties,
                    "fields": fields,
                }
            }
        )
    return result


def _cells(row: Row) -> list[dict[str, Any]]:
    t = row.transaction
    style: dict[str, Any] = {}
    if row.category == UNCATEGORIZED:
        style["backgroundColor"] = _UNCATEGORIZED_BACKGROUND
    account = ACCOUNT_NAMES.get(t.account, t.account)
    if row.pending:
        style["textFormat"] = _PENDING_TEXT
        account += " (pending)"
    return [
        {
            "userEnteredValue": {"numberValue": (t.date - _SHEETS_EPOCH).days},
            "userEnteredFormat": {
                "numberFormat": {"type": "DATE", "pattern": "mm/dd/yyyy"},
                **style,
            },
        },
        {
            "userEnteredValue": {"stringValue": account},
            "userEnteredFormat": style,
        },
        {"userEnteredValue": {"stringValue": t.merchant}, "userEnteredFormat": style},
        {
            "userEnteredValue": {"numberValue": float(t.amount)},
            "userEnteredFormat": {
                "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00;-$#,##0.00"},
                **style,
            },
        },
        {"userEnteredValue": {"stringValue": row.category}, "userEnteredFormat": style},
        {"userEnteredValue": {"numberValue": row.id}, "userEnteredFormat": style},
    ]


def _range(
    sheet_id: int, row_start: int, row_end: int, column_start: int, column_end: int
) -> dict[str, int]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": row_start,
        "endRowIndex": row_end,
        "startColumnIndex": column_start,
        "endColumnIndex": column_end,
    }
