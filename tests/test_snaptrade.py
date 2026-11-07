import base64
import hashlib
import hmac
from typing import Any

import pytest

from tally import snaptrade


def test_signature_follows_the_documented_recipe() -> None:
    # The canonical JSON from SnapTrade's docs: sorted keys, no spaces, null content.
    payload = b'{"content":null,"path":"/accounts/all","query":"clientId=ABC&timestamp=1715123456"}'
    expected = base64.b64encode(hmac.new(b"made-up-key", payload, hashlib.sha256).digest()).decode()
    got = snaptrade.signature("made-up-key", "/accounts/all", "clientId=ABC&timestamp=1715123456")
    assert got == expected


def test_signature_signs_a_body_with_sorted_keys() -> None:
    payload = b'{"content":{"a":1,"b":2},"path":"/x","query":"q=1"}'
    expected = base64.b64encode(hmac.new(b"k", payload, hashlib.sha256).digest()).decode()
    assert snaptrade.signature("k", "/x", "q=1", {"b": 2, "a": 1}) == expected


def fake_get(path: str, params: dict[str, str]) -> Any:
    if path == "/accounts/all":
        return {
            "results": [
                {
                    "id": "a1",
                    "masked_account_number": "Z00001111",
                    "display_name": "ROTH IRA",
                    "kind": "investment",
                    "raw_type": "Roth IRA",
                    "status": "open",
                }
            ]
        }
    if path == "/accounts/a1/positions/all":
        return {"results": [{"units": 10.5, "price": 200.0, "average_purchase_price": 190.48}]}
    if path == "/accounts/a1/activities":
        return {
            "data": [
                {"type": "BUY", "trade_date": "2026-03-02T00:00:00Z", "amount": -2000.0},
                {"type": "DIVIDEND", "trade_date": "2026-03-20T00:00:00Z", "amount": 2.0},
            ]
        }
    raise AssertionError(path)


def test_overview_names_shapes_not_amounts() -> None:
    lines = snaptrade.overview(fake_get)
    text = "\n".join(lines)
    assert "…1111  ROTH IRA  (Roth IRA, open)" in text
    assert "cost basis fields: average_purchase_price" in text
    assert "activities: 2 (2026-03-02 to 2026-03-20); types: BUY 1, DIVIDEND 1" in text
    assert "2000" not in text and "190.48" not in text and "Z00001111" not in text


def test_missing_keys_are_reported(tmp_path: Any) -> None:
    with pytest.raises(snaptrade.SnapTradeError, match="no SnapTrade key"):
        snaptrade.Client.from_secrets(tmp_path)
