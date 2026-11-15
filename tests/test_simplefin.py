import base64
import datetime
from decimal import Decimal
from typing import Any

import pytest
import requests

from tally import simplefin

CLAIM = "https://bridge.example/simplefin/claim/abc"
TOKEN = base64.b64encode(CLAIM.encode()).decode()
ACCESS = "https://user:secret@bridge.example/simplefin"
DAY = 1794657600  # noon on 2026-11-14, in UTC

BODY = {
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
