"""Read-only preview of the App-binding cut-over (owner visibility step 2, design §8.1 step 1).

It opens one admin.db read-only and prints what the cut-over would do: every App with its accountable
person, every live key and trusted address with its recorded creator and whether that creator is proven,
the revocations and removals that would follow, and the Apps that apply would refuse to stop while in use.
It writes nothing, and it is not the apply: the owner reviews this output and its digest before any
apply, and the apply and its acceptance are not part of this module.

    python -m gateway.app_binding_preview --admin-db COPY_OF_ADMIN_DB [--json] [--as-of EPOCH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from gateway import app_binding_guard

# Schema 7 is the access-plan migration; a database below it has not reached the App rows this reads.
ACCESS_PLANS_SCHEMA = 7
RECENT_SECONDS = 30 * 24 * 3600
UNAVAILABLE = "unavailable"
NOT_EVALUATED = (
    "Creations with no recorded person or App (design §10.2 Q2) are not classified here: "
    "they live in media sidecars, outside admin.db."
)


class PreviewError(RuntimeError):
    """The database cannot be previewed; the message says why."""


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PreviewError(f"no database at {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _key_app_id(row: dict[str, Any]) -> str:
    return row["app_id"] or row["subject_id"]


def build(conn: sqlite3.Connection, *, as_of: int) -> dict[str, Any]:
    """The preview as data. Every field is derived from the rows; nothing is read from a receipt or a sidecar."""
    schema = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if schema is None or int(schema[0]) < ACCESS_PLANS_SCHEMA:
        raise PreviewError(
            "this database has not reached the access-plan migration (schema 7); "
            "start the gateway once on a copy so that migration runs, then preview again"
        )
    guards = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    receipt = conn.execute(
        "SELECT COUNT(*) FROM schema_migration_steps WHERE name = ?", (app_binding_guard.RECEIPT,),
    ).fetchone()[0] if _table_exists(conn, "schema_migration_steps") else 0

    session_app_ids = {row["app_id"] for row in _rows(conn, "SELECT app_id FROM session_apps")}
    keys = _rows(conn, "SELECT id, subject_id, app_id, prefix, display_name, created_by, expires_at, last_used_at "
                       "FROM credentials WHERE subject_kind = 'app' AND revoked_at IS NULL ORDER BY id")
    addresses = _rows(conn, "SELECT id, app_id, address, created_by FROM app_trusted_addresses ORDER BY id")
    apps = _rows(conn, "SELECT id, kind, name, owner_user_id, access_plan, disabled_at FROM apps ORDER BY id")
    created_by_audit = {
        row["target_id"]: row["actor_id"] for row in _rows(
            conn, "SELECT target_id, actor_id FROM audit_events WHERE action = 'app.create' AND target_kind = 'app' "
                  "ORDER BY created_at, id")
    }

    recent_from = as_of - RECENT_SECONDS
    previewed = []
    for app in apps:
        if app["id"] in session_app_ids:
            continue
        owner = app["owner_user_id"]
        app_keys = [k for k in keys if _key_app_id(k) == app["id"]]
        app_addresses = [a for a in addresses if a["app_id"] == app["id"]]
        keep_keys = [k for k in app_keys if k["created_by"] is not None and k["created_by"] == owner]
        keep_addresses = [a for a in app_addresses if a["created_by"] is not None and a["created_by"] == owner]
        had_bindings = bool(app_keys or app_addresses)
        survivors = len(keep_keys) + len(keep_addresses)
        loses_all = had_bindings and survivors == 0
        recent_key_use = any(k["last_used_at"] is not None and k["last_used_at"] >= recent_from for k in app_keys)
        blocks = []
        if loses_all and recent_key_use:
            blocks.append("used in the last 30 days and would keep no binding: a replacement or an explicit owner stop is required")
        if loses_all and app_addresses:
            blocks.append("trusted-address use history is unavailable on admin.db: cannot show these addresses unused")
        previewed.append({
            "app_id": app["id"],
            "name": app["name"],
            "kind": app["kind"],
            "accountable_person_id": owner,
            "access_plan": app["access_plan"],
            "disabled": app["disabled_at"] is not None,
            "created_by_audit": created_by_audit.get(app["id"]),
            "keys": [
                {
                    "id": k["id"], "prefix": k["prefix"], "display_name": k["display_name"],
                    "created_by": k["created_by"], "last_used_at": k["last_used_at"],
                    "proven": k in keep_keys, "action": "keep" if k in keep_keys else "revoke",
                }
                for k in app_keys
            ],
            "trusted_addresses": [
                {
                    "id": a["id"], "address": a["address"], "created_by": a["created_by"],
                    "proven": a in keep_addresses, "action": "keep" if a in keep_addresses else "remove",
                    "use_history": UNAVAILABLE,
                }
                for a in app_addresses
            ],
            "surviving_bindings": survivors,
            "would_lose_all_bindings": loses_all,
            "blocks_apply": blocks,
        })

    blocked = sorted({app["app_id"] for app in previewed if app["blocks_apply"]})
    summary = {
        "apps": len(previewed),
        "session_apps_excluded": len(session_app_ids),
        "keys_to_revoke": sum(1 for a in previewed for k in a["keys"] if k["action"] == "revoke"),
        "addresses_to_remove": sum(1 for a in previewed for t in a["trusted_addresses"] if t["action"] == "remove"),
        "apps_blocking_apply": blocked,
    }
    return {
        "as_of": as_of,
        "schema_version": int(schema[0]),
        "guards_installed": sorted(name for name, _ in app_binding_guard.GUARDS if name in guards),
        "guards_expected": len(app_binding_guard.GUARDS),
        "ownership_version_recorded": bool(receipt),
        "apps": previewed,
        "summary": summary,
        "not_evaluated": [NOT_EVALUATED],
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def digest(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def render(body: dict[str, Any], preview_digest: str) -> str:
    lines = [
        f"App binding preview (read-only)  as of {body['as_of']}  digest {preview_digest}",
        f"schema {body['schema_version']}; guards installed {len(body['guards_installed'])} of {body['guards_expected']}; "
        f"ownership version recorded: {'yes' if body['ownership_version_recorded'] else 'no'}",
        "",
    ]
    for app in body["apps"]:
        lines.append(f"App {app['app_id']} \"{app['name']}\" kind={app['kind']} "
                     f"accountable={app['accountable_person_id']} plan={app['access_plan']}"
                     f"{' disabled' if app['disabled'] else ''}")
        for key in app["keys"]:
            lines.append(f"  key {key['prefix']} created_by={_show(key['created_by'])} "
                         f"proven={'yes' if key['proven'] else 'no'} last_used={key['last_used_at'] or 'never'} "
                         f"-> {key['action']}")
        for entry in app["trusted_addresses"]:
            lines.append(f"  address {entry['address']} created_by={_show(entry['created_by'])} "
                         f"proven={'yes' if entry['proven'] else 'no'} use_history={entry['use_history']} "
                         f"-> {entry['action']}")
        if not app["keys"] and not app["trusted_addresses"]:
            lines.append("  no keys or trusted addresses")
        for reason in app["blocks_apply"]:
            lines.append(f"  BLOCKS APPLY: {reason}")
    s = body["summary"]
    lines += [
        "",
        f"summary: apps={s['apps']} session_apps_excluded={s['session_apps_excluded']} "
        f"keys_to_revoke={s['keys_to_revoke']} addresses_to_remove={s['addresses_to_remove']} "
        f"apps_blocking_apply={len(s['apps_blocking_apply'])}",
    ]
    lines += [f"not evaluated: {note}" for note in body["not_evaluated"]]
    return "\n".join(lines)


def _show(value: Any) -> str:
    if value is None:
        return "NULL"
    return repr(value) if value == "" else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.app_binding_preview", description=__doc__.splitlines()[0])
    parser.add_argument("--admin-db", required=True, type=Path, help="a copy of admin.db; it is opened read-only")
    parser.add_argument("--json", action="store_true", help="print the preview as JSON")
    parser.add_argument("--as-of", type=int, default=None, help="epoch seconds for the recent-use window (default: now)")
    args = parser.parse_args(argv)
    try:
        conn = connect_read_only(args.admin_db)
        try:
            body = build(conn, as_of=args.as_of if args.as_of is not None else int(time.time()))
        finally:
            conn.close()
    except (PreviewError, sqlite3.Error) as exc:
        print(f"preview failed: {exc}", file=sys.stderr)
        return 1
    preview_digest = digest(body)
    if args.json:
        print(json.dumps({"digest": preview_digest, **body}, indent=2, sort_keys=True))
    else:
        print(render(body, preview_digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
