import datetime
import json
from pathlib import Path
from typing import Any

import pytest
from test_picture import GOALS, LEDGER, build_with

from tally import advice
from tally.categorizing import ClaudeError

NOW = datetime.datetime(2026, 3, 10, 12, tzinfo=datetime.UTC)
ABOUT = ["first job; wants to travel more"]


class Claude:
    """Answers in turn; records each prompt."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, Any]) -> Any:
        self.prompts.append(prompt)
        found = self.answers.pop(0)
        if isinstance(found, Exception):
            raise found
        return found


REVIEW = {
    "headline": "A steady month.",
    "went_well": ["Rent on time."],
    "watch": [],
    "try_next": [],
}


def test_facts_spell_out_the_page() -> None:
    text = advice.facts(build_with(GOALS), ABOUT)
    assert "- first job; wants to travel more" in text
    assert "February 2026: pay $2,500, take-home $2,000, spent $1,300" in text
    assert "- Groceries: $50, plan $300, usual $300." in text
    assert "Emergency fund: $200 of a $3,900 target, $3,700 to go; suggested $420" in text


def test_a_question_is_answered_once(tmp_path: Path) -> None:
    advice.ask(tmp_path, "  Can I afford a $700 trip?  ", NOW)
    claude = Claude({"answer": "Yes: of the $600 left each month, put aside..."}, REVIEW)
    done = advice.run(tmp_path, build_with(GOALS), ABOUT, claude, NOW)
    assert len(done.answered) == 1 and done.reviewed == "2026-02"
    [q] = advice.questions(tmp_path)
    assert q.text == "Can I afford a $700 trip?" and q.answer and q.answer.startswith("Yes")
    assert "THEIR QUESTION\nCan I afford a $700 trip?" in claude.prompts[0]
    # Nothing left to do on the next run.
    assert advice.run(tmp_path, build_with(GOALS), ABOUT, Claude(), NOW) == advice.Done([], None)
    assert advice.reviewed(tmp_path, "2026-02")
    review = advice.latest_review(tmp_path)
    assert review is not None and review.title == "February 2026"


def test_an_amount_it_made_up_is_asked_again_then_refused(tmp_path: Path) -> None:
    advice.ask(tmp_path, "How much should I save?", NOW)
    claude = Claude(
        {"answer": "Save $612 a month."},  # not in the facts
        {"answer": "Save $600 a month."},
        REVIEW,
    )
    advice.run(tmp_path, build_with(GOALS), ABOUT, claude, NOW)
    assert "Your last answer quoted $612" in claude.prompts[1]
    assert advice.questions(tmp_path)[0].answer == "Save $600 a month."

    advice.ask(tmp_path, "And next year?", NOW + datetime.timedelta(seconds=1))
    advice.run(tmp_path, build_with(GOALS), ABOUT, Claude({"answer": "$1"}, {"answer": "$2"}), NOW)
    q = advice.questions(tmp_path)[0]
    assert q.answer is None and q.error and "$2" in q.error
    # Not tried again for a while, then it is.
    assert advice.run(tmp_path, build_with(GOALS), ABOUT, Claude(), NOW).answered == []
    later = NOW + advice.RETRY_AFTER
    claude = Claude({"answer": "Keep going."})
    assert len(advice.run(tmp_path, build_with(GOALS), ABOUT, claude, later).answered) == 1


def test_a_failed_review_is_tried_again_later(tmp_path: Path) -> None:
    claude = Claude(ClaudeError("no answer"))
    assert advice.run(tmp_path, build_with(GOALS), ABOUT, claude, NOW).reviewed is None
    saved = json.loads((tmp_path / "advice" / "review-2026-02.json").read_text())
    assert saved["error"] == "no answer" and not advice.reviewed(tmp_path, "2026-02")
    later = NOW + advice.RETRY_AFTER
    assert advice.run(tmp_path, build_with(GOALS), ABOUT, Claude(REVIEW), later).reviewed


def test_questions_must_be_short_and_say_something(tmp_path: Path) -> None:
    with pytest.raises(advice.AdviceError):
        advice.ask(tmp_path, "   ", NOW)
    with pytest.raises(advice.AdviceError):
        advice.ask(tmp_path, "x" * 601, NOW)


def test_the_page_takes_a_question(tmp_path: Path) -> None:
    from fava.application import create_app

    folder = tmp_path / "ledger"
    folder.mkdir()
    (folder / "main.beancount").write_text(
        LEDGER + '\n2026-01-01 custom "fava-extension" "tally.fava_page"\n'
    )
    app = create_app([str(folder / "main.beancount")])
    app.testing = True
    client = app.test_client()
    sent = client.post("/beancount/extension/Tally/ask", data={"question": "Am I on track?"})
    assert sent.status_code == 303
    assert [q.text for q in advice.questions(tmp_path)] == ["Am I on track?"]
    page = client.get("/beancount/extension/Tally/").get_data(as_text=True)
    assert "Am I on track?" in page and "Waiting for Claude" in page
