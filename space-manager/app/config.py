"""Configuration loading for iCloud Space Manager.

Accounts are declared in a TOML file (see accounts.example.toml). Paths and
behaviour toggles come from environment variables so the container stays
12-factor:

    SPACE_MANAGER_CONFIG       path to accounts.toml   (default /data/accounts.toml)
    SPACE_MANAGER_DB           path to sqlite db       (default /data/space-manager.db)
    SPACE_MANAGER_DRY_RUN      "true"/"false"          (default true — must be
                               explicitly disabled before anything is deleted)
    SPACE_MANAGER_UI_PASSWORD  optional; when set, the web UI requires HTTP
                               basic auth with this password (user: admin)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    name: str
    apple_id: str
    backup_dirs: tuple[str, ...]
    cookie_directory: str
    domain: str = "com"


@dataclass(frozen=True)
class Settings:
    accounts: tuple[Account, ...]
    db_path: str
    dry_run: bool
    ui_password: str | None = None

    def account(self, name: str) -> Account:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        raise KeyError(name)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings(config_path: str | None = None) -> Settings:
    path = config_path or os.environ.get("SPACE_MANAGER_CONFIG", "/data/accounts.toml")
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    accounts = []
    for entry in raw.get("accounts", []):
        accounts.append(
            Account(
                name=entry["name"],
                apple_id=entry["apple_id"],
                backup_dirs=tuple(entry["backup_dirs"]),
                cookie_directory=entry.get(
                    "cookie_directory", f"/data/cookies/{entry['name']}"
                ),
                domain=entry.get("domain", "com"),
            )
        )
    if not accounts:
        raise ValueError(f"No [[accounts]] entries found in {path}")
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise ValueError("Account names must be unique")

    return Settings(
        accounts=tuple(accounts),
        db_path=os.environ.get("SPACE_MANAGER_DB", "/data/space-manager.db"),
        dry_run=_env_bool("SPACE_MANAGER_DRY_RUN", True),
        ui_password=os.environ.get("SPACE_MANAGER_UI_PASSWORD") or None,
    )
