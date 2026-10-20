"""Reading the Google Sheet through the Sheets API, as the service account.

The read-only scope means this client cannot change the Sheet even by mistake.
"""

from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

READ_ONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
_API = "https://sheets.googleapis.com/v4/spreadsheets"

type Grid = list[list[object]]


class SheetError(Exception):
    """The Sheets API refused or failed a request."""


class Sheet(Protocol):
    def values(self, cell_range: str) -> Grid:
        """Cells as typed: numbers as numbers, formulas as "=...", empty cells as ""."""
        ...


class GoogleSheet:
    def __init__(self, sheet_id: str, key_file: Path) -> None:
        try:
            credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(key_file), scopes=[READ_ONLY]
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
        if not response.ok:
            try:
                status = response.json()["error"]["status"]
            except (ValueError, KeyError, TypeError):
                status = response.reason
            raise SheetError(f"reading {cell_range}: HTTP {response.status_code} {status}")
        rows: Grid = response.json().get("values", [])
        return rows
