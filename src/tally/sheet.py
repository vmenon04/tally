"""Reading and writing the Google Sheet through the Sheets API, as the service account.

A client is created read-only unless writing is asked for, so a dry run cannot change the
Sheet even by mistake.
"""

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


class SheetError(Exception):
    """The Sheets API refused or failed a request."""


class Sheet(Protocol):
    def values(self, cell_range: str) -> Grid:
        """Cells as typed: numbers as numbers, formulas as "=...", empty cells as ""."""
        ...

    def update(self, cells: dict[str, Decimal]) -> None:
        """Set each cell ("Budget!C8") to a number, all in one request."""
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

    def values(self, cell_range: str) -> Grid:
        response = self._session.get(
            f"{_API}/{self._sheet_id}/values/{quote(cell_range)}",
            params={"valueRenderOption": "FORMULA"},
            timeout=30,
        )
        _check(response, f"reading {cell_range}")
        rows: Grid = response.json().get("values", [])
        return rows

    def update(self, cells: dict[str, Decimal]) -> None:
        response = self._session.post(
            f"{_API}/{self._sheet_id}/values:batchUpdate",
            json=update_body(cells),
            timeout=30,
        )
        _check(response, f"writing {len(cells)} cells")


def update_body(cells: dict[str, Decimal]) -> dict[str, Any]:
    """The values:batchUpdate request that sets each cell to a number."""
    return {
        "valueInputOption": "RAW",
        "data": [{"range": cell, "values": [[float(value)]]} for cell, value in cells.items()],
    }


def _check(response: Any, action: str) -> None:
    if response.ok:
        return
    try:
        status = response.json()["error"]["status"]
    except (ValueError, KeyError, TypeError):
        status = response.reason
    raise SheetError(f"{action}: HTTP {response.status_code} {status}")
