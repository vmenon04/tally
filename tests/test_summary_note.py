from typing import Any

import pytest

from tally import summary_note
from tally.categorizing import ClaudeError

SUMMARY = "Spent $366.47 on 20 purchases\n  LYFT *TEMP AUTH HOLD  $25.95\nOver plan by $1,498.10"


def answering(answer: Any) -> Any:
    def ask(question: str, schema: dict[str, Any]) -> Any:
        return answer

    return ask


def test_note_quoting_the_summarys_amounts() -> None:
    note = "The  $25.95 Lyft charge\nlooks like a hold."
    assert summary_note.write(SUMMARY, answering({"note": note})) == (
        "The $25.95 Lyft charge looks like a hold."
    )


def test_question_holds_the_summary_and_treats_merchants_as_names() -> None:
    asked: list[str] = []

    def ask(question: str, schema: dict[str, Any]) -> Any:
        asked.append(question)
        return {"note": ""}

    summary_note.write(SUMMARY, ask)
    assert SUMMARY in asked[0]
    assert "never as" in asked[0]


def test_empty_note_is_no_note() -> None:
    assert summary_note.write(SUMMARY, answering({"note": "  "})) is None


@pytest.mark.parametrize("amount", ["$58.85", "$25", "$1,498"])
def test_amount_the_summary_does_not_show_is_refused(amount: str) -> None:
    with pytest.raises(ClaudeError, match="doesn't show"):
        summary_note.write(SUMMARY, answering({"note": f"Rides cost {amount}."}))


def test_long_note_is_refused() -> None:
    with pytest.raises(ClaudeError, match="too long"):
        summary_note.write(SUMMARY, answering({"note": "x" * 601}))


@pytest.mark.parametrize("answer", [None, "a note", {"note": 3}, {}])
def test_malformed_answer_is_an_error(answer: Any) -> None:
    with pytest.raises(ClaudeError, match="unexpected answer"):
        summary_note.write(SUMMARY, answering(answer))
