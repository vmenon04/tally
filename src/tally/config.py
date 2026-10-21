"""Settings from data/config.toml, and where secrets live."""

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

NTFY_SERVER = "https://ntfy.sh"


class ConfigError(ValueError):
    """config.toml is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    sheet_id: str
    gmail_account: str | None = None  # the mailbox the bank alerts are forwarded to
    ntfy_server: str = NTFY_SERVER
    # This year's Roth IRA limit, and what went in that tally can't see (made before tally, or
    # from another account). The weekly summary checks the plan against the limit.
    roth_limit: Decimal | None = None
    roth_elsewhere: Decimal = Decimal(0)


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    unknown = set(data) - {
        "sheet_id",
        "gmail_account",
        "ntfy_server",
        "roth_limit",
        "roth_elsewhere",
    }
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    sheet_id = data.get("sheet_id")
    if not isinstance(sheet_id, str) or not sheet_id.strip():
        raise ConfigError(f"{path}: sheet_id must be the ID from the Sheet's URL")
    gmail_account = data.get("gmail_account")
    if gmail_account is not None and (
        not isinstance(gmail_account, str) or "@" not in gmail_account.strip()
    ):
        raise ConfigError(f"{path}: gmail_account must be an email address")
    ntfy_server = data.get("ntfy_server", NTFY_SERVER)
    if not isinstance(ntfy_server, str) or not ntfy_server.startswith("https://"):
        raise ConfigError(f"{path}: ntfy_server must be an https:// address")
    roth_limit = _amount(data, "roth_limit", path)
    roth_elsewhere = _amount(data, "roth_elsewhere", path)
    return Config(
        sheet_id=sheet_id.strip(),
        gmail_account=gmail_account.strip() if gmail_account is not None else None,
        ntfy_server=ntfy_server.rstrip("/"),
        roth_limit=roth_limit,
        roth_elsewhere=roth_elsewhere if roth_elsewhere is not None else Decimal(0),
    )


def _amount(data: dict[str, object], key: str, path: Path) -> Decimal | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | Decimal) or value < 0:
        raise ConfigError(f"{path}: {key} must be an amount like 7500 or 1234.00")
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
