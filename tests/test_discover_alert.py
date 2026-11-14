import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally.alerts import Alert, AlertError, html_lines
from tally.alerts.discover import parse

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "discover_alert.eml"
REFUND = FIXTURES / "discover_refund_alert.eml"
GMAIL_CHECK = (
    "Authentication-Results: mx.google.com;\r\n"
    '       dkim=pass header.i=@discover.com header.s=testselector header.b="AbCdEf12";'
)


def fixture(*replacements: tuple[str, str], path: Path = FIXTURE) -> bytes:
    text = path.read_bytes().decode("ascii")
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text.encode("ascii")


def test_fixture_is_encoded_like_the_real_emails() -> None:
    raw = FIXTURE.read_bytes()
    assert b"\r\n" in raw
    assert b"Content-Transfer-Encoding: quoted-printable" in raw
    assert b'style=3D"' in raw


def test_alert_in_full() -> None:
    assert parse(FIXTURE.read_bytes()) == Alert(
        account="discover",
        date=datetime.date(2026, 3, 12),
        merchant="MAPLE MARKET #0412",
        amount=Decimal("-42.10"),
        message_id="<0100000000000000-test-alert-0001@email.amazonses.com>",
        received=datetime.datetime(2026, 3, 12, 15, 15, tzinfo=datetime.UTC),
    )


def test_amount_with_thousands_separator() -> None:
    alert = parse(fixture(("Amount: $42.10", "Amount: $1,234.56")))
    assert alert is not None
    assert alert.amount == Decimal("-1234.56")


def test_other_discover_emails_are_not_alerts() -> None:
    raw = fixture(("Subject: Transaction Alert", "Subject: Your statement is ready"))
    assert parse(raw) is None


def test_email_without_gmail_check_is_refused() -> None:
    with pytest.raises(AlertError, match="no authentication result from Gmail"):
        parse(fixture(("Authentication-Results: mx.google.com;", "X-Note: unrelated;")))


def test_failed_signature_is_refused() -> None:
    with pytest.raises(AlertError, match="signed by discover.com"):
        parse(fixture(("dkim=pass header.i=@discover.com", "dkim=fail header.i=@discover.com")))


def test_lookalike_signing_domain_is_refused() -> None:
    for lookalike in ("@discover.com.example.net", "@notdiscover.com"):
        with pytest.raises(AlertError, match="signed by discover.com"):
            parse(fixture(("header.i=@discover.com ", f"header.i={lookalike} ")))


def test_forged_check_below_gmails_is_ignored() -> None:
    raw = fixture(
        ("dkim=pass header.i=@discover.com", "dkim=fail header.i=@discover.com"),
        ("Subject: Transaction Alert", f"{GMAIL_CHECK}\r\nSubject: Transaction Alert"),
    )
    with pytest.raises(AlertError, match="signed by discover.com"):
        parse(raw)


def test_signed_email_from_another_sender_is_refused() -> None:
    with pytest.raises(AlertError, match="not discover.com"):
        parse(fixture(("<discover@services.discover.com>", "<alerts@example.com>")))


def test_changed_layout_is_refused() -> None:
    with pytest.raises(AlertError, match="'Amount:'"):
        parse(fixture(("Amount: $42.10", "Total: $42.10")))


def test_bad_date_is_refused() -> None:
    with pytest.raises(AlertError, match="bad date"):
        parse(fixture(("Date: March 12, 2026", "Date: 2026-03-12")))


def test_refund_in_full() -> None:
    # Money in, dated the day the refund posted.
    assert parse(REFUND.read_bytes()) == Alert(
        account="discover",
        date=datetime.date(2026, 3, 16),
        merchant="MAPLE MARKET #0412 SPRINGFIELD IL",
        amount=Decimal("16.06"),
        message_id="<0100000000000000-test-alert-0004@email.amazonses.com>",
        received=datetime.datetime(2026, 3, 17, 11, 47, 12, tzinfo=datetime.UTC),
    )


def test_refund_signature_is_checked() -> None:
    raw = fixture(
        ("dkim=pass header.i=@discover.com", "dkim=fail header.i=@discover.com"), path=REFUND
    )
    with pytest.raises(AlertError, match="signed by discover.com"):
        parse(raw)


def test_refund_with_a_changed_layout_is_refused() -> None:
    raw = fixture((">Amount Credited:<", ">Amount:<"), path=REFUND)
    with pytest.raises(AlertError, match="'Amount Credited:'"):
        parse(raw)


def test_refund_with_a_missing_value_is_refused() -> None:
    # The merchant's cell is the last line; without it there's nothing after the label.
    raw = fixture(
        ("MAPLE MARKET #0412 SPRINGFIELD IL</td></tr></table>", "</td></tr></table>"), path=REFUND
    )
    raw = raw.replace(b"See Transactions", b"")
    with pytest.raises(AlertError, match="'Merchant:'"):
        parse(raw)


def test_html_lines() -> None:
    html = (
        "<html><head><style>p { color: red; }</style></head><body>"
        "<p>One <strong>line</strong>\n  still one</p><p>A&nbsp;&amp; B<br/>C</p></body></html>"
    )
    assert html_lines(html) == ["One line still one", "A & B", "C"]


PAYMENT = FIXTURES / "discover_payment_alert.eml"


def test_payment_in_full() -> None:
    # Money in, dated the day it posted, named as the export names an online payment so the
    # posted row matches it and the rules categorize it.
    assert parse(PAYMENT.read_bytes()) == Alert(
        account="discover",
        date=datetime.date(2026, 3, 16),
        merchant="INTERNET PAYMENT - THANK YOU",
        amount=Decimal("1250.00"),
        message_id="<0100000000000000-test-alert-0005@email.amazonses.com>",
        received=datetime.datetime(2026, 3, 17, 12, 48, 59, tzinfo=datetime.UTC),
    )


def test_payment_matches_its_posted_row() -> None:
    from tally.matching import match
    from tally.transaction import Transaction

    alert = parse(PAYMENT.read_bytes())
    assert alert is not None
    row = Transaction(
        "discover",
        datetime.date(2026, 3, 16),
        datetime.date(2026, 3, 16),
        "INTERNET PAYMENT - THANK YOU",
        "INTERNET PAYMENT - THANK YOU",
        Decimal("1250.00"),
    )
    found = match([alert], [row])
    assert [(m.transaction, m.by_amount_only) for m in found.matches] == [(row, False)]


def test_payment_with_a_changed_layout_is_refused() -> None:
    raw = fixture(("posted to your account on <strong>", "posted on <strong>"), path=PAYMENT)
    with pytest.raises(AlertError, match="Your payment of"):
        parse(raw)
