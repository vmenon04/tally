import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data, position
from test_ledger_importers import BOFA
from test_ledger_sync import ACCOUNTS, MAIN, RULES
from test_sync import BUDGET_ID, FakeSheet, budget_grid

from tally import sheet, store
from tally.cli import main
from tally.ledger import book, weekly

POSITIONS = Path(__file__).parent / "fixtures" / "fidelity_positions.csv"
FIDELITY_ACCOUNTS = """
2026-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1111"
2026-01-01 open Assets:Fidelity:401k
  fidelity-account: "22222"
2026-01-01 open Assets:Fidelity:Go
  fidelity-account: "3333"
2026-01-01 open Assets:Fidelity:Go:Cash USD
2026-01-01 open Income:Fidelity:Unreconciled
"""


@pytest.fixture
def data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(folder))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(sheet, "GoogleSheet", FakeSheet)
    FakeSheet.tab_ids = {"Budget": BUDGET_ID}
    FakeSheet.grids = {BUDGET_ID: budget_grid()}
    ledger = folder / "ledger"
    (ledger / "imported").mkdir(parents=True)
    (ledger / "main.beancount").write_text(MAIN)
    (ledger / "accounts.beancount").write_text(ACCOUNTS + FIDELITY_ACCOUNTS)
    (ledger / "imported" / "2026.beancount").write_text("")
    (folder / "config.toml").write_text('sheet_id = "test-sheet"\nroth_limit = 7500\n')
    (folder / "rules.toml").write_text(RULES)
    # BofA in March, with 500.00 into the Roth IRA on 03/10.
    assert main(["import", str(BOFA)]) == 0
    capsys.readouterr()
    return folder


def ledger_entries(folder: Path) -> list[data.Directive]:
    from tally import rules

    return book.load(folder / "ledger", rules.parse(RULES)).entries


def held(folder: Path, account: str, symbol: str) -> tuple[Decimal, Decimal]:
    units = cost = Decimal(0)
    for e in ledger_entries(folder):
        if isinstance(e, data.Transaction):
            for p in e.postings:
                if p.account == account and p.units and p.units.currency == symbol:
                    units += p.units.number or 0
                    assert isinstance(p.cost, position.Cost)
                    cost += (p.units.number or 0) * p.cost.number
    return units, cost.quantize(Decimal("0.01"))


def snapshot_text(**changes: str) -> str:
    text = POSITIONS.read_text(encoding="utf-8", newline="")
    for old, new in changes.items():
        text = text.replace(old.replace("_", " "), new)
    return text


def import_text(
    folder: Path, tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    path = tmp_path / "positions.csv"
    path.write_text(text, encoding="utf-8", newline="")
    code = main(["import", str(path)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_first_snapshot_opens_holdings_at_cost(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    out = capsys.readouterr().out
    assert "Fidelity positions as of 03/20  3 accounts" in out
    assert book.check(data_dir / "ledger") == []
    assert held(data_dir, "Assets:Fidelity:Roth", "FXAIX") == (Decimal("10.5"), Decimal("2000.00"))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("100.123"),
        Decimal("1500.00"),
    )
    text = (data_dir / "ledger" / "imported" / "2026.beancount").read_text()
    assert "FXAIX {{2000.00 USD}}" in text
    assert "2026-03-20 price FXAIX" in text
    assert "2026-03-21 balance Assets:Fidelity:Roth:Cash" in text
    # The 500.00 contributed on 03/10 went into holdings; the rest came before the ledger.
    roth = next(
        e
        for e in ledger_entries(data_dir)
        if isinstance(e, data.Transaction) and e.meta.get("fidelity-account") == "1111"
    )
    assert roth.flag == "*"
    sides = {
        p.account: p.units.number for p in roth.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Assets:Fidelity:Roth:Cash"] == Decimal("-487.50")
    assert sides["Equity:Opening-Balances"] == Decimal("-2012.50")


def test_the_database_never_sees_positions(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = store.connect(data_dir / "tally.db")
    before = conn.execute("SELECT count(*) FROM imports").fetchone()
    conn.close()
    assert main(["import", str(POSITIONS)]) == 0
    conn = store.connect(data_dir / "tally.db")
    assert conn.execute("SELECT count(*) FROM imports").fetchone() == before
    conn.close()


def test_the_same_file_again_adds_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    capsys.readouterr()
    assert main(["import", str(POSITIONS)]) == 0
    assert "ledger: 0 new entries" in capsys.readouterr().out


def test_a_later_snapshot_moves_holdings_to_fidelity_cost_basis(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    capsys.readouterr()
    later = snapshot_text(
        **{
            "Mar-20-2026": "Mar-27-2026",
            # Bought 2 more FXAIX for 410.00 out of cash; a 2.00 dividend landed in cash.
            "10.5,$200.00": "12.5,$205.00",
            "$2000.00,$190.48": "$2410.00,$192.80",
            "$12.50": "$14.50",
            # Sold all of ABCD.
            "Z00001111,ROTH IRA,ABCD": "Z00009999,OTHER,ABCD",
        }
    )
    later = "\r\n".join(line for line in later.split("\r\n") if "Z00009999" not in line)
    code, out, err = import_text(data_dir, tmp_path, later, capsys)
    assert code == 0, err
    assert book.check(data_dir / "ledger") == []
    assert held(data_dir, "Assets:Fidelity:Roth", "FXAIX") == (Decimal("12.5"), Decimal("2410.00"))
    assert held(data_dir, "Assets:Fidelity:Roth", "ABCD")[0] == 0
    second = [
        e
        for e in ledger_entries(data_dir)
        if isinstance(e, data.Transaction) and e.meta.get("fidelity-account") == "1111"
    ][-1]
    assert second.flag == "!"
    assert any(p.account == "Income:Fidelity:Unreconciled" for p in second.postings)


def test_an_older_snapshot_is_refused(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    capsys.readouterr()
    code, _, err = import_text(
        data_dir, tmp_path, snapshot_text(**{"Mar-20-2026": "Mar-01-2026"}), capsys
    )
    assert code == 1
    assert "newer Fidelity snapshot" in err


def test_an_unknown_account_is_refused(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = import_text(
        data_dir, tmp_path, snapshot_text(**{"X00003333": "X00004444"}), capsys
    )
    assert code == 1
    assert 'fidelity-account: "4444"' in err


def test_roth_contributions_ignore_snapshot_cash(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    later = snapshot_text(**{"Mar-20-2026": "Mar-27-2026", "$12.50": "$20.00"})
    assert import_text(data_dir, tmp_path, later, capsys)[0] == 0
    found = weekly.build(data_dir / "ledger", datetime.date(2026, 3, 31), Decimal(7500), Decimal(0))
    assert found.roth_in == Decimal("500.00")


def test_accounts_are_named_by_last_four_or_whole_number(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    text = (data_dir / "ledger" / "imported" / "2026.beancount").read_text()
    # The 401(k) is named by its whole (short) number, the others by their last four.
    assert 'fidelity-account: "22222"' in text
    assert 'fidelity-account: "1111"' in text
    # Long account numbers never reach the ledger.
    assert "Z00001111" not in text and "X00003333" not in text
