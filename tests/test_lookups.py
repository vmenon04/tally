import io
import json
from pathlib import Path
from typing import Any

import pytest

from tally import lookups

SURE_TOOLS = [
    {"name": "get_transactions", "description": "Search transactions.", "inputSchema": {}},
    {"name": "delete_transaction", "description": "Delete one.", "inputSchema": {}},
    {"name": "get_accounts", "description": "Accounts and balances.", "inputSchema": {}},
]
FOUND = {"content": [{"type": "text", "text": '{"transactions": [{"amount": "-38.17"}]}'}]}


class FakeSure:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "tools/list":
            return {"tools": SURE_TOOLS}
        if params.get("arguments", {}).get("search") == "down":
            raise lookups.LookupFailed("couldn't reach Sure: refused")
        return FOUND


def run(sure: FakeSure, record: Path, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    out = io.StringIO()
    lookups.serve(sure, record, io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(n: int, name: str, **arguments: Any) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments}
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": params}


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "looked-up"


def test_offers_only_the_tools_that_read(record: Path) -> None:
    sure = FakeSure()
    replies = run(
        sure,
        record,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert [r["id"] for r in replies] == [1, 2]  # the notification gets no answer
    assert replies[0]["result"]["protocolVersion"] == "x"
    names = [t["name"] for t in replies[1]["result"]["tools"]]
    assert names == ["get_transactions", "get_accounts"]


def test_a_lookup_is_passed_on_and_recorded(record: Path) -> None:
    sure = FakeSure()
    replies = run(sure, record, call(1, "get_transactions", search="TRADER"))
    assert replies[0]["result"] == FOUND
    assert sure.calls == [
        ("tools/call", {"name": "get_transactions", "arguments": {"search": "TRADER"}})
    ]
    assert "-38.17" in record.read_text()


def test_a_tool_that_changes_sure_is_refused_without_asking_sure(record: Path) -> None:
    sure = FakeSure()
    replies = run(sure, record, call(1, "delete_transaction", id="abc"))
    assert replies[0]["result"]["isError"] is True
    assert "isn't a lookup" in replies[0]["result"]["content"][0]["text"]
    assert sure.calls == []
    assert not record.exists()


def test_failures_are_reported_to_claude(record: Path) -> None:
    replies = run(
        FakeSure(),
        record,
        call(1, "get_transactions", search="down"),
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
    )
    assert replies[0]["result"]["isError"] is True
    assert "couldn't reach Sure" in replies[0]["result"]["content"][0]["text"]
    assert replies[1]["error"]["code"] == -32601
