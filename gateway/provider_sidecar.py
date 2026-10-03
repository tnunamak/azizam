"""Runtime environment injector for the LiteLLM sidecar.

The generated LiteLLM YAML keeps provider keys as ``os.environ/NAME`` refs.
This module resolves gateway-owned encrypted provider secrets immediately
before starting LiteLLM, then replaces itself with the LiteLLM process. Secret
values do not appear in YAML, argv, stdout, or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway.litellm_substrate import secret_env_name_for_provider
from gateway.secret_store import credential_origin_from_url, get_provider_secret

DEFAULT_ADMIN_DB = Path("data/admin.db")


def provider_secret_env_map_from_registry(registry: Mapping[str, Any]) -> dict[str, str]:
    """Return ``{env_var: secret_id}`` declared by provider registry records.

    This accepts the contract used by the provider-admin lane:
    Env-only providers keep ``config.api_key_env``. UI-secret providers keep
    ``config.api_key_secret_id`` and use the same deterministic env-var naming
    helper as the LiteLLM config compiler.
    """

    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if not isinstance(services, Mapping):
        return {}
    out: dict[str, str] = {}
    for raw_service_id, raw_service in services.items():
        if not isinstance(raw_service, Mapping):
            continue
        config = raw_service.get("config") if isinstance(raw_service.get("config"), Mapping) else {}
        refs = (
            raw_service.get("secret_refs")
            if isinstance(raw_service.get("secret_refs"), Mapping)
            else {}
        )
        secret_id = str(
            config.get("api_key_secret_id")
            or refs.get("api_key_secret_id")
            or refs.get("provider_secret_id")
            or refs.get("api_key")
            or ""
        ).strip()
        if secret_id and secret_id != "<external-secret>":
            provider_id = str(
                config.get("provider_id") or raw_service.get("id") or raw_service_id or ""
            )
            env_name = secret_env_name_for_provider(provider_id, secret_id)
            out[env_name] = secret_id
    return out


def provider_secret_origin_map_from_registry(registry: Mapping[str, Any]) -> dict[str, str]:
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if not isinstance(services, Mapping):
        return {}
    out: dict[str, str] = {}
    for raw_service_id, raw_service in services.items():
        if not isinstance(raw_service, Mapping):
            continue
        config = raw_service.get("config") if isinstance(raw_service.get("config"), Mapping) else {}
        refs = raw_service.get("secret_refs") if isinstance(raw_service.get("secret_refs"), Mapping) else {}
        secret_id = str(
            config.get("api_key_secret_id")
            or refs.get("api_key_secret_id")
            or refs.get("provider_secret_id")
            or refs.get("api_key")
            or ""
        ).strip()
        if not secret_id or secret_id == "<external-secret>":
            continue
        provider_id = str(config.get("provider_id") or raw_service.get("id") or raw_service_id or "")
        env_name = secret_env_name_for_provider(provider_id, secret_id)
        try:
            origin = credential_origin_from_url(str(config.get("base_url") or ""))
        except Exception:
            origin = ""
        if origin:
            out[env_name] = origin
    return out


def provider_secret_env_map_from_db(db_path: Path) -> dict[str, str]:
    registry = _read_service_registry(db_path)
    return provider_secret_env_map_from_registry(registry or {})


def provider_secret_fingerprint(db_path: Path) -> str:
    """Return a non-secret hash that changes on provider secret rotate/delete."""

    secret_map = provider_secret_env_map_from_db(db_path)
    rows: list[tuple[str, str, str, str, str]] = []
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as conn:
            if _table_exists(conn, "provider_secrets"):
                for env_name, secret_id in sorted(secret_map.items()):
                    row = conn.execute(
                        """
                        SELECT nonce_b64, ciphertext_b64, updated_at
                        FROM provider_secrets
                        WHERE id = ?
                        """,
                        (secret_id,),
                    ).fetchone()
                    if row is None:
                        rows.append((env_name, secret_id, "missing", "", ""))
                    else:
                        rows.append(
                            (env_name, secret_id, str(row[0]), str(row[1]), str(row[2]))
                        )
    payload = json.dumps(
        {"provider_secret_env_map": sorted(secret_map.items()), "provider_secrets": rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_sidecar_env(
    *,
    db_path: Path,
    base_env: Mapping[str, str] | None = None,
    env_map: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment with decrypted provider secrets injected."""

    child_env = dict(os.environ if base_env is None else base_env)
    registry = _read_service_registry(db_path) or {}
    resolved_map = dict(env_map or provider_secret_env_map_from_registry(registry))
    origin_map = provider_secret_origin_map_from_registry(registry)
    require_origin = env_map is None
    for env_name, secret_id in resolved_map.items():
        try:
            expected_origin = origin_map.get(str(env_name))
            if require_origin and not expected_origin:
                raise PermissionError("provider secret has no approved execution origin")
            child_env[str(env_name)] = get_provider_secret(
                str(secret_id),
                db_path=db_path,
                expected_origin=expected_origin,
            )
        except Exception as exc:
            child_env.pop(str(env_name), None)
            print(
                f"[provider-sidecar] provider secret {secret_id!r} was not injected "
                f"into {env_name!r}: {exc.__class__.__name__}",
                file=sys.stderr,
            )
    return child_env


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="provider_sidecar")
    subcommands = parser.add_subparsers(dest="command", required=True)

    hash_parser = subcommands.add_parser("fingerprint")
    hash_parser.add_argument("--db", type=Path, default=DEFAULT_ADMIN_DB)

    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("--db", type=Path, default=DEFAULT_ADMIN_DB)
    run_parser.add_argument("exec_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "fingerprint":
        print(provider_secret_fingerprint(args.db))
        return 0

    exec_args = list(args.exec_args)
    if exec_args and exec_args[0] == "--":
        exec_args = exec_args[1:]
    if not exec_args:
        parser.error("run requires a command after --")
    child_env = build_sidecar_env(db_path=args.db)
    os.execvpe(exec_args[0], exec_args, child_env)
    raise AssertionError("unreachable after exec")


def _read_service_registry(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path)) as conn:
        if not _table_exists(conn, "operator_settings"):
            return None
        row = conn.execute(
            "SELECT value_json FROM operator_settings WHERE key = 'service_registry'"
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


if __name__ == "__main__":
    raise SystemExit(main())
