from decimal import Decimal
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
        ('sheet_id = "a"\nntfy_server = "http://ntfy.example.com"\n', "must be an https"),
        ('sheet_id = "a"\nroth_limit = "7500"\n', "roth_limit must be an amount"),
        ('sheet_id = "a"\nroth_elsewhere = -1\n', "roth_elsewhere must be an amount"),
        ('sheet_id = "a"\nroth_limit = true\n', "roth_limit must be an amount"),
    ],
)
def test_bad_config(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(config.ConfigError, match=error):
        config.load(write(tmp_path, text))


def test_gmail_account(tmp_path: Path) -> None:
    text = 'sheet_id = "abc123"\ngmail_account = " alerts@example.com "\n'
    assert config.load(write(tmp_path, text)).gmail_account == "alerts@example.com"


def test_ntfy_server(tmp_path: Path) -> None:
    assert config.load(write(tmp_path, 'sheet_id = "a"\n')).ntfy_server == "https://ntfy.sh"
    text = 'sheet_id = "a"\nntfy_server = "https://ntfy.example.com/"\n'
    assert config.load(write(tmp_path, text)).ntfy_server == "https://ntfy.example.com"


def test_secret_is_written_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "thing"
    config.write_secret(path, "old")
    config.write_secret(path, "new")
    assert path.read_text() == "new"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["thing"]


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="no config file"):
        config.load(tmp_path / "config.toml")


def test_secrets_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALLY_SECRETS_DIR", raising=False)
    assert config.secrets_dir() == Path("secrets")
    monkeypatch.setenv("TALLY_SECRETS_DIR", "/srv/tally/secrets")
    assert config.secrets_dir() == Path("/srv/tally/secrets")


def test_roth_settings(tmp_path: Path) -> None:
    plain = config.load(write(tmp_path, 'sheet_id = "a"\n'))
    assert (plain.roth_limit, plain.roth_elsewhere) == (None, Decimal(0))
    loaded = config.load(
        write(tmp_path, 'sheet_id = "a"\nroth_limit = 7500\nroth_elsewhere = 1234.50\n')
    )
    assert (loaded.roth_limit, loaded.roth_elsewhere) == (Decimal(7500), Decimal("1234.50"))
