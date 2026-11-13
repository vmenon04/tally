"""Sure, the web and phone UI tally fills from the ledger (github.com/we-promise/sure).

tally talks to Sure's REST API on the same machine, with an API key made in Sure's settings
and saved by `tally sure-login` as secrets/sure-api-key. The ledger stays the record: Sure
holds a copy.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

API_KEY_FILE = "sure-api-key"
_PAGE_SIZE = 100  # the most Sure returns per page


class SureError(RuntimeError):
    """Sure refused a request, or couldn't be reached."""


@dataclass(frozen=True, slots=True)
class Client:
    url: str  # where Sure listens, like http://127.0.0.1:3200
    api_key: str
    timeout: int = 30

    @classmethod
    def from_secrets(cls, url: str, folder: Path) -> "Client":
        try:
            key = (folder / API_KEY_FILE).read_text(encoding="utf-8").strip()
        except FileNotFoundError as e:
            raise SureError(f"no Sure API key at {e.filename}; run `tally sure-login`") from None
        return cls(url, key)

    def request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            response = requests.request(
                method,
                f"{self.url}{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise SureError(f"couldn't reach Sure at {self.url}: {type(e).__name__}") from e
        if response.status_code == 401:
            raise SureError("Sure didn't accept the API key")
        if not response.ok:
            detail = response.text[:200].replace("\n", " ")
            raise SureError(f"Sure refused {method} {path}: HTTP {response.status_code} {detail}")
        return response.json() if response.content else None

    def each(self, path: str, key: str, params: dict[str, str] | None = None) -> Iterator[Any]:
        """Every item of a paginated list; `key` names the list in each page's answer."""
        page = 1
        while True:
            query = {**(params or {}), "page": str(page), "per_page": str(_PAGE_SIZE)}
            answer = self.request("GET", path, params=query)
            try:
                yield from answer[key]
                last = answer["pagination"]["total_pages"]
            except (KeyError, TypeError):
                raise SureError(f"unexpected answer from Sure's {path}") from None
            if page >= last:
                return
            page += 1


def account_names(client: Client) -> list[str]:
    """The accounts Sure has, by name."""
    return [str(account["name"]) for account in client.each("/api/v1/accounts", "accounts")]
