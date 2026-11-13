from pathlib import Path
from typing import Any

import pytest
import requests

from tally import sure
from tally.cli import main

URL = "http://127.0.0.1:3200"


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self.ok = status < 400
        self.content = b"x"
        self.text = str(body)
        self._body = body

    def json(self) -> Any:
        return self._body


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A Sure with three accounts, two per page, that accepts only the key "good"."""
    calls: list[dict[str, Any]] = []
    names = ["Checking", "Card", "Roth IRA"]

    def request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        if kwargs["headers"]["X-Api-Key"] != "good":
            return FakeResponse(401, {"error": "unauthorized"})
        page = int(kwargs["params"]["page"])
        chunk = names[(page - 1) * 2 : page * 2]
        return FakeResponse(
            200, {"accounts": [{"name": n} for n in chunk], "pagination": {"total_pages": 2}}
        )

    monkeypatch.setattr(requests, "request", request)
    return calls


def test_account_names_follow_the_pages(served: list[dict[str, Any]]) -> None:
    assert sure.account_names(sure.Client(URL, "good")) == ["Checking", "Card", "Roth IRA"]
    assert [call["url"] for call in served] == [f"{URL}/api/v1/accounts"] * 2
    assert [call["params"]["page"] for call in served] == ["1", "2"]


def test_a_refused_key_is_reported(served: list[dict[str, Any]]) -> None:
    with pytest.raises(sure.SureError, match="didn't accept the API key"):
        sure.account_names(sure.Client(URL, "bad"))


def test_an_unreachable_sure_is_reported_without_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(method: str, url: str, **kwargs: Any) -> Any:
        raise requests.ConnectionError("secret-looking details")

    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(sure.SureError, match="couldn't reach Sure") as caught:
        sure.account_names(sure.Client(URL, "good"))
    assert "secret-looking" not in str(caught.value)


def test_a_missing_key_says_how_to_add_one(tmp_path: Path) -> None:
    with pytest.raises(sure.SureError, match="tally sure-login"):
        sure.Client.from_secrets(URL, tmp_path)


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.toml").write_text("")
    monkeypatch.setenv("TALLY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path / "secrets"))
    return tmp_path


def test_login_saves_a_key_sure_accepts(
    dirs: Path,
    served: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: " good\n")
    assert main(["sure-login"]) == 0
    saved = dirs / "secrets" / "sure-api-key"
    assert saved.read_text() == "good\n"
    assert saved.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "3 account(s): Checking, Card, Roth IRA" in out
    assert sure.Client.from_secrets(URL, dirs / "secrets").api_key == "good"


def test_login_saves_nothing_when_sure_refuses(
    dirs: Path,
    served: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "bad")
    assert main(["sure-login"]) == 1
    assert "didn't accept the API key" in capsys.readouterr().err
    assert not (dirs / "secrets").exists()
