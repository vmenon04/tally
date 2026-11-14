import http.client
import json
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_advice import Claude

from tally import chat
from tally.categorizing import ClaudeError

KNOWN = "Today is Tuesday, March 10, 2026.\n- Dining: $240, plan $200, usual $180."
TOKEN = "made-up-token"


def said(*texts: str) -> list[chat.Message]:
    return [chat.Message("user" if n % 2 == 0 else "assistant", t) for n, t in enumerate(texts)]


def test_reply_answers_the_last_message_from_the_facts() -> None:
    claude = Claude({"reply": "Dining is at **$240**, past the $200 plan."})
    conversation = said("hi", "Hi! What would you like to look at?", "How's dining?")
    assert chat.reply(conversation, KNOWN, claude) == "Dining is at **$240**, past the $200 plan."
    prompt = claude.prompts[0]
    assert "practical money coach" in prompt
    assert "chat of their finance app" in prompt
    assert KNOWN in prompt
    assert prompt.endswith(
        "Them: hi\n\nYou: Hi! What would you like to look at?\n\nThem: How's dining?\n"
    )


def test_reply_may_quote_amounts_they_said() -> None:
    claude = Claude({"reply": "Putting $150 a month aside works."})
    assert chat.reply(said("Can I save $150 a month?"), KNOWN, claude).startswith("Putting $150")


def test_reply_quoting_a_made_up_amount_is_asked_again_then_refused() -> None:
    claude = Claude({"reply": "You spent $999."}, {"reply": "Still $999."})
    with pytest.raises(ClaudeError, match="999"):
        chat.reply(said("How's dining?"), KNOWN, claude)
    assert "Your last answer quoted $999" in claude.prompts[1]


def test_conversation_keeps_the_chat_and_needs_their_message_last() -> None:
    body = {
        "messages": [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "dining?"},
        ]
    }
    assert chat.conversation(body) == said("hi", "hello", "dining?")
    bad: Any
    for bad in (
        {},
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "x"}]},
        {"messages": [{"role": "user", "content": None}]},
    ):
        with pytest.raises(ValueError):
            chat.conversation(bad)


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost")
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.path))


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, list[list[chat.Message]]]]:
    asked: list[list[chat.Message]] = []

    def answer(conversation: list[chat.Message]) -> str:
        asked.append(conversation)
        if conversation[-1].content == "break":
            raise ClaudeError("no answer within 300 seconds")
        return "Dining is at $240."

    monkeypatch.setattr(chat, "WAITING", 0.01)
    # A short folder: Unix socket paths are limited to about 100 characters.
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        path = Path(folder) / "chat.sock"
        server = chat.server(path, TOKEN, answer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield path, asked
        server.shutdown()
        server.server_close()


def request(
    path: Path, method: str, url: str, body: Any = None, token: str = TOKEN
) -> tuple[int, str]:
    connection = _Unix(path)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    connection.request(method, url, json.dumps(body) if body is not None else None, headers)
    response = connection.getresponse()
    return response.status, response.read().decode()


def streamed(text: str) -> str:
    events = [line.removeprefix("data: ") for line in text.splitlines() if line.startswith("data:")]
    assert events[-1] == "[DONE]"
    return "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1])


def test_server_streams_the_answer_as_sure_reads_it(served: tuple[Path, Any]) -> None:
    path, asked = served
    body = {"model": "tally", "stream": True, "messages": [{"role": "user", "content": "dining?"}]}
    status, text = request(path, "POST", "/v1/chat/completions", body)
    assert status == 200
    assert streamed(text) == "Dining is at $240."
    assert asked == [said("dining?")]
    assert path.stat().st_mode & 0o777 == 0o666


def test_server_says_why_it_couldnt_answer(served: tuple[Path, Any]) -> None:
    path, _ = served
    body = {"messages": [{"role": "user", "content": "break"}]}
    _, text = request(path, "POST", "/v1/chat/completions", body)
    assert streamed(text) == "Sorry, I couldn't answer that: no answer within 300 seconds"


def test_server_turns_away_requests_without_the_token(served: tuple[Path, Any]) -> None:
    path, asked = served
    body = {"messages": [{"role": "user", "content": "dining?"}]}
    assert request(path, "POST", "/v1/chat/completions", body, token="wrong")[0] == 401
    assert request(path, "GET", "/v1/models", token="")[0] == 401
    assert asked == []


def test_server_lists_its_one_model_and_refuses_bad_requests(served: tuple[Path, Any]) -> None:
    path, _ = served
    status, text = request(path, "GET", "/v1/models")
    assert (status, json.loads(text)["data"][0]["id"]) == (200, "tally")
    assert request(path, "POST", "/v1/chat/completions", {"messages": "hi"})[0] == 400
    assert request(path, "POST", "/v1/other", {})[0] == 404
