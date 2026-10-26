import datetime
import json
import stat
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tally import categorizing, corrections, rules
from tally.categorizing import ClaudeError, ask_claude  # the real one, before conftest's stand-in
from tally.transaction import Transaction

TODAY = datetime.date(2026, 3, 20)
RULES = """categories = ["Groceries", "Eating out / dates"]

[[rule]]
match = "MARKET"
category = "Groceries"

[[rule]]
merchant = "VENMO"
date = 2026-03-05
amount = -25.00
category = "Eating out / dates"
"""


def tx(merchant: str, amount: str = "-12.00", day: int = 7) -> Transaction:
    return Transaction(
        account="discover",
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


class FakeClaude:
    """Answers with the categories it's given, and keeps the questions it was asked."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.questions: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, question: str, schema: dict[str, Any]) -> Any:
        self.questions.append((question, schema))
        return {"answers": [{"merchant": m, "category": c} for m, c in self.answers.items()]}


def test_answers_become_rules_at_the_end() -> None:
    claude = FakeClaude({"TACO TRUCK": "Eating out / dates"})
    result = categorizing.add(RULES, [tx("MAPLE MARKET"), tx("TACO TRUCK")], claude, TODAY)
    assert result.messages == ["TACO TRUCK → Eating out / dates"]
    assert result.text.startswith(RULES.rstrip("\n"))
    assert result.text.endswith(
        f"{categorizing.SECTION_START}\n\n"
        "[[rule]]  # by Claude 2026-03-20\n"
        'merchant = "TACO TRUCK"\n'
        'category = "Eating out / dates"\n\n'
        f"{categorizing.SECTION_END}\n"
    )
    assert rules.parse(result.text).categorize(tx("TACO TRUCK", day=19)) == "Eating out / dates"


def test_only_uncategorized_merchants_are_asked_about_once_each() -> None:
    claude = FakeClaude({})
    rows = [tx("MAPLE MARKET"), tx("TACO TRUCK"), tx("TACO TRUCK", "-8.50"), tx("CAFE")]
    categorizing.add(RULES, rows, claude, TODAY)
    [(question, _)] = claude.questions
    assert '"TACO TRUCK"  (account: discover; amounts: -12.00, -8.50)' in question
    assert '"CAFE"' in question
    assert '"MAPLE MARKET"' not in question


def test_nothing_uncategorized_asks_nothing() -> None:
    claude = FakeClaude({})
    assert categorizing.add(RULES, [tx("MAPLE MARKET")], claude, TODAY).text == RULES
    assert claude.questions == []


def test_question_shows_your_merchant_rules_but_not_one_offs() -> None:
    claude = FakeClaude({})
    categorizing.add(RULES, [tx("TACO TRUCK")], claude, TODAY)
    [(question, schema)] = claude.questions
    assert "  MARKET -> Groceries" in question
    assert "VENMO" not in question
    enum = schema["properties"]["answers"]["items"]["properties"]["category"]["enum"]
    assert enum == ["Groceries", "Eating out / dates", *rules.NOT_SPENDING, "Uncategorized"]


def test_claudes_own_rules_are_not_examples() -> None:
    text = categorizing.add(RULES, [tx("CAFE")], FakeClaude({"CAFE": "Groceries"}), TODAY).text
    claude = FakeClaude({})
    categorizing.add(text, [tx("DELI")], claude, TODAY)
    assert "CAFE" not in claude.questions[0][0]


def test_unsure_unknown_and_unasked_answers_are_dropped() -> None:
    claude = FakeClaude({"TACO TRUCK": "Uncategorized", "CAFE": "Snacks", "OTHER": "Groceries"})
    result = categorizing.add(RULES, [tx("TACO TRUCK"), tx("CAFE")], claude, TODAY)
    assert result.messages == ["Couldn't tell, left uncategorized: CAFE, TACO TRUCK"]
    assert "[[rule]]  # by Claude" not in result.text


def test_later_answers_join_the_section() -> None:
    first = categorizing.add(RULES, [tx("CAFE")], FakeClaude({"CAFE": "Eating out / dates"}), TODAY)
    second = categorizing.add(
        first.text, [tx("CAFE"), tx("DELI")], FakeClaude({"DELI": "Groceries"}), TODAY
    )
    assert second.text.count(categorizing.SECTION_START) == 1
    assert second.text.index('"CAFE"') < second.text.index('"DELI"')
    assert second.text.rstrip().endswith(categorizing.SECTION_END)


def test_your_rules_win_over_claudes() -> None:
    claude = FakeClaude({"CAFE": "Eating out / dates"})
    text = categorizing.add(RULES, [tx("CAFE")], claude, TODAY).text
    # A rule you add later, anywhere above Claude's section.
    text = text.replace(
        "[[rule]]\nmatch", '[[rule]]\nmatch = "CAFE"\ncategory = "Groceries"\n\n[[rule]]\nmatch', 1
    )
    assert rules.parse(text).categorize(tx("CAFE")) == "Groceries"


def test_malformed_answer_is_an_error() -> None:
    def broken(question: str, schema: dict[str, Any]) -> Any:
        return {"categories": "Groceries"}

    with pytest.raises(ClaudeError, match="unexpected answer"):
        categorizing.add(RULES, [tx("CAFE")], broken, TODAY)


def test_correcting_claudes_choice_changes_its_rule() -> None:
    text = categorizing.add(RULES, [tx("CAFE")], FakeClaude({"CAFE": "Groceries"}), TODAY).text
    result = corrections.apply(text, [(tx("CAFE"), "Eating out / dates")], TODAY)
    assert result.messages == [
        "Changed Claude's rule for CAFE to Eating out / dates: CAFE (03/07/2026, -12.00)"
    ]
    assert "# corrected on the Sheet" not in result.text
    assert rules.parse(result.text).categorize(tx("CAFE", day=19)) == "Eating out / dates"


def test_correcting_a_row_your_rules_decide_leaves_claudes_rule() -> None:
    # Claude's rule for this exact name never applied: "MARKET" in your rules came first.
    text = RULES + (
        f'\n{categorizing.SECTION_START}\n\n[[rule]]\nmerchant = "MAPLE MARKET"\n'
        f'category = "Eating out / dates"\n\n{categorizing.SECTION_END}\n'
    )
    result = corrections.apply(text, [(tx("MAPLE MARKET"), "Eating out / dates")], TODAY)
    assert result.messages[0].startswith("Added rule: MAPLE MARKET")
    assert rules.parse(result.text).categorize(tx("MAPLE MARKET", day=19)) == "Groceries"


# ---- The real call, against a stand-in `claude` program ----


@pytest.fixture
def program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `claude` on PATH that saves its arguments and prints the output it's given."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    monkeypatch.setenv("PATH", str(script.parent))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return script


def fake_claude(script: Path, output: str, code: int = 0) -> None:
    saved = script.parent / "called.json"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"json.dump({{'args': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),"
        f" 'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')}}, open({str(saved)!r}, 'w'))\n"
        f"print({output!r})\n"
        f"sys.exit({code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def called(script: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((script.parent / "called.json").read_text())
    return result


def test_ask_claude_with_no_tools(program: Path) -> None:
    fake_claude(program, json.dumps({"is_error": False, "structured_output": {"answers": []}}))
    assert ask_claude("which category?", {"type": "object"}, "token-123") == {"answers": []}
    call = called(program)
    args = call["args"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--json-schema") + 1] == '{"type": "object"}'
    assert "--print" in args and "--no-session-persistence" in args
    assert call["stdin"] == "which category?"
    assert call["token"] == "token-123"
    assert call["cwd"] != str(Path.cwd())


def test_ask_claude_reports_an_error_answer(program: Path) -> None:
    fake_claude(program, json.dumps({"is_error": True, "result": "Not logged in"}), code=1)
    with pytest.raises(ClaudeError, match="Not logged in"):
        ask_claude("q", {}, None)


def test_ask_claude_reports_a_crash(program: Path) -> None:
    fake_claude(program, "", code=2)
    with pytest.raises(ClaudeError, match="exit 2"):
        ask_claude("q", {}, None)


def test_ask_claude_without_claude_code(program: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(program.parent / "nowhere"))
    with pytest.raises(ClaudeError, match="isn't installed"):
        ask_claude("q", {}, None)


def test_load_token(tmp_path: Path) -> None:
    assert categorizing.load_token(tmp_path / "claude-token") is None
    (tmp_path / "claude-token").write_text("token-123\n")
    assert categorizing.load_token(tmp_path / "claude-token") == "token-123"
