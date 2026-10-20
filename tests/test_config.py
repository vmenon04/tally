from pathlib import Path

import pytest

from tally import config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load(tmp_path: Path) -> None:
    assert config.load(write(tmp_path, 'sheet_id = " abc123 "\n')) == config.Config("abc123")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("sheet_id = ", "not valid TOML"),
        ("", "sheet_id must be"),
        ('sheet_id = ""\n', "sheet_id must be"),
        ("sheet_id = 5\n", "sheet_id must be"),
        ('sheet_id = "a"\nsheet = "b"\n', "unknown keys"),
        ('sheet_id = "a"\ngmail_account = "alerts"\n', "gmail_account must be"),
        ('sheet_id = "a"\ngmail_account = 5\n', "gmail_account must be"),
    ],
)
def test_bad_config(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(config.ConfigError, match=error):
        config.load(write(tmp_path, text))


def test_gmail_account(tmp_path: Path) -> None:
    text = 'sheet_id = "abc123"\ngmail_account = " alerts@example.com "\n'
    assert config.load(write(tmp_path, text)).gmail_account == "alerts@example.com"


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load(tmp_path / "config.toml")


def test_secrets_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALLY_SECRETS_DIR", raising=False)
    assert config.secrets_dir() == Path("secrets")
    monkeypatch.setenv("TALLY_SECRETS_DIR", "/srv/tally/secrets")
    assert config.secrets_dir() == Path("/srv/tally/secrets")
