import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_picture import GOALS, INVESTING, build_with

from tally import advice, summary, summary_email
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
    "try_next": ["Put $420 toward the emergency fund."],
}


def test_facts_spell_out_the_picture() -> None:
    text = advice.facts(build_with(GOALS), ABOUT)
    assert "- first job; wants to travel more" in text
    assert "February 2026: pay $2,500, take-home $2,000, spent $1,300" in text
    assert "- Groceries: $50, plan $300, usual $300." in text
    assert "Emergency fund: $200 of a $3,900 target, $3,700 to go; suggested $420" in text


def test_facts_include_investing() -> None:
    text = advice.facts(build_with(INVESTING, roth_limit=Decimal("7500")), ABOUT)
    assert "- Roth: worth $1,540, paid $1,500, gain $40 (+3%)." in text
    assert (
        "Roth IRA 2026: $500 contributed of the $7,500 limit, $7,000 of room left; "
        "contributions for 2026 can be made until April 15, 2027." in text
    )


def test_last_month_is_reviewed_once(tmp_path: Path) -> None:
    found = advice.review(tmp_path, build_with(GOALS), ABOUT, Claude(REVIEW), NOW)
    assert found is not None
    assert (found.title, found.headline, found.watch) == ("February 2026", "A steady month.", [])
    saved = json.loads((tmp_path / "advice" / "review-2026-02.json").read_text())
    assert saved["headline"] == "A steady month."
    # Written, so sent: the next summary has none, and doesn't ask.
    assert advice.review(tmp_path, build_with(GOALS), ABOUT, Claude(), NOW) is None


def test_an_amount_it_made_up_is_asked_again_then_refused(tmp_path: Path) -> None:
    made_up = {**REVIEW, "headline": "You saved $612."}  # not in the facts
    claude = Claude(made_up, REVIEW)
    assert advice.review(tmp_path, build_with(GOALS), ABOUT, claude, NOW) is not None
    assert "Your last answer quoted $612" in claude.prompts[1]

    other = tmp_path / "other"
    with pytest.raises(ClaudeError, match=r"\$612"):
        advice.review(other, build_with(GOALS), ABOUT, Claude(made_up, made_up), NOW)


def test_a_failed_review_is_tried_again_next_time(tmp_path: Path) -> None:
    with pytest.raises(ClaudeError):
        advice.review(tmp_path, build_with(GOALS), ABOUT, Claude(ClaudeError("no answer")), NOW)
    assert not (tmp_path / "advice" / "review-2026-02.json").exists()
    assert advice.review(tmp_path, build_with(GOALS), ABOUT, Claude(REVIEW), NOW) is not None


def test_the_review_is_in_the_summary(tmp_path: Path) -> None:
    found = advice.review(tmp_path, build_with(GOALS), ABOUT, Claude(REVIEW), NOW)
    s = summary.Summary(NOW.date(), [], [], None, [], [], None, review=found)
    text = summary.format_summary(s)
    assert (
        "CLAUDE'S REVIEW OF FEBRUARY 2026\n  A steady month.\n  Went well:\n  - Rent on time."
        in text
    )
    assert "Watch:" not in text
    html = summary_email.format_html(s)
    assert "Claude&#x27;s review of February 2026" in html
    assert '<li style="margin:2px 0;">Put $420 toward the emergency fund.</li>' in html
