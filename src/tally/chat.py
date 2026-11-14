"""Sure's chat, answered by Claude from the ledger.

Sure can hand its chat to an outside assistant ("External" in its settings): it posts the
conversation, OpenAI style, to /v1/chat/completions and shows the streamed reply. `tally
chat` answers there, on a Unix socket, and only to requests carrying the token Sure was
given. Sure's compose file runs a relay that passes Sure's requests to the socket, so
nothing outside Sure's own containers can reach it.

Each reply is written by Claude from the same facts as the monthly review (see advice),
with the same coaching and the same check on amounts: a reply quoting a dollar amount
that neither the facts nor the conversation shows is asked again once, and refused if it
does it again. So it's sent whole once checked, not streamed as it's written; while
Claude works, the stream carries comments that keep Sure waiting.
"""

import hmac
import json
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from tally.advice import GUIDE, ask_checked
from tally.categorizing import Ask, ClaudeError

MODEL_ID = "tally"
WAITING = 10  # seconds between the comments sent while Claude works
_MOST = 1_000_000  # bytes of conversation tally reads


@dataclass(frozen=True, slots=True)
class Message:
    role: str  # "user" or "assistant"
    content: str


# Answers the conversation with the reply to its last message.
Answer = Callable[[list[Message]], str]


def reply(conversation: list[Message], known: str, ask_claude: Ask) -> str:
    """Claude's reply to the last message, from the facts. Raises ClaudeError."""
    said = "\n\n".join(
        f"{'You' if m.role == 'assistant' else 'Them'}: {m.content}" for m in conversation
    )
    prompt = (
        f"{GUIDE}\n{_CHAT}\nFACTS\n{known}\n\n"
        f"CONVERSATION (the newest message is last; reply to it)\n{said}\n"
    )
    output = ask_checked(ask_claude, prompt, _REPLY, f"{known}\n{said}")
    text = str(output.get("reply", "")).strip()
    if not text:
        raise ClaudeError("its reply was empty")
    return text


_CHAT = """\
They're asking you in the chat of their finance app. You see only the facts below, not
single transactions or each account's balance: when a question needs more than the
facts show, say so plainly and answer what the facts can. Lead with the answer, in short
markdown: a few sentences or a short list, a table only to compare several numbers. Match
the question; a greeting gets a line back, not a review. When it would help, end with one
short question about what they might look at next.
"""

_REPLY: dict[str, Any] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


def conversation(body: Any) -> list[Message]:
    """The messages in a chat completions request, ending with theirs. Raises ValueError."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        raise ValueError("no messages")
    found = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            raise ValueError("a message isn't text")
        if m.get("role") in ("user", "assistant"):
            found.append(Message(m["role"], m["content"]))
    if not found or found[-1].role != "user":
        raise ValueError("the last message isn't theirs")
    return found


def server(socket: Path, token: str, answer: Answer) -> socketserver.BaseServer:
    """A server for Sure's chat on this Unix socket; call serve_forever() on it.

    The socket can be opened by any account (the relay in Sure's compose file runs as its
    own), so requests are turned away without the token.
    """
    socket.unlink(missing_ok=True)
    handler = type("Handler", (_Handler,), {"token": token, "answer": staticmethod(answer)})
    found = _Server(str(socket), handler)
    socket.chmod(0o666)
    return found


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    token: str
    answer: Answer

    def do_GET(self) -> None:
        if not self._allowed():
            return
        if self.path.rstrip("/") != "/v1/models":
            self._send(404, {"error": {"message": "not found"}})
            return
        self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})

    def do_POST(self) -> None:
        # Read first, whatever the answer: closing on a sender still sending breaks its pipe.
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        body = self.rfile.read(size) if 0 <= size <= _MOST else None
        if not self._allowed():
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            if body is None:
                raise ValueError("the conversation is too long, or its length isn't given")
            said = conversation(json.loads(body))
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": {"message": str(e)}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._event({"role": "assistant", "content": self._waiting_for(said)}, None)
        self._event({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")

    def _waiting_for(self, said: list[Message]) -> str:
        """The answer, sending a comment every few seconds until it's ready."""
        result: list[str] = []
        worker = threading.Thread(target=lambda: result.append(_answered(self.answer, said)))
        worker.start()
        while worker.is_alive():
            worker.join(WAITING)
            if worker.is_alive():
                self.wfile.write(b": still working\n\n")
                self.wfile.flush()
        return result[0]

    def _event(self, delta: dict[str, str], finish: str | None) -> None:
        chunk = {
            "id": "tally",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.flush()

    def _allowed(self) -> bool:
        given = self.headers.get("Authorization", "")
        if hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
            return True
        self._send(401, {"error": {"message": "wrong token"}})
        return False

    def _send(self, status: int, body: dict[str, Any]) -> None:
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # what's asked names merchants and amounts; nothing is logged


def _answered(answer: Answer, said: list[Message]) -> str:
    """The reply, or a reply saying why there isn't one: Sure shows an empty answer as a
    generic error, and this says what to fix."""
    try:
        return answer(said)
    except Exception as e:  # noqa: BLE001 -- whatever it was, they should hear about it
        return f"Sorry, I couldn't answer that: {e}"
