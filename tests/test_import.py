import shutil
import stat
from pathlib import Path

import pytest

from tally import store
from tally.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVER = FIXTURES / "discover_recent.csv"
BOFA = FIXTURES / "bofa_checking.csv"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(data))
    return data


def put_in_inbox(data: Path, source: Path, name: str) -> Path:
    inbox = data / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copyfile(source, inbox / name))


def stored_count(data: Path) -> int:
    conn = store.connect(data / "tally.db")
    try:
        return len(store.transactions(conn))
    finally:
        conn.close()


def test_no_arguments_prints_help() -> None:
    assert main([]) == 0


def test_empty_inbox(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import"]) == 0
    assert "Nothing to import" in capsys.readouterr().out
    assert (data / "inbox").is_dir()


def test_imports_inbox_and_empties_it(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put_in_inbox(data, DISCOVER, "Discover-RecentActivity.csv")
    put_in_inbox(data, BOFA, "stmt.csv")

    assert main(["import"]) == 0
    out = capsys.readouterr().out
    assert "stmt.csv  bofa_checking  03/02–03/13  13 rows: 13 new, 0 already imported" in out
    assert "Discover-RecentActivity.csv  discover  03/07–03/12  9 rows: 9 new" in out
    assert stored_count(data) == 22
    assert list((data / "inbox").iterdir()) == []
    assert len(list((data / "raw" / "discover").glob("*.csv"))) == 1
    [archived] = (data / "raw" / "bofa_checking").glob("*.csv")
    assert archived.read_bytes() == BOFA.read_bytes()


def test_bank_is_recognized_by_content_not_name(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put_in_inbox(data, BOFA, "discover.csv")
    assert main(["import"]) == 0
    assert "discover.csv  bofa_checking" in capsys.readouterr().out


def test_reimport_adds_nothing_and_archives_once(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(DISCOVER)]) == 0
    assert main(["import", str(DISCOVER)]) == 0
    assert "9 rows: 0 new, 9 already imported" in capsys.readouterr().out
    assert stored_count(data) == 9
    assert len(list((data / "raw" / "discover").glob("*.csv"))) == 1


def test_named_files_are_left_in_place(data: Path, tmp_path: Path) -> None:
    mine = Path(shutil.copyfile(DISCOVER, tmp_path / "mine.csv"))
    assert main(["import", str(mine)]) == 0
    assert mine.exists()


def test_dry_run_saves_nothing(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put_in_inbox(data, DISCOVER, "d.csv")
    assert main(["import", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing will be saved." in out
    assert "9 rows: 9 new" in out
    assert "  2026-03-12      -12.34  MAPLE MARKET #0412 SPRINGFIELD IL" in out
    assert (data / "inbox" / "d.csv").exists()
    assert not (data / "tally.db").exists()
    assert not (data / "raw").exists()


def test_dry_run_sees_existing_data_and_earlier_files(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(BOFA)]) == 0
    put_in_inbox(data, DISCOVER, "a.csv")
    put_in_inbox(data, DISCOVER, "b.csv")
    put_in_inbox(data, BOFA, "stmt.csv")
    capsys.readouterr()

    assert main(["import", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "stmt.csv  bofa_checking  03/02–03/13  13 rows: 0 new" in out
    assert out.count("9 rows: 9 new") == 1
    assert out.count("9 rows: 0 new") == 1
    assert stored_count(data) == 13


def test_bad_file_stays_in_inbox_and_others_still_import(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data / "inbox").mkdir(parents=True)
    (data / "inbox" / "notes.csv").write_text("hello,world\n")
    put_in_inbox(data, DISCOVER, "d.csv")

    assert main(["import"]) == 1
    captured = capsys.readouterr()
    assert (
        "notes.csv: not imported: line 1: not a Discover, BofA or Fidelity positions export"
        in captured.err
    )
    assert "d.csv  discover" in captured.out
    assert [p.name for p in (data / "inbox").iterdir()] == ["notes.csv"]
    assert stored_count(data) == 9


def test_broken_export_stays_in_inbox(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = put_in_inbox(data, BOFA, "stmt.csv")
    broken.write_bytes(broken.read_bytes().replace(b'"-42.10"', b'"-42.01"'))
    assert main(["import"]) == 1
    assert "stmt.csv: not imported: line 14: running balance" in capsys.readouterr().err
    assert broken.exists()
    assert stored_count(data) == 0


def test_gap_stays_in_inbox(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import", str(BOFA)]) == 0
    later = put_in_inbox(data, BOFA, "later.csv")
    later.write_text(
        "Description,,Summary Amt.\r\n"
        'Beginning balance as of 04/01/2026,,"5,000.00"\r\n'
        'Total credits,,"0.00"\r\n'
        'Total debits,,"-1.00"\r\n'
        'Ending balance as of 04/30/2026,,"4,999.00"\r\n'
        "\r\n"
        "Date,Description,Amount,Running Bal.\r\n"
        '04/01/2026,Beginning balance as of 04/01/2026,,"5,000.00"\r\n'
        '04/02/2026,"SHOP","-1.00","4,999.00"\r\n'
    )
    assert main(["import"]) == 1
    assert "later.csv: not imported: gap" in capsys.readouterr().err
    assert later.exists()
    assert stored_count(data) == 13


def test_binary_file_is_rejected(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (data / "inbox").mkdir(parents=True)
    (data / "inbox" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    assert main(["import"]) == 1
    assert "photo.jpg: not imported: not a text file" in capsys.readouterr().err


def test_hidden_files_in_inbox_are_ignored(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (data / "inbox").mkdir(parents=True)
    (data / "inbox" / ".DS_Store").write_bytes(b"\x00\x00")
    assert main(["import"]) == 0
    assert "Nothing to import" in capsys.readouterr().out


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_created_folders_and_database_are_owner_only(data: Path) -> None:
    put_in_inbox(data, DISCOVER, "d.csv")
    for folder in (data, data / "inbox"):
        folder.chmod(0o755)

    assert main(["import"]) == 0
    for folder in (data, data / "inbox", data / "raw", data / "raw" / "discover"):
        assert mode(folder) == 0o700, folder
    assert mode(data / "tally.db") == 0o600
    [archived] = (data / "raw" / "discover").iterdir()
    assert mode(archived) == 0o600
