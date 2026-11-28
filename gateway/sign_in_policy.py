"""The sign-in policy, from ``registry/sign_in_policy.yml``.

The file is data and ``registry/sign_in_policy.schema.json`` is its closed schema. This module holds no value of its
own: a missing or broken file is an error, never a guess. It reads only the bundled release file, so every sign-in
module may call it at import or at call time; it does not wait for the config sections to load.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema
from http.cookies import SimpleCookie

from gateway.strict_yaml import safe_load_strict

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "sign_in_policy.yml"
SCHEMA_PATH = _REGISTRY_DIR / "sign_in_policy.schema.json"


@dataclass(frozen=True)
class SignInPolicy:
    provider_enabled: bool
    scopes: tuple[str, ...]
    groups_claim: str
    clock_skew_seconds: int
    display_name_max_chars: int
    session_cookie_name: str
    session_cookie_max_age_seconds: int
    pending_login_seconds: int
    max_pending_logins: int
    auth_time_clock_skew_seconds: int
    jwks_ttl_seconds: int
    jwks_refresh_interval_seconds: int
    connection_attempt_seconds: int


def _read(path: Path, schema_path: Path) -> SignInPolicy:
    document = safe_load_strict(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(document)
    def check_integers(value):
        if isinstance(value, dict):
            for item in value.values():
                check_integers(item)
        elif isinstance(value, list):
            for item in value:
                check_integers(item)
        elif isinstance(value, float):
            raise ValueError("policy integers must use integer syntax")

    check_integers(document)
    cookie_test = SimpleCookie()
    cookie_test[document["session_cookie"]["name"]] = "test"
    if document["jwks"]["refresh_interval_seconds"] > document["jwks"]["ttl_seconds"]:
        raise ValueError("JWKS refresh interval exceeds TTL")
    provider, cookie, pending, jwks = (document["provider"], document["session_cookie"], document["pending_login"],
                                       document["jwks"])
    return SignInPolicy(
        provider_enabled=provider["enabled"],
        scopes=tuple(provider["scopes"]),
        groups_claim=provider["groups_claim"],
        clock_skew_seconds=provider["clock_skew_seconds"],
        display_name_max_chars=provider["display_name_max_chars"],
        session_cookie_name=cookie["name"],
        session_cookie_max_age_seconds=cookie["max_age_seconds"],
        pending_login_seconds=pending["seconds"],
        max_pending_logins=pending["max_pending"],
        auth_time_clock_skew_seconds=document["auth_time_clock_skew_seconds"],
        jwks_ttl_seconds=jwks["ttl_seconds"],
        jwks_refresh_interval_seconds=jwks["refresh_interval_seconds"],
        connection_attempt_seconds=document["connection_attempt_seconds"],
    )


@functools.lru_cache(maxsize=1)
def policy() -> SignInPolicy:
    """Validate one release policy snapshot per process; restart to apply changes."""
    return _read(BUNDLED_PATH, SCHEMA_PATH)
