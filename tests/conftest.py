from typing import Any

import pytest

from tally import categorizing


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never ask the real Claude; the ones about it put in their own answers."""

    def unavailable(question: str, schema: dict[str, Any], token: str | None, **_: Any) -> Any:
        raise categorizing.ClaudeError("not asked in tests")

    monkeypatch.setattr(categorizing, "ask_claude", unavailable)
