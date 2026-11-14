import datetime
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data, position
from test_ledger_importers import BOFA
from test_ledger_sync import ACCOUNTS, MAIN, RULES

from tally import store
from tally.cli import main
from tally.importers.fidelity import Account, Holding, Snapshot
from tally.ledger import book, positions, weekly

POSITIONS = Path(__file__).parent / "fixtures" / "fidelity_positions.csv"
FIDELITY_ACCOUNTS = """
2026-01-01 open Assets:Fidelity:Roth
  fidelity-account: "1111"
2026-01-01 open Assets:Fidelity:401k
  fidelity-account: "2222"
2026-01-01 open Assets:Fidelity:Go
  fidelity-account: "3333"
2026-01-01 open Assets:Fidelity:Go:Cash USD
2026-01-01 open Income:Fidelity:Unreconciled
2026-01-01 open Income:Fidelity:Dividends
"""


@pytest.fixture
def data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("TALLY_DATA_DIR", str(folder))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    ledger = folder / "ledger"
    (ledger / "imported").mkdir(parents=True)
    (ledger / "main.beancount").write_text(MAIN)
    (ledger / "plan.beancount").write_text("")
    (ledger / "accounts.beancount").write_text(ACCOUNTS + FIDELITY_ACCOUNTS)
    (ledger / "imported" / "2026.beancount").write_text("")
    (folder / "config.toml").write_text("roth_limit = 7500\n")
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


def holdings_snapshot(day: datetime.date, units: str, price: str, cost: str | None) -> Snapshot:
    return Snapshot(
        day,
        [
            Account(
                "2222",
                "EXAMPLE RET SAVINGS",
                None,
                [Holding("FFTGX", Decimal(units), Decimal(price), Decimal(cost) if cost else None)],
            ),
            Account("9999", "AN EMPTY ACCOUNT", None, []),
        ],
    )


def add_snapshot(folder: Path, snap: Snapshot) -> None:
    from tally import rules

    ledger = book.load(folder / "ledger", rules.parse(RULES))
    new = book.new_entries(positions.entries(snap, ledger.entries, ledger.accounts, "test"), ledger)
    book.add(ledger, new)


def test_without_cost_basis_new_units_cost_todays_price(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0  # FFTGX: 100.123 units cost 1500.00
    # A week later SnapTrade, with no cost basis: 10 more units, price now 16.00.
    add_snapshot(data_dir, holdings_snapshot(datetime.date(2026, 3, 27), "110.123", "16.00", None))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("110.123"),
        Decimal("1660.00"),
    )
    # The same holdings again, at another price: nothing to record but the price.
    add_snapshot(data_dir, holdings_snapshot(datetime.date(2026, 4, 3), "110.123", "17.00", None))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("110.123"),
        Decimal("1660.00"),
    )
    # Units sold take their share of the cost.
    add_snapshot(data_dir, holdings_snapshot(datetime.date(2026, 4, 10), "55.0615", "17.00", None))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("55.0615"),
        Decimal("830.00"),
    )
    assert book.check(data_dir / "ledger") == []


