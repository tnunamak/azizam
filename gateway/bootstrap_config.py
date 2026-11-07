"""Shared startup-file and admin-store path resolution for gateway processes.

The startup file holds only the keys ``registry/startup.schema.json`` lists (docs/design/CONFIG_HOMES.md, C10). Loading
refuses any other key and says where the setting lives now.
"""

from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway import startup_config
from gateway.compat import get_env


ROOT = Path(__file__).resolve().parents[1]


def bootstrap_config_path() -> Path:
    raw = get_env("AZIZAM_CONFIG_PATH")
    return Path(raw) if raw else ROOT / "proxy_config.yml"


def load_bootstrap_config(path: Path) -> dict:
    """The startup file as a mapping; ``{}`` when there is no file. Raises ``StartupConfigError`` for a key it may not hold."""
    if not path.exists():
        return {}
    with path.open() as source:
        raw = yaml.safe_load(source)
    startup_config.check(raw, source=str(path))
    return raw or {}


def configured_admin_db_path(admin_config: Mapping[str, Any] | None = None) -> Path:
    admin_config = admin_config or {}
    raw = get_env("AZIZAM_ADMIN_DB") or admin_config.get("db_path") or "data/admin.db"
    path = Path(str(raw))
    return path if path.is_absolute() else ROOT / path


def resolve_admin_db_path() -> Path:
    config = load_bootstrap_config(bootstrap_config_path())
    return configured_admin_db_path(config.get("admin", {}) or {})
