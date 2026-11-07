"""SnapTrade: read-only access to brokerage accounts (Fidelity), with a Personal API key.

Every request carries clientId and a timestamp in its query, and a Signature header: an
HMAC-SHA256, keyed by the consumer key, of a canonical JSON of the request's content, path
and query string, base64-encoded (docs.snaptrade.com/docs/request-signatures). The keys are
in secrets/snaptrade-client-id and secrets/snaptrade-consumer-key; only the client ID, which
is no use without the consumer key, ever appears in a URL.
"""

import base64
import hashlib
import hmac
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

BASE = "https://api.snaptrade.com"
CLIENT_ID_FILE = "snaptrade-client-id"
CONSUMER_KEY_FILE = "snaptrade-consumer-key"

type Get = Callable[[str, dict[str, str]], Any]


class SnapTradeError(RuntimeError):
    """SnapTrade refused a request, or couldn't be reached."""


def signature(consumer_key: str, path: str, query: str, content: object = None) -> str:
    """The Signature header for a request with this path, raw query string and JSON body."""
    payload = json.dumps(
        {"content": content or None, "path": path, "query": query},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hmac.new(consumer_key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    consumer_key: str
    timeout: int = 30

    @classmethod
    def from_secrets(cls, folder: Path) -> "Client":
        try:
            client_id = (folder / CLIENT_ID_FILE).read_text(encoding="utf-8").strip()
            key = (folder / CONSUMER_KEY_FILE).read_text(encoding="utf-8").strip()
        except FileNotFoundError as e:
            raise SnapTradeError(f"no SnapTrade key at {e.filename}") from None
        return cls(client_id, key)

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urlencode(
            {**(params or {}), "clientId": self.client_id, "timestamp": str(int(time.time()))}
        )
        headers = {
            "Signature": signature(self.consumer_key, path, query),
            "Accept": "application/json",
        }
        try:
            response = requests.get(f"{BASE}{path}?{query}", headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise SnapTradeError(f"couldn't reach SnapTrade: {type(e).__name__}") from e
        if not response.ok:
            detail = response.text[:200].replace("\n", " ")
            raise SnapTradeError(f"SnapTrade refused {path}: HTTP {response.status_code} {detail}")
        return response.json()


def overview(get: Get) -> list[str]:
    """What SnapTrade has, as lines that name no amounts: accounts, positions, activities."""
    found = get("/accounts/all", {})
    accounts = found.get("results", found) if isinstance(found, dict) else found
    if not isinstance(accounts, list):
        raise SnapTradeError(f"unexpected accounts answer: {str(found)[:120]}")
    lines = [f"{len(accounts)} account(s):"]
    for account in accounts:
        if not isinstance(account, dict):
            continue
        number = str(account.get("masked_account_number") or "")
        lines.append(
            f"- …{number[-4:]}  {account.get('display_name')}  "
            f"({account.get('raw_type') or account.get('kind')}, {account.get('status')})"
        )
        account_id = str(account.get("id"))
        positions = get(f"/accounts/{account_id}/positions/all", {})
        results = positions.get("results", positions) if isinstance(positions, dict) else positions
        results = results if isinstance(results, list) else []
        fields = Counter(k for p in results if isinstance(p, dict) for k in p)
        cost = [k for k in ("average_purchase_price", "tax_lots", "cost_basis") if fields[k]]
        lines.append(
            f"    positions: {len(results)}; cost basis fields: {', '.join(cost) or 'none'}; "
            f"fields: {', '.join(sorted(fields))}"
        )
        activities = get(f"/accounts/{account_id}/activities", {})
        items = activities.get("data", activities) if isinstance(activities, dict) else activities
        items = items if isinstance(items, list) else []
        kinds = Counter(str(a.get("type")) for a in items if isinstance(a, dict))
        dates = sorted(
            str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
            for a in items
            if isinstance(a, dict)
        )
        span = f"{dates[0]} to {dates[-1]}" if dates else "none"
        lines.append(
            f"    activities: {len(items)} ({span}); types: "
            + (", ".join(f"{k} {n}" for k, n in kinds.most_common()) or "none")
        )
    return lines
