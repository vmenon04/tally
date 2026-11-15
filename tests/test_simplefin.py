import base64
import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from tally import simplefin
from tally.cli import main

CLAIM = "https://bridge.example/simplefin/claim/abc"
TOKEN = base64.b64encode(CLAIM.encode()).decode()
ACCESS = "https://user:secret@bridge.example/simplefin"
DAY = 1794657600  # noon on 2026-11-14, in UTC

BODY: dict[str, Any] = {
    "errlist": [{"code": "con.auth", "msg": "Capital One needs you to sign in again"}],
    "connections": [{"conn_id": "c1", "name": "Discover Card", "org_id": "o", "sfin_url": "x"}],
    "accounts": [
        {
            "id": "a1",
            "name": "Discover It",
            "conn_id": "c1",
            "currency": "USD",
            "balance": "-74.20",
            "balance-date": DAY,
            "available-balance": "925.80",
            "transactions": [
                {"id": "t1", "posted": DAY - 86400, "amount": "-5.90", "description": " MAPLE "},
                {
                    "id": "t2",
                    "posted": 0,
                    "transacted_at": DAY,
                    "amount": "-253.77",
                    "description": "OAK BAKERY",
                    "pending": True,
                },
            ],
        }
    ],
}


def response(status: int, body: Any = None, text: str = "") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = (text if body is None else __import__("json").dumps(body)).encode()
    return r


def test_claim_posts_to_the_tokens_url_and_returns_the_access() -> None:
    posted: list[str] = []

    def post(url: str) -> requests.Response:
        posted.append(url)
        return response(200, text=ACCESS + "\n")

    assert simplefin.claim(f" {TOKEN}\n", post) == ACCESS
    assert posted == [CLAIM]


def test_claim_refuses_a_bad_token_and_warns_about_a_claimed_one() -> None:
    with pytest.raises(simplefin.SimpleFINError, match="isn't a SimpleFIN setup token"):
        simplefin.claim("not base64!")
    plain = base64.b64encode(b"http://bridge.example/claim").decode()
    with pytest.raises(simplefin.SimpleFINError, match="https://"):
        simplefin.claim(plain)
    with pytest.raises(simplefin.SimpleFINError, match="someone else may have"):
        simplefin.claim(TOKEN, lambda url: response(403))


def test_fetch_asks_with_the_credentials_apart_and_reads_the_answer() -> None:
    asked: list[tuple[str, dict[str, str], tuple[str, str]]] = []

    def get(url: str, params: dict[str, str], auth: tuple[str, str]) -> requests.Response:
        asked.append((url, params, auth))
        return response(200, BODY)

    found = simplefin.fetch(ACCESS, datetime.date(2026, 10, 15), get=get)
    url, params, auth = asked[0]
    assert url == "https://bridge.example/simplefin/accounts"  # no credentials in the URL
    assert auth == ("user", "secret")
    assert params == {"version": "2", "pending": "1", "start-date": "1792022400"}
    account = found.accounts[0]
    assert (account.bank, account.name, account.balance) == (
        "Discover Card",
        "Discover It",
        Decimal("-74.20"),
    )
    assert account.balance_date == datetime.date(2026, 11, 14)
    posted, pending = account.transactions
    assert (posted.posted, posted.amount, posted.description, posted.pending) == (
        datetime.date(2026, 11, 13),
        Decimal("-5.90"),
        "MAPLE",
        False,
    )
    assert (pending.posted, pending.transacted, pending.pending) == (
        None,
        datetime.date(2026, 11, 14),
        True,
    )
    assert found.problems == ["Capital One needs you to sign in again"]
    assert account.unread == {"available-balance": "925.80"}  # kept as the bridge sent it


@pytest.mark.parametrize(
    ("status", "message"), [(402, "wants payment"), (403, "refused the saved access")]
)
def test_fetch_says_what_the_bridge_refused(status: int, message: str) -> None:
    with pytest.raises(simplefin.SimpleFINError, match=message) as e:
        simplefin.fetch(ACCESS, get=lambda u, p, a: response(status))
    assert "secret" not in str(e.value)


def test_overview_names_each_account_without_the_access() -> None:
    found = simplefin.fetch(ACCESS, get=lambda u, p, a: response(200, BODY))
    assert simplefin.overview(found, datetime.date(2026, 11, 14)) == [
        "Discover Card / Discover It: balance -74.20 USD (today); 2 transaction(s), 1 pending, "
        "2026-11-13 to 2026-11-14",
        "The bridge says: Capital One needs you to sign in again",
    ]


