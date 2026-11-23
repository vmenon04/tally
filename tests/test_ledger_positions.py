import datetime
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data, position
from sample import POSITIONS, bofa_rows, record, seed, snapshot
from test_ledger_sync import ACCOUNTS, MAIN, RULES

from tally.cli import main
from tally.importers import ParseError
from tally.importers.fidelity import Account, Holding, Snapshot
from tally.ledger import accounts, book, parity, positions, weekly

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
    seed(folder, bofa_rows())
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
    """Record a snapshot written as a positions export: (0, what was added, "") or (1, "", why
    not)."""
    try:
        added = record(folder, snapshot(text))
    except (ParseError, accounts.AccountsError) as e:
        return 1, "", str(e)
    return 0, f"ledger: {added} new entries", ""


def test_first_snapshot_opens_holdings_at_cost(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0
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


def test_the_same_file_again_adds_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0
    assert record(data_dir, snapshot()) == 0


def test_a_later_snapshot_moves_holdings_to_fidelity_cost_basis(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0
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
    assert record(data_dir, snapshot()) > 0
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
    assert record(data_dir, snapshot()) > 0
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
    assert record(data_dir, snapshot()) > 0  # FFTGX: 100.123 units cost 1500.00
    # A week later, with no cost basis: 10 more units, price now 16.00.
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
    assert record(data_dir, snapshot()) > 0
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


def holdings_from_bank(monkeypatch: pytest.MonkeyPatch, *holdings: dict[str, str]) -> None:
    """SimpleFIN sends these holdings for the 401(k) ending 2222, and an empty account."""
    from tally import cli, simplefin

    def account(name: str, found: list[dict[str, str]]) -> simplefin.Account:
        return simplefin.Account(
            name,
            name,
            "Fidelity",
            "USD",
            Decimal(0),
            datetime.date(2026, 3, 27),
            unread={"holdings": found},
        )

    answer = simplefin.Answer(
        [account("EXAMPLE RET SAVINGS (2222)", list(holdings)), account("EMPTY (9999)", [])], []
    )
    monkeypatch.setattr(cli, "_bank", lambda: lambda: answer)


def test_sync_records_the_holdings_simplefin_sends(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert record(data_dir, snapshot()) > 0
    fund = {"symbol": "FFTGX", "shares": "110.123", "market_value": "1761.97"}
    holdings_from_bank(monkeypatch, {**fund, "purchase_price": "0.00"})
    capsys.readouterr()
    assert main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Fidelity holdings from SimpleFIN (would add" in out
    assert "EXAMPLE RET SAVINGS …2222: cost 0.00 (no cost basis given), market 1,761.97" in out
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("100.123")
    assert main(["sync"]) == 0
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX")[0] == Decimal("110.123")


def test_holdings_simplefin_garbles_dont_stop_the_sync(
    data_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fund = {"symbol": "FFTGX", "shares": "lots", "market_value": "1.00", "purchase_price": "0"}
    holdings_from_bank(monkeypatch, fund)
    capsys.readouterr()
    assert main(["sync"]) == 0
    assert "Fidelity holdings not updated: bad shares 'lots'" in capsys.readouterr().err


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
    assert record(data_dir, snapshot()) > 0  # Roth cash 12.50 on 03/20
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
    assert record(data_dir, snapshot()) > 0
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
    assert record(data_dir, snapshot()) > 0
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
    assert record(data_dir, snapshot()) > 0  # FFTGX: 100.123 units cost 1500.00 on 03/20
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
    assert record(data_dir, snapshot()) > 0
    snap = holdings_snapshot(datetime.date(2026, 3, 27), "110.123", "16.00", None)
    paid = ((datetime.date(2026, 3, 24), Decimal("155.00")),)
    add_snapshot(data_dir, Snapshot(snap.date, [replace(snap.accounts[0], contributions=paid)]))
    # As before: bought at today's price, and the rest unexplained.
    assert held(data_dir, "Assets:Fidelity:401k", "FFTGX") == (
        Decimal("110.123"),
        Decimal("1660.00"),
    )
    assert book.check(data_dir / "ledger") == []


def waiting_in_transfers(folder: Path, day: str, amount: str) -> None:
    """The Roth takes money from Assets:Transfers:Fidelity:Roth, and the bank sent some on day."""
    accounts_file = folder / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text().replace(
            '  fidelity-account: "1111"\n',
            '  fidelity-account: "1111"\n  transfers-from: "Assets:Transfers:Fidelity:Roth"\n',
        )
        + "2026-01-01 open Assets:Transfers:Fidelity:Roth USD\n"
        + f'{day} * "FID BKG SVC LLC"\n'
        + f"  Assets:Transfers:Fidelity:Roth  {amount} USD\n"
        + "  Equity:Opening-Balances\n"
    )


def test_money_from_the_bank_waits_until_fidelity_has_it(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0  # Roth cash 12.50 on 03/20
    waiting_in_transfers(data_dir, "2026-03-23", "1000.00")
    # The bank sent it on 03/23, but Fidelity doesn't have it on 03/24: nothing to record,
    # and the cash balance still holds.
    add_snapshot(data_dir, roth_snapshot(datetime.date(2026, 3, 24), "12.50", []))
    assert len(roth_entries(data_dir)) == 1
    assert book.check(data_dir / "ledger") == []
    # On 03/26 it has: the cash comes from the clearing account, which is back to zero.
    add_snapshot(data_dir, roth_snapshot(datetime.date(2026, 3, 26), "1012.50", []))
    later = roth_entries(data_dir)[-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Assets:Transfers:Fidelity:Roth"] == Decimal("-1000.00")
    assert later.flag == "*"
    assert book.check(data_dir / "ledger") == []


def test_money_from_the_bank_spent_the_day_it_arrives(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0
    waiting_in_transfers(data_dir, "2026-03-23", "1000.00")
    # It arrived and 600.00 of it bought 3 more FXAIX; and only part of a second transfer
    # shows yet, so the rest keeps waiting.
    accounts_file = data_dir / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text()
        + '2026-03-25 * "FID BKG SVC LLC"\n'
        + "  Assets:Transfers:Fidelity:Roth  500.00 USD\n"
        + "  Equity:Opening-Balances\n"
    )
    snap = roth_snapshot(datetime.date(2026, 3, 26), "512.50", [])
    holdings = [
        Holding("FXAIX", Decimal("13.5"), Decimal("200.00"), Decimal("2600.00")),
        *snap.accounts[0].holdings[1:],
    ]
    add_snapshot(data_dir, Snapshot(snap.date, [replace(snap.accounts[0], holdings=holdings)]))
    later = roth_entries(data_dir)[-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Assets:Transfers:Fidelity:Roth"] == Decimal("-1100.00")
    assert later.flag == "*"
    assert book.check(data_dir / "ledger") == []


def test_roth_contributions_count_what_waits_in_the_clearing_account(data_dir: Path) -> None:
    waiting_in_transfers(data_dir, "2026-03-23", "1000.00")
    found = weekly.build(data_dir / "ledger", datetime.date(2026, 3, 31), Decimal(7500), Decimal(0))
    assert found.roth_in == Decimal("1500.00")  # the 500 straight to cash on 03/10, and this


def test_rows_from_before_the_clearing_account_keep_their_category() -> None:
    from beancount import loader

    loaded, errors, _ = loader.load_string(
        "2026-01-01 open Assets:Fidelity:Roth\n"
        '  fidelity-account: "1111"\n'
        '  transfers-from: "Assets:Transfers:Fidelity:Roth"\n'
        "2026-01-01 open Assets:Fidelity:Roth:Cash USD\n"
        "2026-01-01 open Assets:Fidelity:Go\n"
        '  fidelity-account: "3333"\n'
        "2026-01-01 open Assets:Fidelity:Go:Cash USD\n"
        "2026-01-01 open Assets:Transfers:Fidelity:Roth USD\n"
        '  category: "Investing"\n'
    )
    assert errors == []
    found = accounts.from_entries(loaded)
    assert found.category_of("Assets:Fidelity:Roth:Cash") == "Investing"
    assert found.category_of("Assets:Fidelity:Go:Cash") is None
    assert found.counter("Investing", "FID BKG SVC LLC") == "Assets:Transfers:Fidelity:Roth"


def test_money_to_the_bank_leaves_when_fidelity_shows_it_gone(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record(data_dir, snapshot()) > 0  # Roth cash 12.50 on 03/20
    # 500.00 went to the Roth on 03/23; 200.00 came back to the bank on 03/30.
    waiting_in_transfers(data_dir, "2026-03-23", "500.00")
    add_snapshot(data_dir, roth_snapshot(datetime.date(2026, 3, 24), "512.50", []))
    accounts_file = data_dir / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text()
        + '2026-03-30 * "FID BKG SVC LLC"\n'
        + "  Assets:Transfers:Fidelity:Roth  -200.00 USD\n"
        + "  Equity:Opening-Balances\n"
    )
    # Fidelity's holdings still show it on 03/31, and it's gone on 04/02.
    add_snapshot(data_dir, roth_snapshot(datetime.date(2026, 3, 31), "512.50", []))
    add_snapshot(data_dir, roth_snapshot(datetime.date(2026, 4, 2), "312.50", []))
    later = roth_entries(data_dir)[-1]
    sides = {
        p.account: p.units.number for p in later.postings if p.units and p.units.currency == "USD"
    }
    assert sides["Assets:Transfers:Fidelity:Roth"] == Decimal("200.00")
    assert later.flag == "*"
    assert book.check(data_dir / "ledger") == []


def test_transfers_wait_for_the_fidelity_account_the_bank_names() -> None:
    from beancount import loader

    loaded, errors, _ = loader.load_string(
        "2026-01-01 open Assets:Fidelity:Roth\n"
        '  fidelity-account: "1111"\n'
        '  transfers-from: "Assets:Transfers:Fidelity:Roth"\n'
        "2026-01-01 open Assets:Fidelity:Roth:Cash USD\n"
        "2026-01-01 open Assets:Fidelity:Go\n"
        '  fidelity-account: "3333"\n'
        '  transfers-from: "Assets:Transfers:Fidelity:Go"\n'
        "2026-01-01 open Assets:Fidelity:Go:Cash USD\n"
        "2026-01-01 open Assets:Transfers:Fidelity:Roth USD\n"
        '  category: "Investing"\n'
        '  merchant-contains: "1111"\n'
        "2026-01-01 open Assets:Transfers:Fidelity:Go USD\n"
        '  category: "Investing"\n'
        '  merchant-contains: "3333"\n'
        "2026-01-01 open Assets:Transfers:Fidelity USD\n"
        '  category: "Investing"\n'
    )
    assert errors == []
    found = accounts.from_entries(loaded)
    bank = "FID BKG SVC LLC DES:MONEYLINE ID:XXXXX{} K56FX INDN:A PERSON CO ID:XXXXX4600 PPD"
    merchant = "FID BKG SVC LLC"
    assert found.counter("Investing", merchant, bank.format("1111")) == (
        "Assets:Transfers:Fidelity:Roth"
    )
    assert found.counter("Investing", merchant, bank.format("3333")) == (
        "Assets:Transfers:Fidelity:Go"
    )
    # An account tally doesn't know waits where nothing clears it, and nothing counts it.
    assert found.counter("Investing", merchant, bank.format("9999")) == "Assets:Transfers:Fidelity"
    assert found.roth_from("Assets:Fidelity:Roth") == {
        "Assets:Fidelity:Roth:Cash",
        "Assets:Transfers:Fidelity:Roth",
    }
    # Each cash account's old rows take its own clearing account's category.
    assert found.category_of("Assets:Fidelity:Go:Cash") == "Investing"


# Scenarios for money moving in and out of Fidelity accounts, each played out to the end and
# held to the same promises: bean-check is clean, every clearing account is back to zero (or
# reported if it isn't), Unreconciled holds only what nothing explains, and the Roth IRA total
# counts each contribution from the bank once.


def with_go_clearing(folder: Path) -> None:
    """Roth and Go each with their own clearing account; any other goes to the catch-all."""
    accounts_file = folder / "ledger" / "accounts.beancount"
    accounts_file.write_text(
        accounts_file.read_text()
        .replace(
            '  fidelity-account: "1111"\n',
            '  fidelity-account: "1111"\n  transfers-from: "Assets:Transfers:Fidelity:Roth"\n',
        )
        .replace(
            '  fidelity-account: "3333"\n',
            '  fidelity-account: "3333"\n  transfers-from: "Assets:Transfers:Fidelity:Go"\n',
        )
        + "2026-01-01 open Assets:Transfers:Fidelity:Roth USD\n"
        + "2026-01-01 open Assets:Transfers:Fidelity:Go USD\n"
        + "2026-01-01 open Assets:Transfers:Fidelity USD\n"
    )


def bank_sends(folder: Path, day: str, account: str, amount: str, pending: bool = False) -> None:
    """A transfer out of the bank, posted (or still pending) on day."""
    path = folder / "ledger" / ("pending.beancount" if pending else "imported/2026.beancount")
    flag = "!" if pending else "*"
    path.write_text(
        (path.read_text() if path.exists() else "")
        + f'\n{day} {flag} "FID BKG SVC LLC"\n'
        + f"  Assets:BofA:Checking  -{amount} USD\n"
        + f"  {account}  {amount} USD\n"
    )


def both(day: datetime.date, roth_cash: str, go_cash: str) -> Snapshot:
    """Roth and Go holdings as the sample has them, with this much cash in each."""
    roth = roth_snapshot(day, roth_cash, []).accounts[0]
    go = Account(
        "3333",
        "FIDELITY GO",
        Decimal(go_cash),
        [Holding("FDFIX", Decimal("30"), Decimal("30.00"), Decimal("750.00"))],
    )
    return Snapshot(day, [roth, go])


def balance_of(folder: Path, account: str) -> Decimal:
    return sum(
        (
            p.units.number or Decimal(0)
            for e in ledger_entries(folder)
            if isinstance(e, data.Transaction)
            for p in e.postings
            if p.account == account and p.units is not None and p.units.currency == "USD"
        ),
        Decimal(0),
    )


def stuck(folder: Path, today: datetime.date) -> list[str]:
    from tally import rules

    ledger = book.load(folder / "ledger", rules.parse(RULES))
    return [account for account, _, _ in parity.stuck_transfers(ledger, today)]


ROTH_CLEARING = "Assets:Transfers:Fidelity:Roth"
GO_CLEARING = "Assets:Transfers:Fidelity:Go"


def test_scenario_roth_and_go_each_clear_their_own(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    assert record(data_dir, both(datetime.date(2026, 3, 20), "12.50", "5.00")) > 0
    bank_sends(data_dir, "2026-03-23", ROTH_CLEARING, "1000.00")
    bank_sends(data_dir, "2026-03-23", GO_CLEARING, "200.00")
    # Fidelity's holdings lag: nothing on 03/24, Go's on 03/25, the Roth's on 03/26.
    add_snapshot(data_dir, both(datetime.date(2026, 3, 24), "12.50", "5.00"))
    add_snapshot(data_dir, both(datetime.date(2026, 3, 25), "12.50", "205.00"))
    add_snapshot(data_dir, both(datetime.date(2026, 3, 26), "1012.50", "205.00"))
    assert book.check(data_dir / "ledger") == []
    assert balance_of(data_dir, ROTH_CLEARING) == 0
    assert balance_of(data_dir, GO_CLEARING) == 0
    assert balance_of(data_dir, "Income:Fidelity:Unreconciled") == 0
    found = weekly.build(data_dir / "ledger", datetime.date(2026, 3, 31), Decimal(7500), Decimal(0))
    assert found.roth_in == Decimal("1500.00")  # 500 from the sample's BofA rows, and 1,000
    assert stuck(data_dir, datetime.date(2026, 4, 30)) == []


def test_scenario_a_gain_while_money_waits_ends_up_unreconciled(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    assert record(data_dir, both(datetime.date(2026, 3, 20), "12.50", "5.00")) > 0
    bank_sends(data_dir, "2026-03-23", ROTH_CLEARING, "1000.00")
    # 50.00 nobody explains shows up first; the transfer two days later.
    add_snapshot(data_dir, both(datetime.date(2026, 3, 24), "62.50", "5.00"))
    add_snapshot(data_dir, both(datetime.date(2026, 3, 26), "1062.50", "5.00"))
    assert book.check(data_dir / "ledger") == []
    assert balance_of(data_dir, ROTH_CLEARING) == 0
    assert balance_of(data_dir, "Income:Fidelity:Unreconciled") == Decimal("-50.00")


def test_scenario_a_pending_transfer_is_never_taken_as_arrived(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    assert record(data_dir, both(datetime.date(2026, 3, 20), "12.50", "5.00")) > 0
    bank_sends(data_dir, "2026-03-23", ROTH_CLEARING, "1000.00", pending=True)
    add_snapshot(data_dir, both(datetime.date(2026, 3, 24), "1012.50", "5.00"))
    later = roth_entries(data_dir)[-1]
    assert all(p.account != ROTH_CLEARING for p in later.postings)
    assert later.flag == "!"
    assert book.check(data_dir / "ledger") == []


def test_scenario_between_two_fidelity_accounts(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    assert record(data_dir, both(datetime.date(2026, 3, 20), "12.50", "405.00")) > 0
    # 400.00 from Go to the Roth, inside Fidelity: no bank row. Both snapshots book it to
    # Unreconciled, flagged, and the two cancel out.
    add_snapshot(data_dir, both(datetime.date(2026, 3, 24), "412.50", "5.00"))
    assert book.check(data_dir / "ledger") == []
    assert balance_of(data_dir, "Income:Fidelity:Unreconciled") == 0
    assert all(e.flag == "!" for e in roth_entries(data_dir)[1:])


def test_scenario_a_transfer_whose_other_side_never_comes_is_reported(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    assert record(data_dir, both(datetime.date(2026, 3, 20), "12.50", "5.00")) > 0
    bank_sends(data_dir, "2026-03-23", "Assets:Transfers:Fidelity", "300.00")  # unknown account
    bank_sends(data_dir, "2026-03-23", ROTH_CLEARING, "100.00")  # holdings never show it
    assert stuck(data_dir, datetime.date(2026, 3, 29)) == []  # not a week yet
    assert stuck(data_dir, datetime.date(2026, 3, 30)) == [
        "Assets:Transfers:Fidelity",
        ROTH_CLEARING,
    ]


def test_scenario_money_sent_before_an_accounts_first_snapshot(data_dir: Path) -> None:
    with_go_clearing(data_dir)
    bank_sends(data_dir, "2026-03-18", ROTH_CLEARING, "1000.00")
    assert record(data_dir, both(datetime.date(2026, 3, 20), "1012.50", "5.00")) > 0
    assert balance_of(data_dir, ROTH_CLEARING) == 0
    assert book.check(data_dir / "ledger") == []
