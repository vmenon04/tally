import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally import corrections, rules
from tally.transaction import Transaction

TODAY = datetime.date(2026, 3, 20)

RULES = """# my notes stay put
categories = ["Groceries", "Home / apartment stuff"]

# ---- General ----

[[rule]]
match = "MARKET"
category = "Groceries"
"""


def tx(merchant: str = "MAPLE MARKET", day: int = 7, amount: str = "-42.10") -> Transaction:
    return Transaction(
        account="discover",
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description=merchant,
        merchant=merchant,
        amount=Decimal(amount),
    )


def test_correction_goes_in_a_new_section_above_the_rules() -> None:
    result = corrections.apply(RULES, [(tx(), "Home / apartment stuff")], TODAY)
    assert result.text == (
        "# my notes stay put\n"
        'categories = ["Groceries", "Home / apartment stuff"]\n'
        "\n"
        f"{corrections.SECTION_START}\n"
        "\n"
        "[[rule]]  # set on a pending purchase 2026-03-20\n"
        'account = "discover"\n'
        "date = 2026-03-07\n"
        "amount = -42.10\n"
        'merchant = "MAPLE MARKET"\n'
        'category = "Home / apartment stuff"\n'
        "\n"
        f"{corrections.SECTION_END}\n"
        "\n"
        "# ---- General ----\n"
        "\n"
        "[[rule]]\n"
        'match = "MARKET"\n'
        'category = "Groceries"\n'
    )
    assert result.messages == [
        "Added rule: MAPLE MARKET (03/07/2026, -42.10) → Home / apartment stuff"
    ]
    assert rules.parse(result.text).categorize(tx()) == "Home / apartment stuff"
    assert rules.parse(result.text).categorize(tx(day=8)) == "Groceries"


def test_everything_outside_the_section_is_untouched() -> None:
    first = corrections.apply(RULES, [(tx(), "Home / apartment stuff")], TODAY).text
    second = corrections.apply(first, [(tx(day=8), "Home / apartment stuff")], TODAY).text
    outside = first.split(corrections.SECTION_START)[0], first.split(corrections.SECTION_END)[1]
    assert second.split(corrections.SECTION_START)[0] == outside[0]
    assert second.split(corrections.SECTION_END)[1] == outside[1]
    assert second.count("[[rule]]  # set on a pending purchase") == 2


def test_correcting_again_replaces_the_rule() -> None:
    text = corrections.apply(RULES, [(tx(), "Home / apartment stuff")], TODAY).text
    text = corrections.apply(text, [(tx(), "Income")], TODAY).text
    assert text.count("[[rule]]  # set on a pending purchase") == 1
    assert rules.parse(text).categorize(tx()) == "Income"


def test_clearing_removes_the_rule() -> None:
    text = corrections.apply(RULES, [(tx(), "Home / apartment stuff")], TODAY).text
    result = corrections.apply(text, [(tx(), "")], TODAY)
    assert "# corrected" not in result.text
    assert result.messages == ["Removed correction: MAPLE MARKET (03/07/2026, -42.10)"]
    assert rules.parse(result.text).categorize(tx()) == "Groceries"


def test_clearing_without_a_correction_changes_nothing() -> None:
    result = corrections.apply(RULES, [(tx(), "")], TODAY)
    assert result.messages == ["No change: MAPLE MARKET (03/07/2026, -42.10)"]
    assert "# corrected" not in result.text


def test_correction_matching_the_rules_adds_nothing() -> None:
    result = corrections.apply(RULES, [(tx(), "Groceries")], TODAY)
    assert "# corrected" not in result.text
    assert result.messages[0].startswith("No rule needed, the rules already say Groceries")


def test_unknown_category_is_refused() -> None:
    with pytest.raises(rules.RulesError, match="'Groceriez' is not a category"):
        corrections.apply(RULES, [(tx(), "Groceriez")], TODAY)


def test_section_without_end_is_refused() -> None:
    broken = RULES + f"\n{corrections.SECTION_START}\n"
    with pytest.raises(rules.RulesError, match="but not"):
        corrections.apply(broken, [(tx(), "Income")], TODAY)


