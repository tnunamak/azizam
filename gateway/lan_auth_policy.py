"""LAN authentication policy loaded from operator settings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gateway import admin_store

LAN_PLACEHOLDER_KEYS_SETTING_KEY = "lan_placeholder_keys"
DEFAULT_LAN_PLACEHOLDER_KEYS = ["lan", "none", "sk-local", "dummy", "EMPTY", "not-needed"]
MAX_LAN_PLACEHOLDER_KEYS = 64
MAX_LAN_PLACEHOLDER_KEY_LENGTH = 128


class LanPlaceholderKeysError(ValueError):
    """Raised when a placeholder-key policy document is invalid."""


def validate_lan_placeholder_keys(value: Any) -> list[str]:
    """Return a normalized placeholder-key list or raise for unsafe policy input."""
    if not isinstance(value, list):
        raise LanPlaceholderKeysError("lan_placeholder_keys must be a list")
    if len(value) > MAX_LAN_PLACEHOLDER_KEYS:
        raise LanPlaceholderKeysError("lan_placeholder_keys is too large")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise LanPlaceholderKeysError("lan_placeholder_keys entries must be strings")
        key = item.strip()
        if not key:
            raise LanPlaceholderKeysError("lan_placeholder_keys entries must not be empty")
        if len(key) > MAX_LAN_PLACEHOLDER_KEY_LENGTH:
            raise LanPlaceholderKeysError("lan_placeholder_keys entries are too long")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in key):
            raise LanPlaceholderKeysError("lan_placeholder_keys entries must not contain control characters")
        if key.startswith(("opk_", "oak_")) or (key.startswith("sk-") and key != "sk-local"):
            raise LanPlaceholderKeysError("lan_placeholder_keys must not contain real key prefixes")
        normalized.append(key)
    return normalized


def lan_placeholder_keys(path: Path) -> list[str]:
    """Return the effective LAN placeholder-key policy for the admin database."""
    stored = admin_store.get_operator_setting(LAN_PLACEHOLDER_KEYS_SETTING_KEY, path=path)
    if stored is None:
        return list(DEFAULT_LAN_PLACEHOLDER_KEYS)
    try:
        value = stored["value"]
        if not isinstance(value, dict) or set(value) != {LAN_PLACEHOLDER_KEYS_SETTING_KEY}:
            raise LanPlaceholderKeysError("malformed lan_placeholder_keys policy")
        return validate_lan_placeholder_keys(value[LAN_PLACEHOLDER_KEYS_SETTING_KEY])
    except (KeyError, LanPlaceholderKeysError, TypeError):
        return []
