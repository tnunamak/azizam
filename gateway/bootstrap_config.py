"""Shared bootstrap YAML and admin-store path resolution for gateway processes."""

from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway.compat import get_env


ROOT = Path(__file__).resolve().parents[1]


def bootstrap_config_path() -> Path:
    raw = get_env("AZIZAM_CONFIG_PATH")
    return Path(raw) if raw else ROOT / "proxy_config.yml"


def load_bootstrap_config(path: Path) -> dict:
    if path.exists():
        with path.open() as source:
            return yaml.safe_load(source)
    return {}


def configured_admin_db_path(admin_config: Mapping[str, Any] | None = None) -> Path:
    admin_config = admin_config or {}
    raw = get_env("AZIZAM_ADMIN_DB") or admin_config.get("db_path") or "data/admin.db"
    path = Path(str(raw))
    return path if path.is_absolute() else ROOT / path


def resolve_admin_db_path() -> Path:
    config = load_bootstrap_config(bootstrap_config_path())
    return configured_admin_db_path(config.get("admin", {}) or {})
