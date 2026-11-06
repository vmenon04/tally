import dataclasses
import datetime

from test_summary import NO_LIMIT, RULES, SUNDAY, TARGETS, tx

from tally import summary, summary_email
from tally.transaction import Transaction


def html(*transactions: Transaction, pending: tuple[Transaction, ...] = ()) -> str:
    pairs = [(n, t) for n, t in enumerate(transactions, start=1)]
    pairs += [(-n, t) for n, t in enumerate(pending, start=1)]
    s = summary.build(SUNDAY, pairs, RULES, TARGETS, [], NO_LIMIT)
    return summary_email.format_html(s)


def test_layout() -> None:
    page = html(tx(13, "MARKET", "-12.50"), pending=(tx(14, "ARCADE", "-5.00"),))
    assert page.startswith("<!doctype html>")
    assert "Week of Mar 9 – Mar 15, 2026" in page
    assert ">$17.50</div>" in page
    assert "spent on 2 purchases · your plan allows about $316 a week" in page
    assert "Mar 15" in page and ">pending</span>" in page
    assert "March so far" in page and "day 15 of 31" in page
    assert "$12.50 of $300.00 · 4%" in page
    assert "Nothing this week." in page
    assert "docs.google.com" not in page  # no Sheet to link to


def test_bar_colors_follow_the_plan() -> None:
    # Day 15 of 31: Groceries 4% (on track), Fun 120% (over), Rent 80% (ahead of pace).
    page = html(
        tx(3, "MARKET", "-12.00"),
        tx(4, "ARCADE", "-120.00"),
        tx(1, "LANDLORD", "-800.00"),
    )
    assert 'width="4%" style="background:#188038' in page
    assert 'width="100%" style="background:#d93025' in page
    assert 'width="80%" style="background:#e37400' in page


def test_text_is_escaped() -> None:
    page = html(tx(13, '<img src=x onerror="alert(1)"> MARKET', "-1.00"))
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; MARKET" in page


def test_no_purchases_and_no_plan() -> None:
    s = summary.build(datetime.date(2026, 3, 15), [], RULES, None, [], NO_LIMIT, "(no plan)")
    page = summary_email.format_html(s)
    assert "No purchases." in page
    assert "(no plan)" in page
    assert "a week" not in page


def test_empty_category_has_an_empty_bar() -> None:
    page = html(tx(3, "MARKET", "-12.00"))
    assert 'width="0%"' not in page  # Fun and Rent: nothing spent, only the grey track
    assert 'width="100%" style="background:#e8eaed' in page


def test_claudes_note_is_escaped() -> None:
    s = summary.build(SUNDAY, [], RULES, TARGETS, [], NO_LIMIT)
    page = summary_email.format_html(dataclasses.replace(s, claude_note="<b>odd</b>"))
    assert "Claude&#x27;s note" in page or "Claude's note" in page
    assert "&lt;b&gt;odd&lt;/b&gt;" in page