def test_only_the_first_snapshot_of_a_day_is_recorded(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    day = datetime.date(2026, 3, 27)
    add_snapshot(data_dir, holdings_snapshot(day, "110.123", "16.00", None))
    # Later the same day the holdings differ: the next day's snapshot records that.
    add_snapshot(data_dir, holdings_snapshot(day, "120.123", "16.00", None))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("110.123")
    assert book.check(data_dir / "ledger") == []
    add_snapshot(data_dir, holdings_snapshot(day.replace(day=28), "120.123", "16.00", None))
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("120.123")
    assert book.check(data_dir / "ledger") == []


def test_an_empty_account_you_dont_track_is_skipped(data_dir: Path) -> None:
    from tally import rules

    ledger = book.load(data_dir / "ledger", rules.parse(RULES))
    snap = Snapshot(datetime.date(2026, 3, 20), [Account("9999", "EMPTY", None, [])])
    assert positions.entries(snap, ledger.entries, ledger.accounts, "test") == []


def test_sync_records_brokerage_holdings(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tally import cli

    assert main(["import", str(POSITIONS)]) == 0
    snap = holdings_snapshot(datetime.date(2026, 3, 27), "110.123", "16.00", None)
    monkeypatch.setattr(cli, "_brokerage", lambda: lambda: snap)
    capsys.readouterr()
    assert main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Fidelity holdings from SnapTrade (would add" in out
    assert "EXAMPLE RET SAVINGS …2222: cost 0.00 (no cost basis given), market 1,761.97" in out
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("100.123")
    assert main(["sync"]) == 0
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("110.123")


def test_a_brokerage_failure_doesnt_stop_the_sync(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tally import cli, snaptrade

    def fail() -> Snapshot:
        raise snaptrade.SnapTradeError("couldn't reach SnapTrade: ConnectionError")

    monkeypatch.setattr(cli, "_brokerage", lambda: fail)
    capsys.readouterr()
    assert main(["sync"]) == 0
    assert "Fidelity holdings not updated: couldn't reach SnapTrade" in capsys.readouterr().err


def roth_snapshot(
    day: datetime.date, cash: str, dividends: list[tuple[datetime.date, str]]
) -> Snapshot:
    holdings = [
        Holding("FXAIX", Decimal("10.5"), Decimal("200.00"), Decimal("2000.00")),
        Holding("ABCD", Decimal("3"), Decimal("150.25"), Decimal("500.00")),
    ]
    return Snapshot(
        day,
        [
            Account(
                "1111",
                "ROTH IRA",
                Decimal(cash),
                holdings,
                tuple((d, Decimal(a)) for d, a in dividends),
            )
        ],
    )


def roth_entries(folder: Path) -> list[data.Transaction]:
    return [
        e
        for e in ledger_entries(folder)
        if isinstance(e, data.Transaction) and e.meta.get("fidelity-account") == "1111"
    ]


def test_dividends_explain_a_cash_increase(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0  # Roth cash 12.50 on 03/20
    # A week later: a 2.50 dividend paid on 03/25 is all that changed; one from before the
    # last snapshot doesn't count again.
    snap = roth_snapshot(
        datetime.date(2026, 3, 27),
        "15.00",
        [(datetime.date(2026, 3, 10), "9.99"), (datetime.date(2026, 3, 25), "2.50")],
    )
    add_snapshot(data_dir, snap)
    later = roth_entries(data_dir)[-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Income:Fidelity:Dividends"] == Decimal("-2.50")
    assert sides.get("Income:Fidelity:Unreconciled", Decimal(0)) == 0
    assert later.flag == "*"  # nothing left unexplained
    assert book.check(data_dir / "ledger") == []


def test_what_dividends_dont_explain_stays_flagged(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    # Cash went up 7.50, but only 2.50 of it was a dividend.
    snap = roth_snapshot(
        datetime.date(2026, 3, 27), "20.00", [(datetime.date(2026, 3, 25), "2.50")]
    )
    add_snapshot(data_dir, snap)
    later = roth_entries(data_dir)[-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Income:Fidelity:Dividends"] == Decimal("-2.50")
    assert sides["Income:Fidelity:Unreconciled"] == Decimal("-5.00")
    assert later.flag == "!"


def test_without_a_dividends_account_everything_stays_unreconciled(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_file = data_dir / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text().replace("2026-01-01 open Income:Fidelity:Dividends\n", "")
    )
    assert main(["import", str(POSITIONS)]) == 0
    snap = roth_snapshot(
        datetime.date(2026, 3, 27), "15.00", [(datetime.date(2026, 3, 25), "2.50")]
    )
    add_snapshot(data_dir, snap)
    later = roth_entries(data_dir)[-1]
    assert all(p.account != "Income:Fidelity:Dividends" for p in later.postings)
    assert book.check(data_dir / "ledger") == []


def test_payroll_contributions_are_income_and_cost_basis(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_file = data_dir / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text().replace(
            '  fidelity-account: "2222"\n',
            '  fidelity-account: "2222"\n  contributions-from: "Income:Salary:401k"\n',
        )
        + "2026-01-01 open Income:Salary:401k USD\n"
    )
    assert main(["import", str(POSITIONS)]) == 0  # FFTGX: 100.123 units cost 1500.00 on 03/20
    # A week later: 155.00 from the 03/24 paycheck bought 10 units. The one from 03/10 was
    # before the last snapshot, so it's already in.
    snap = holdings_snapshot(datetime.date(2026, 3, 27), "110.123", "16.00", None)
    paid = (
        (datetime.date(2026, 3, 10), Decimal("150.00")),
        (datetime.date(2026, 3, 24), Decimal("155.00")),
    )
    snap = Snapshot(snap.date, [replace(snap.accounts[0], contributions=paid)])
    add_snapshot(data_dir, snap)
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("110.123"),
        Decimal("1655.00"),
    )
    later = [
        e
        for e in ledger_entries(data_dir)
        if isinstance(e, data.Transaction) and e.meta.get("fidelity-account") == "2222"
    ][-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Income:Salary:401k"] == Decimal("-155.00")
    assert later.flag == "*"
    assert book.check(data_dir / "ledger") == []


def test_contributions_without_contributions_from_are_unreconciled(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["import", str(POSITIONS)]) == 0
    snap = holdings_snapshot(datetime.date(2026, 3, 27), "110.123", "16.00", None)
    paid = ((datetime.date(2026, 3, 24), Decimal("155.00")),)
    add_snapshot(data_dir, Snapshot(snap.date, [replace(snap.accounts[0], contributions=paid)]))
    # As before: bought at today's price, and the rest unexplained.
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("110.123"),
        Decimal("1660.00"),
    )
    assert book.check(data_dir / "ledger") == []
