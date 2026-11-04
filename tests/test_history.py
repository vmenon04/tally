import base64
from pathlib import Path

import pytest

from tally.ledger import history


def test_token_goes_in_an_environment_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    (tmp_path / "forgejo-token").write_text("made-up-token\n")
    env = history._auth_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    header = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(header) == b"tally:made-up-token"


def test_no_token_file_pushes_without_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALLY_SECRETS_DIR", str(tmp_path))
    assert history._auth_env() == {}