def test_transactions_lists_one_accounts_rows_newest_first() -> None:
    found = simplefin.fetch(ACCESS, get=lambda u, p, a: response(200, BODY))
    assert simplefin.transactions(found, "1234") is None
    assert simplefin.transactions(found, " It") == [
        "pending  bought 2026-11-14    -253.77  OAK BAKERY",
        "2026-11-13  bought ?      -5.90  MAPLE",
    ]


def test_rows_are_a_linked_accounts_posted_rows_as_its_export_gives_them() -> None:
    bofa_account = {
        "id": "a2",
        "name": "Adv SafeBalance Banking (4321)",
        "conn_id": "c1",
        "currency": "USD",
        "balance": "10.00",
        "balance-date": DAY,
        "transactions": [
            {
                "id": "b1",
                "posted": DAY,
                "amount": "-13.50",
                "description": "Maple Gas  11/12 #XXXXX2887 WITHDRWL   Maple   SPRINGFIELD  IL",
            }
        ],
    }
    body = {**BODY, "accounts": [*BODY["accounts"], bofa_account]}
    found = simplefin.fetch(ACCESS, get=lambda u, p, a: response(200, body))
    rows = simplefin.rows(found, {"4321": "bofa_checking", " It": "discover"})
    (checking,) = rows["bofa_checking"]
    assert checking.description == "Maple Gas 11/12 #XXXXX2887 WITHDRWL Maple SPRINGFIELD IL"
    assert checking.merchant == "Maple Gas WITHDRWL Maple SPRINGFIELD IL"
    assert (checking.date, checking.posted) == (
        datetime.date(2026, 11, 12),
        datetime.date(2026, 11, 14),
    )
    (card,) = rows["discover"]  # the pending one is left to the alerts for now
    assert (card.date, card.posted, card.amount, card.merchant) == (
        datetime.date(2026, 11, 13),
        datetime.date(2026, 11, 13),
        Decimal("-5.90"),
        "MAPLE",
    )
    with pytest.raises(simplefin.SimpleFINError, match="no one account ending in 9999"):
        simplefin.rows(found, {"9999": "discover"})


def test_the_extra_option_shows_what_tally_doesnt_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    (tmp_path / simplefin.ACCESS_FILE).write_text(ACCESS + "\n")
    monkeypatch.setattr(
        simplefin, "fetch", lambda *a, **k: simplefin._answer(json.loads(json.dumps(BODY)))
    )
    assert main(["simplefin", "--extra", " It"]) == 0
    assert json.loads(capsys.readouterr().out) == {"available-balance": "925.80"}
    assert main(["simplefin", "--extra", "9999"]) == 1
    assert "no account ending in 9999" in capsys.readouterr().err


def test_snapshot_reads_holdings_cash_and_what_was_paid_in() -> None:
    def holding(symbol: str, shares: str, value: str, paid: str) -> dict[str, str]:
        return {"symbol": symbol, "shares": shares, "market_value": value, "purchase_price": paid}

    def row(day: int, amount: str, description: str) -> simplefin.Transaction:
        posted = datetime.date(2026, 11, day)
        return simplefin.Transaction(str(day), posted, posted, Decimal(amount), description, False)

    roth = simplefin.Account(
        "r",
        "ROTH IRA (8434)",
        "Fidelity",
        "USD",
        Decimal(0),
        datetime.date(2026, 11, 15),
        [
            row(3, "1.45", "DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)"),
            row(3, "-1.45", "REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)"),
            row(4, "500.00", "CASH CONTRIBUTION CURRENT YEAR"),
            row(5, "169.92", "REDEMPTION FROM CORE ACCOUNT"),
        ],
        unread={
            "holdings": [
                holding("FXAIX", "20.252", "5325.87", "239.6889"),
                holding("SPAXX", "9.75", "9.75", "0.00"),
                holding("FFPKX", "272.192", "4619.10", "0.00"),  # a 401(k)'s: no cost
                holding("GONE", "0", "0", "0"),
            ]
        },
    )
    checking = simplefin.Account(
        "c", "Checking (0925)", "BofA", "USD", Decimal(0), roth.balance_date
    )
    snap = simplefin.snapshot(simplefin.Answer([checking, roth], []), datetime.date(2026, 11, 15))
    (account,) = snap.accounts  # the bank account sends no holdings
    assert (account.label, account.name, account.cash) == ("8434", "ROTH IRA", Decimal("9.75"))
    assert [(h.symbol, h.quantity, h.price, h.cost) for h in account.holdings] == [
        ("FXAIX", Decimal("20.252"), Decimal("262.9800"), Decimal("4854.18")),
        ("FFPKX", Decimal("272.192"), Decimal("16.9700"), None),
    ]
    assert account.dividends == ((datetime.date(2026, 11, 3), Decimal("1.45")),)
    assert account.contributions == ((datetime.date(2026, 11, 4), Decimal("500.00")),)
