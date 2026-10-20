import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tally import store
from tally.alert_reading import pending
from tally.alerts import Alert
from tally.cli import main
from tally.transaction import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVER = FIXTURES / "discover_alert.eml"
BOFA = FIXTURES / "bofa_alert.eml"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    return data


def save(data: Path, name: str, raw: bytes) -> None:
    folder = data / "alerts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(raw)


def put_fixtures(data: Path) -> None:
    save(data, "one.eml", DISCOVER.read_bytes())
    save(data, "two.eml", BOFA.read_bytes())


def stored(data: Path) -> list[Alert]:
    conn = store.connect(data / "tally.db")
    try:
        return store.alerts(conn)
    finally:
        conn.close()


def test_no_alerts_folder(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["alerts"]) == 0
    assert "Nothing to read: no .eml files" in capsys.readouterr().out


def test_reads_and_stores_each_alert_once(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put_fixtures(data)
    assert main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert "one.eml: added  discover  2026-03-12  -42.10  MAPLE MARKET #0412" in out
    assert "two.eml: added  bofa_checking  2026-03-14  -23.45" in out
    assert "2 added, 0 already stored, 0 skipped, 0 refused." in out
    assert "Pending: 2 alert(s)" in out
    assert [a.account for a in stored(data)] == ["discover", "bofa_checking"]

    assert main(["alerts"]) == 0
    assert "0 added, 2 already stored" in capsys.readouterr().out
    assert len(stored(data)) == 2
    assert sorted(p.name for p in (data / "alerts").iterdir()) == ["one.eml", "two.eml"]


def test_dry_run_saves_nothing(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put_fixtures(data)
    assert main(["alerts", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Dry run: nothing will be saved.")
    assert "2 added" in out
    assert not (data / "tally.db").exists()


def test_forged_email_is_refused_and_the_rest_stored(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put_fixtures(data)
    forged = DISCOVER.read_bytes().replace(b"dkim=pass", b"dkim=fail")
    save(data, "forged.eml", forged)
    assert main(["alerts"]) == 1
    captured = capsys.readouterr()
    assert "forged.eml: refused: Gmail didn't confirm" in captured.err
    assert "2 added, 0 already stored, 0 skipped, 1 refused." in captured.out
    assert len(stored(data)) == 2


def test_other_emails_are_skipped(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    newsletter = DISCOVER.read_bytes().replace(
        b"<discover@services.discover.com>", b"<news@example.com>"
    )
    statement = DISCOVER.read_bytes().replace(
        b"Subject: Transaction Alert", b"Subject: Your statement is ready"
    )
    save(data, "news.eml", newsletter)
    save(data, "statement.eml", statement)
    save(data, "notes.txt", b"not an email")
    assert main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert "news.eml: skipped, not from Discover or Bank of America" in out
    assert "statement.eml: skipped, not a transaction alert" in out
    assert "notes.txt" not in out
    assert "0 added, 0 already stored, 2 skipped, 0 refused." in out


def test_alerts_folder_is_made_owner_only(data: Path) -> None:
    put_fixtures(data)
    (data / "alerts").chmod(0o755)
    assert main(["alerts"]) == 0
    assert (data / "alerts").stat().st_mode & 0o777 == 0o700


def alert(day: int, amount: str, account: str = "discover") -> Alert:
    return Alert(
        account=account,
        date=datetime.date(2026, 3, day),
        merchant="MAPLE MARKET",
        amount=Decimal(amount),
        message_id=f"<{day}{amount}@example.com>",
        received=datetime.datetime(2026, 3, day, 12, tzinfo=datetime.UTC),
    )


def tx(day: int, amount: str, account: str = "discover") -> Transaction:
    return Transaction(
        account=account,
        date=datetime.date(2026, 3, day),
        posted=datetime.date(2026, 3, day),
        description="MAPLE MARKET SPRINGFIELD IL",
        merchant="MAPLE MARKET SPRINGFIELD IL",
        amount=Decimal(amount),
    )


def test_posted_row_explains_its_alert() -> None:
    assert pending([alert(12, "-42.10")], [tx(12, "-42.10")]) == []


def test_tip_and_late_posting_still_match() -> None:
    assert pending([alert(12, "-11.80")], [tx(15, "-13.45")]) == []


def test_too_far_apart_stays_pending() -> None:
    assert pending([alert(12, "-11.80")], [tx(18, "-11.80")]) == [alert(12, "-11.80")]
    assert pending([alert(12, "-11.80")], [tx(12, "-16.81")]) == [alert(12, "-11.80")]


def test_other_account_does_not_match() -> None:
    a = alert(12, "-42.10")
    assert pending([a], [tx(12, "-42.10", account="bofa_checking")]) == [a]


def test_one_row_explains_one_alert() -> None:
    # Two equal coffees, only one posted so far.
    first, second = alert(12, "-4.09"), alert(13, "-4.09")
    assert pending([first, second], [tx(12, "-4.09")]) == [second]


def test_closest_amount_wins() -> None:
    small, large = alert(12, "-10.00"), alert(12, "-12.00")
    # The -12.00 row is within $5 of both alerts, but must not be used up by the -10.00 one.
    assert pending([small, large], [tx(12, "-12.00"), tx(12, "-10.50")]) == []
