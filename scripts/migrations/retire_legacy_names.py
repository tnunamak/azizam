#!/usr/bin/env python3
"""Retire gateway-owned names in the service registry, without exposing secrets.

Dry-run is the default. Apply only after stopping Azizam, or explicitly use
--allow-running (a restart is still required to reload the registry and sidecar).
Other settings, provider credentials, model data and historical records are untouched.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
OLD_SELF = "openai-proxy"
NEW_SELF = "azizam"
ENV_PATTERN = re.compile(r"\b(?:AI_GATEWAY_|OPENAI_PROXY_|VIVID_|AGW_)[A-Z0-9_]+\b")
ENV_SPECIAL = {
    "VIVID_ADMIN_API_KEY": "AZIZAM_ADMIN_KEY",
    "VIVID_OPENAI_API_KEY": "AZIZAM_API_KEY",
    "VIVID_OPENAI_BASE_URL": "AZIZAM_BASE_URL",
}
RECIPE_ENV_PATTERN = re.compile(r"\$(?:\{(?P<braced>(?:AI_GATEWAY_|OPENAI_PROXY_|VIVID_|AGW_)[A-Z0-9_]+)\}|(?P<plain>(?:AI_GATEWAY_|OPENAI_PROXY_|VIVID_|AGW_)[A-Z0-9_]+))")


@dataclass(frozen=True)
class Change:
    path: str
    value: str
    reason: str


def renamed_env(name: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match[0]
        if token in ENV_SPECIAL:
            return ENV_SPECIAL[token]
        for prefix in ("AI_GATEWAY_", "OPENAI_PROXY_", "VIVID_", "AGW_"):
            if token.startswith(prefix):
                return "AZIZAM_" + token[len(prefix):]
        raise AssertionError("matched environment token has no known prefix")

    return ENV_PATTERN.sub(replace, name)


def json_path(*parts: str) -> str:
    # SQLite JSON paths cannot reliably address embedded quotes in member names.
    if any('"' in part for part in parts):
        raise ValueError("registry member name contains an unsupported quote")
    return "$" + "".join("." + json.dumps(part) for part in parts)


def renamed_recipe(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match["braced"]:
            return "${" + renamed_env(match["braced"]) + "}"
        return "$" + renamed_env(match["plain"])

    return RECIPE_ENV_PATTERN.sub(replace, value)


def selected(conn: sqlite3.Connection, path: str):
    row = conn.execute(
        "SELECT json_extract(value_json, ?) FROM operator_settings WHERE key='service_registry'",
        (path,),
    ).fetchone()
    return row[0] if row else None


def plan_changes(conn: sqlite3.Connection) -> tuple[list[Change], bool]:
    """Read only owned JSON fields; never select the complete settings document."""
    changes: list[Change] = []
    services = [row[0] for row in conn.execute(
        "SELECT s.key FROM operator_settings o, json_each(o.value_json, '$.services') s "
        "WHERE o.key='service_registry'"
    )]
    move_self = OLD_SELF in services
    if move_self and NEW_SELF in services:
        raise ValueError("both old and new self service IDs exist; resolve the collision first")
    for service_id in services:
        for field in ("api_key_env", "base_url_env", "api_key_ref", "api_base_env"):
            path = json_path("services", service_id, "config", field)
            value = selected(conn, path)
            if isinstance(value, str) and ENV_PATTERN.fullmatch(value):
                changes.append(Change(path, renamed_env(value), "environment reference"))
        if service_id == OLD_SELF:
            path = json_path("services", service_id, "id")
            if selected(conn, path) == OLD_SELF:
                changes.append(Change(path, NEW_SELF, "self service ID"))
    frontends = [row[0] for row in conn.execute(
        "SELECT f.key FROM operator_settings o, json_each(o.value_json, '$.protocol_frontends') f "
        "WHERE o.key='service_registry'"
    )]
    for frontend in frontends:
        path = json_path("protocol_frontends", frontend, "public_discovery", "compatibility")
        if selected(conn, path) == "ai-gateway-native":
            changes.append(Change(path, "azizam-native", "compatibility tag"))
        # Recipe leaves are gateway-owned connection instructions. Only replace
        # variable tokens; URLs, provider credentials and free text stay intact.
        recipe_path = json_path("protocol_frontends", frontend, "public_discovery", "recipe")
        for fullkey, value in conn.execute(
            "SELECT j.fullkey, j.value FROM operator_settings o, json_tree(o.value_json, ?) j "
            "WHERE o.key='service_registry' AND j.type='text'", (recipe_path,),
        ):
            new = renamed_recipe(value)
            if new != value:
                changes.append(Change(fullkey, new, "recipe environment token"))
        path = json_path("protocol_frontends", frontend, "public_discovery", "base_url_recipe")
        value = selected(conn, path)
        if isinstance(value, str) and (new := renamed_recipe(value)) != value:
            changes.append(Change(path, new, "recipe environment token"))
    return changes, move_self


def require_stopped() -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "azizam.service", "--property=ActiveState", "--value"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot determine gateway state; use --allow-running explicitly") from exc
    state = result.stdout.strip()
    if result.returncode or state not in {"inactive", "failed"}:
        raise RuntimeError("gateway is running or its state is unknown; stop it or use --allow-running")


def read_connection(path: Path) -> sqlite3.Connection:
    # A WAL database without side files otherwise tries to create them even for
    # mode=ro. Immutable is safe for this read-only inspection only when no WAL
    # exists. The caller verifies the file has not changed before reporting.
    wal = Path(str(path) + "-wal")
    if wal.exists() and not Path(str(path) + "-shm").exists():
        raise RuntimeError("WAL has no shared-memory file; inspect a checkpointed database or a consistent backup instead")
    immutable = not wal.exists()
    return sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)


def snapshot_signature(path: Path) -> tuple:
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                 for p in (path, Path(str(path) + "-wal")))


def apply_changes(path: Path, *, allow_running: bool) -> tuple[list[Change], bool, Path | None]:
    if not allow_running:
        require_stopped()
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changes, move_self = plan_changes(conn)
            if not changes and not move_self:
                conn.rollback()
                return changes, move_self, None
            # Hold the writer lock while a separate connection takes a consistent
            # SQLite backup. It includes unrelated settings but never logs them.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(path.name + ".bak.retire-legacy-names." + stamp)
            with backup.open("xb"):
                backup.chmod(0o600)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
            if not allow_running:
                require_stopped()
            for change in changes:
                conn.execute(
                    "UPDATE operator_settings SET value_json=json_set(value_json, ?, ?) "
                    "WHERE key='service_registry'", (change.path, change.value),
                )
            if move_self:
                old_path, new_path = json_path("services", OLD_SELF), json_path("services", NEW_SELF)
                conn.execute(
                    "UPDATE operator_settings SET value_json=json_remove("
                    "json_set(value_json, ?, json_extract(value_json, ?)), ?) WHERE key='service_registry'",
                    (new_path, old_path, old_path),
                )
            # Keep the registry's compare-and-swap generation truthful. Do not
            # import gateway stores: their import/init paths can create state.
            conn.execute(
                "INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) "
                "VALUES ('service_registry', 1) ON CONFLICT(setting_key) DO UPDATE SET "
                "operator_settings_generation=operator_settings_generation+1"
            )
            conn.execute(
                "UPDATE operator_settings SET updated_at=?, updated_by=? WHERE key='service_registry'",
                (int(time.time()), "migration:retire-legacy-names"),
            )
            conn.commit()
            return changes, move_self, backup
        except BaseException:
            conn.rollback()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/admin.db")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="inspect without writing (default)")
    mode.add_argument("--apply", action="store_true", help="back up and migrate owned fields")
    parser.add_argument("--allow-running", action="store_true", help="permit inspection/migration while gateway runs")
    args = parser.parse_args(argv)
    path = args.db.expanduser().resolve()
    try:
        if args.apply:
            changes, move_self, backup = apply_changes(path, allow_running=args.allow_running)
        else:
            if not args.allow_running:
                require_stopped()
            before = snapshot_signature(path)
            with closing(read_connection(path)) as conn:
                changes, move_self = plan_changes(conn)
            if before != snapshot_signature(path):
                raise RuntimeError("database changed during inspection; retry for a consistent dry-run")
            backup = None
        print("APPLY" if args.apply else "DRY RUN (no writes)")
        for change in changes:
            print(f"  service_registry {change.path}: {change.reason}")
        if move_self:
            print("  service_registry $.services: rename self service map key")
        count = len(changes) + int(move_self)
        print(f"{count} owned {'field' if count == 1 else 'fields'} {'migrated' if args.apply else 'to migrate'}")
        if backup:
            print(f"Backup: {backup}")
        if args.allow_running:
            print("Running-gateway check bypassed explicitly; restart after apply to reload state.")
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        # SQL errors contain no selected values. Never include entire documents.
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
