import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally.alerts import Alert, AlertError, bofa, discover

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "bofa_alert.eml"


def fixture(*replacements: tuple[str, str]) -> bytes:
    text = FIXTURE.read_bytes().decode("ascii")
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text.encode("ascii")


def test_fixture_is_encoded_like_the_real_email() -> None:
    raw = FIXTURE.read_bytes()
    assert b"\r\n" in raw
    assert b"Content-Type: text/html" in raw
    assert b"multipart" not in raw


def test_alert_in_full() -> None:
    assert bofa.parse(FIXTURE.read_bytes()) == Alert(
        account="bofa_checking",
        date=datetime.date(2026, 3, 14),
        merchant="MAPLE MARKET #0412 SPRINGFIELD ,IL",
        amount=Decimal("-23.45"),
        message_id="<00000000-test-alert-0002@example.xt.local>",
        received=datetime.datetime(
            2026, 3, 14, 17, 49, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=-6))
        ),
    )


def test_amount_with_thousands_separator() -> None:
    alert = bofa.parse(fixture((">$23.45<", ">$1,234.56<")))
    assert alert is not None
    assert alert.amount == Decimal("-1234.56")


def test_other_bofa_emails_are_not_alerts() -> None:
    raw = fixture(("Subject: Your debit card was used", "Subject: Your statement is available"))
    assert bofa.parse(raw) is None


def test_signature_from_a_bofa_subdomain_is_accepted() -> None:
    # The real alerts are signed by ealerts.bankofamerica.com, not bankofamerica.com itself.
    assert b"header.i=@ealerts.bankofamerica.com" in FIXTURE.read_bytes()
    assert bofa.parse(FIXTURE.read_bytes()) is not None


def test_failed_signature_is_refused() -> None:
    with pytest.raises(AlertError, match="signed by bankofamerica.com"):
        bofa.parse(fixture(("dkim=pass header.i=@ealerts", "dkim=fail header.i=@ealerts")))


def test_signed_email_from_another_sender_is_refused() -> None:
    raw = fixture(("<onlinebanking@ealerts.bankofamerica.com>", "<alerts@example.com>"))
    with pytest.raises(AlertError, match="not bankofamerica.com"):
        bofa.parse(raw)


def test_each_parser_refuses_the_other_banks_email() -> None:
    with pytest.raises(AlertError, match="signed by discover.com"):
        discover.parse(FIXTURE.read_bytes())
    with pytest.raises(AlertError, match="signed by bankofamerica.com"):
        bofa.parse((FIXTURES / "discover_alert.eml").read_bytes())


def test_changed_layout_is_refused() -> None:
    with pytest.raises(AlertError, match="'Purchase made at'"):
        bofa.parse(fixture((">Purchase made at<", ">Merchant<")))


def test_label_without_a_value_is_refused() -> None:
    text = FIXTURE.read_bytes().decode("ascii")
    cut = text[: text.index("March 14, 2026")] + "</a></td></tr></table></body></html>\r\n"
    with pytest.raises(AlertError, match="'Purchase date' line followed by its value"):
        bofa.parse(cut.encode("ascii"))
