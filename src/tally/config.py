"""Settings from data/config.toml, and where secrets live."""

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

NTFY_SERVER = "https://ntfy.sh"
SURE_URL = "http://127.0.0.1:3200"  # deploy/sure/compose.yml


class ConfigError(ValueError):
    """config.toml is missing or malformed."""


# Keys an older tally used, accepted and ignored so an old config.toml still loads.


@dataclass(frozen=True, slots=True)
class Config:
    gmail_account: str | None = None  # the Gmail address the weekly summary is sent from
    summary_to: str | None = None  # where `tally summary --email` sends the weekly summary
    ntfy_server: str = NTFY_SERVER
    # This year's Roth IRA limit, and what went in that tally can't see (made before tally, or
    # from another account). The weekly summary checks the plan against the limit.
    roth_limit: Decimal | None = None
    roth_elsewhere: Decimal = Decimal(0)
    sure_url: str = SURE_URL  # where Sure listens


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    unknown = set(data) - {
        "gmail_account",
        "summary_to",
        "ntfy_server",
        "roth_limit",
        "roth_elsewhere",
        "sure_url",
    }
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    gmail_account = _address(data, "gmail_account", path)
    summary_to = _address(data, "summary_to", path)
    ntfy_server = data.get("ntfy_server", NTFY_SERVER)
    if not isinstance(ntfy_server, str) or not ntfy_server.startswith("https://"):
        raise ConfigError(f"{path}: ntfy_server must be an https:// address")
    sure_url = data.get("sure_url", SURE_URL)
    if not isinstance(sure_url, str) or not sure_url.startswith(("http://", "https://")):
        raise ConfigError(f"{path}: sure_url must be an http:// or https:// address")
    roth_limit = _amount(data, "roth_limit", path)
    roth_elsewhere = _amount(data, "roth_elsewhere", path)
    return Config(
        gmail_account=gmail_account,
        summary_to=summary_to,
        ntfy_server=ntfy_server.rstrip("/"),
        roth_limit=roth_limit,
        roth_elsewhere=roth_elsewhere if roth_elsewhere is not None else Decimal(0),
        sure_url=sure_url.rstrip("/"),
    )


def _address(data: dict[str, object], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or "@" not in value.strip():
        raise ConfigError(f"{path}: {key} must be an email address")
    return value.strip()


def _amount(data: dict[str, object], key: str, path: Path) -> Decimal | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | Decimal) or value < 0:
        raise ConfigError(f"{path}: {key} must be an amount like 7500 or 1200.00")
    return Decimal(value)


def secrets_dir() -> Path:
    """Where API keys live: TALLY_SECRETS_DIR, else ./secrets."""
    return Path(os.environ.get("TALLY_SECRETS_DIR", "secrets"))


def write_secret(path: Path, text: str) -> None:
    """Write a secret in one step, readable only by you, in a folder only you can open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def private_dir(data_dir: Path, *parts: str) -> Path:
    """data_dir/<parts>, with data_dir and each part created if needed and owner-only (700).

    Folders that already exist are tightened too. mkdir's mode alone isn't enough: it is
    reduced by the umask and isn't applied to parents created along the way.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    path = data_dir
    for part in parts:
        path = path / part
        path.mkdir(exist_ok=True)
        path.chmod(0o700)
    return path
