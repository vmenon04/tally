"""Settings from data/config.toml, and where secrets live."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """config.toml is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    sheet_id: str
    gmail_account: str | None = None  # the mailbox the bank alerts are forwarded to


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    unknown = set(data) - {"sheet_id", "gmail_account"}
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
    return Config(
        sheet_id=sheet_id.strip(),
        gmail_account=gmail_account.strip() if gmail_account is not None else None,
    )


def secrets_dir() -> Path:
    """Where API keys live: TALLY_SECRETS_DIR, else ./secrets."""
    return Path(os.environ.get("TALLY_SECRETS_DIR", "secrets"))
