from pathlib import Path

import pytest

from tally import store
from tally.alerts import Alert
from tally.cli import main

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
    assert "Matched to posted rows: 0 alert(s)" in out
    assert "Waiting: 2 alert(s)" in out
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


def test_alert_the_csv_should_show_is_not_found(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Discover CSV fixture runs to 03/12, well past this alert, but has no row for it.
    save(data, "old.eml", DISCOVER.read_bytes().replace(b"March 12, 2026", b"March 2, 2026"))
    assert main(["import", str(FIXTURES / "discover_recent.csv")]) == 0
    assert main(["alerts"]) == 0
    out = capsys.readouterr().out
    assert "Not found: 1 alert(s) the CSVs should show by now" in out
    assert "  2026-03-02  discover         -42.10  MAPLE MARKET #0412" in out
    assert "Waiting: 0 alert(s)" in out
