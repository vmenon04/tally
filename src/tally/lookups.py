"""Sure's lookups for the chat: an MCP server that passes on only tools that read.

Sure offers its data to agents at /mcp, but the same endpoint has tools that create,
change and delete. So Claude never gets Sure's token: for each reply it starts `tally
lookups` (an MCP server on stdin and stdout), which forwards only the tools in TOOLS and
refuses the rest, so a merchant name posing as an instruction can't change anything.

Every result is also added to a record file, so the chat's check on amounts can accept
the amounts Claude looked up (see chat). Holdings and the balance sheet are left out:
tally doesn't send Fidelity to Sure, so the ledger's facts are the record for those.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import requests

TOOLS = frozenset(
    {
        "get_accounts",
        "get_categories",
        "get_income_statement",
        "get_merchants",
        "get_recurring_transactions",
        "get_transactions",
    }
)
PROTOCOL = "2025-06-18"
TIMEOUT = 60  # seconds for one request to Sure

# Sends one JSON-RPC request to Sure and returns its result; raises LookupFailed.
Call = Callable[[str, dict[str, Any]], Any]


class LookupFailed(RuntimeError):
    """Sure couldn't be asked, or turned the request down."""


def sure(url: str, token: str) -> Call:
    """Requests to Sure's /mcp at url, with the token Sure was given as MCP_API_TOKEN."""

    def call(method: str, params: dict[str, Any]) -> Any:
        try:
            response = requests.post(
                f"{url.rstrip('/')}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Authorization": f"Bearer {token}", "Mcp-Protocol-Version": PROTOCOL},
                timeout=TIMEOUT,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as e:
            raise LookupFailed(f"couldn't reach Sure: {e}") from None
        if "error" in body or response.status_code != 200:
            message = (body.get("error") or {}).get("message", response.status_code)
            raise LookupFailed(f"Sure refused: {message}")
        return body.get("result")

    return call


def serve(call: Call, record: Path, stdin: TextIO, stdout: TextIO) -> None:
    """Answer MCP requests, one JSON message per line, until stdin closes."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _send(stdout, {"jsonrpc": "2.0", "id": None, "error": _error(-32700, "parse error")})
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # a notification: nothing to answer
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = _handle(call, record, message.get("method"), message.get("params"))
        except _Refused as e:
            reply["error"] = _error(e.code, str(e))
        _send(stdout, reply)


class _Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _handle(call: Call, record: Path, method: Any, params: Any) -> Any:
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sure-lookups", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        try:
            tools = call("tools/list", {}).get("tools", [])
        except (LookupFailed, AttributeError) as e:
            raise _Refused(-32603, str(e)) from None
        return {"tools": [t for t in tools if t.get("name") in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _failed(f"{name} isn't a lookup; only these are: {', '.join(sorted(TOOLS))}")
        try:
            result = call("tools/call", {"name": name, "arguments": params.get("arguments") or {}})
        except LookupFailed as e:
            return _failed(str(e))
        with record.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        return result
    raise _Refused(-32601, f"method not found: {method}")


def _failed(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _error(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _send(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message) + "\n")
    stdout.flush()
