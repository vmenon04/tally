"""Reading and writing the Google Sheet through the Sheets API, as the service account.

A client is created read-only unless writing is asked for, so a dry run cannot change the
Sheet even by mistake. Changes are sent as one batchUpdate, which Google applies all or
nothing.
"""

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

READ_ONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
READ_WRITE = "https://www.googleapis.com/auth/spreadsheets"
_API = "https://sheets.googleapis.com/v4/spreadsheets"

type Grid = list[list[object]]
type Request = dict[str, Any]


class SheetError(Exception):
    """The Sheets API refused or failed a request."""


class Sheet(Protocol):
    def tabs(self) -> dict[str, int]:
        """Each tab's title and its sheetId (the gid in the tab's URL)."""
        ...

    def values(self, cell_range: str) -> Grid:
        """Cells as typed: numbers as numbers, formulas as "=...", empty cells as ""."""
        ...

    def batch_update(self, requests: list[Request]) -> None:
        """Apply the requests in one batchUpdate: all of them or none."""
        ...


class GoogleSheet:
    def __init__(self, sheet_id: str, key_file: Path, *, write: bool = False) -> None:
        try:
            credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(key_file), scopes=[READ_WRITE if write else READ_ONLY]
            )
        except FileNotFoundError:
            raise SheetError(f"no service account key at {key_file}") from None
        self._sheet_id = sheet_id
        self._session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]

    def tabs(self) -> dict[str, int]:
        response = self._session.get(
            f"{_API}/{self._sheet_id}",
            params={"fields": "sheets.properties(sheetId,title)"},
            timeout=30,
        )
        _check(response, "reading the list of tabs")
        return {
            s["properties"]["title"]: s["properties"]["sheetId"] for s in response.json()["sheets"]
        }

    def values(self, cell_range: str) -> Grid:
        response = self._session.get(
            f"{_API}/{self._sheet_id}/values/{quote(cell_range)}",
            params={"valueRenderOption": "FORMULA"},
            timeout=30,
        )
        _check(response, f"reading {cell_range}")
        rows: Grid = response.json().get("values", [])
        return rows

    def batch_update(self, requests: list[Request]) -> None:
        response = self._session.post(
            f"{_API}/{self._sheet_id}:batchUpdate", json={"requests": requests}, timeout=60
        )
        _check(response, f"applying {len(requests)} changes")


def number_cell(tab_id: int, cell: str, value: Decimal) -> Request:
    """A request that sets one cell ("C8") of a tab to a number."""
    match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
    if not match:
        raise ValueError(f"not a cell address: {cell!r}")
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(match[2])
    return {
        "updateCells": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": column - 1,
                "endColumnIndex": column,
            },
            "rows": [{"values": [{"userEnteredValue": {"numberValue": float(value)}}]}],
            "fields": "userEnteredValue",
        }
    }


def _check(response: Any, action: str) -> None:
    if response.ok:
        return
    try:
        status = response.json()["error"]["status"]
    except (ValueError, KeyError, TypeError):
        status = response.reason
    raise SheetError(f"{action}: HTTP {response.status_code} {status}")