def test_file_without_rules_gets_the_section_at_the_end() -> None:
    text = 'categories = ["Groceries"]\n'
    result = corrections.apply(text, [(tx(), "Income")], TODAY)
    assert result.text.startswith(text)
    assert rules.parse(result.text).categorize(tx()) == "Income"


def test_quotes_in_merchant_names_are_escaped() -> None:
    odd = tx(merchant='JOE\'S "BEST" CAFE')
    result = corrections.apply(RULES, [(odd, "Income")], TODAY)
    assert rules.parse(result.text).categorize(odd) == "Income"


def test_save_replaces_the_file_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "rules.toml"
    path.write_text("old")
    path.chmod(0o644)
    corrections.save(path, RULES)
    assert path.read_text() == RULES
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


ALERT = tx("MAPLE MARKET #0412", day=7, amount="-40.00")  # as the alert had it
POSTED = tx("MAPLE MARKET #0412 SPRINGFIELD IL", day=8, amount="-42.10")  # as the CSV has it


def test_carry_over_moves_the_alerts_correction() -> None:
    text = corrections.apply(RULES, [(ALERT, "Home / apartment stuff")], TODAY).text
    result = corrections.carry_over(text, [(ALERT, POSTED)], TODAY)
    assert result.messages == [
        "Posted, correction carried over: MAPLE MARKET #0412 SPRINGFIELD IL "
        "(03/08/2026, -42.10) → Home / apartment stuff"
    ]
    moved = rules.parse(result.text)
    assert moved.categorize(POSTED) == "Home / apartment stuff"
    assert moved.categorize(ALERT) == "Groceries"  # the alert's own rule is gone
    assert result.text.count("[[rule]]") == text.count("[[rule]]")


def test_carry_over_without_corrections_changes_nothing() -> None:
    assert corrections.carry_over(RULES, [(ALERT, POSTED)], TODAY).text == RULES
    text = corrections.apply(RULES, [(tx("OTHER"), "Groceries")], TODAY).text
    assert corrections.carry_over(text, [(ALERT, POSTED)], TODAY) == corrections.Result(text, [])


def test_posted_rows_own_correction_wins() -> None:
    text = corrections.apply(
        RULES, [(ALERT, "Home / apartment stuff"), (POSTED, "Income")], TODAY
    ).text
    result = corrections.carry_over(text, [(ALERT, POSTED)], TODAY)
    assert result.messages == [
        "Kept the posted row's own correction: MAPLE MARKET #0412 SPRINGFIELD IL "
        "(03/08/2026, -42.10)"
    ]
    assert 'MAPLE MARKET #0412"' not in result.text


def test_carry_over_when_the_rules_already_agree() -> None:
    # The CSV's longer name meets a general rule the alert's name didn't.
    general = RULES.replace(
        "[[rule]]\nmatch",
        '[[rule]]\nmatch = "SPRINGFIELD"\ncategory = "Home / apartment stuff"\n\n[[rule]]\nmatch',
    )
    text = corrections.apply(general, [(ALERT, "Home / apartment stuff")], TODAY).text
    result = corrections.carry_over(text, [(ALERT, POSTED)], TODAY)
    assert result.messages == [
        "Posted, and the rules already say Home / apartment stuff: "
        "MAPLE MARKET #0412 SPRINGFIELD IL (03/08/2026, -42.10)"
    ]
    assert "set on a pending purchase" not in result.text


def test_carry_over_reports_a_broken_file() -> None:
    with pytest.raises(rules.RulesError, match="not valid TOML"):
        corrections.carry_over("categories = [", [(ALERT, POSTED)], TODAY)


def test_the_old_sheet_header_is_read_and_renamed() -> None:
    old = "# ---- Corrections from the Sheet (tally sync adds these; edit or delete freely) ----"
    text = f'categories = ["Groceries"]\n\n{old}\n\n# ---- End of corrections ----\n'
    t = Transaction(
        account="discover",
        date=datetime.date(2026, 3, 18),
        posted=None,
        description="OAK BAKERY",
        merchant="OAK BAKERY",
        amount=Decimal("-9.00"),
    )
    result = corrections.apply(text, [(t, "Groceries")], datetime.date(2026, 3, 20))
    assert old not in result.text
    assert result.text.count(corrections.SECTION_START) == 1
