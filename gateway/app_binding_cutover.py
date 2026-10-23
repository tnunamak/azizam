"""Ownership cut-over for App keys and trusted addresses (owner visibility step 2, design §8.1).

    python -m gateway.app_binding_cutover accept --admin-db PATH --digest sha256:... [--by NAME]
    python -m gateway.app_binding_cutover apply --admin-db PATH

``accept`` records the digest of a preview the owner reviewed (``gateway.app_binding_preview``). It refuses
a digest that is not the database's digest now, and a preview that blocks apply. It does not need the
gateway stopped.

``apply`` changes the database once, and only when every one of these holds:

* the cut-over is not applied yet, and the database has run this release's migrations;
* the gateway is not running: it holds its private directory lock for its whole life (gateway/private_tmp.py);
* no other process has the database open: apply holds the store lock (gateway/sqlite_startup.py) while it
  works, so a gateway that starts meanwhile waits for it;
* an accepted preview matches the database's digest now, checked again inside the write transaction;
* no App would stop while in use (the preview's ``blocks_apply``).

It then copies the database beside itself (``admin.db.pre-ownership-cutover-<epoch>``), and in one
transaction revokes every unproven App key, removes every unproven trusted address, installs the guards
and records the ownership version. Any failure rolls the whole change back.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from gateway import admin_store, app_binding_guard, app_binding_preview, private_tmp, sqlite_startup

ACCEPTANCES = "app_binding_cutover_acceptances"
BACKUP_PREFIX = "pre-ownership-cutover-"
ACTION = "app_binding.cutover_applied"


class CutoverError(RuntimeError):
    """The cut-over is refused, or cannot be done; the message says why and what to do next."""


def record_acceptance(conn: sqlite3.Connection, digest: str, accepted_by: str, *, now: int | None = None) -> None:
    """Record that the owner accepted the preview with this digest. The caller commits."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ACCEPTANCES} ("
                 "id INTEGER PRIMARY KEY, digest TEXT NOT NULL, accepted_by TEXT NOT NULL, accepted_at INTEGER NOT NULL)")
    conn.execute(f"INSERT INTO {ACCEPTANCES}(digest, accepted_by, accepted_at) VALUES (?, ?, ?)",
                 (digest, accepted_by, int(time.time()) if now is None else now))


def accept(admin_db: Path, digest: str, accepted_by: str) -> str:
    path = _existing(admin_db)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _require_migrated(conn)
            _require_not_applied(conn)
            body = app_binding_preview.build(conn, as_of=int(time.time()))
            current = app_binding_preview.digest(body)
            if current != digest:
                raise CutoverError(f"the database changed since this preview (its digest is now {current}); "
                                   "re-run the preview and accept that digest.")
            _require_unblocked(body)
            record_acceptance(conn, current, accepted_by)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return f"accepted {current} for {accepted_by}"


def apply(admin_db: Path) -> list[str]:
    """Apply the accepted cut-over. Returns the report lines it printed."""
    path = _existing(admin_db)
    store_lock = _hold_store_lock(path)
    try:
        _refuse_while_gateway_runs(private_tmp.configured_dir(path))
        conn = _connect(path)
        try:
            _require_not_applied(conn)
            _require_migrated(conn)
            _decide(conn)  # refuse before any copy is made
            backup = _backup(conn, path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                body, digest = _decide(conn)  # the same test again, now that no other writer can change the database
                revoked, removed = _change(conn, body, digest)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    finally:
        store_lock.close()
    return _report(backup, revoked, removed)


def _connect(path: Path) -> sqlite3.Connection:
    """Autocommit, so the transactions below are the ones written here; rows by name for the preview."""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _existing(admin_db: Path) -> Path:
    path = Path(admin_db)
    if not path.is_file():
        raise CutoverError(f"no database at {path}")
    return path


def _require_migrated(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None or int(row[0]) != admin_store.SCHEMA_VERSION:
        raise CutoverError("this database has not run this release's migrations: start the gateway once with this "
                           "release, then preview and accept again")


def _require_not_applied(conn: sqlite3.Connection) -> None:
    if app_binding_guard.recorded(conn):
        raise CutoverError("the cut-over is already applied; there is nothing to do")


def _require_unblocked(body: dict[str, Any]) -> None:
    blocked = body["summary"]["apps_blocking_apply"]
    if blocked:
        raise CutoverError(f"this preview blocks apply for App {', '.join(blocked)}: each would stop while in use "
                           "with no proven binding. Keep a proven key or address on it, or disable it, then preview again")


def _decide(conn: sqlite3.Connection) -> tuple[dict[str, Any], str]:
    """The preview of the database now, and its digest, when an accepted preview matches and nothing blocks."""
    body = app_binding_preview.build(conn, as_of=int(time.time()))
    digest = app_binding_preview.digest(body)
    if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '{ACCEPTANCES}'").fetchone() is None:
        raise CutoverError("no accepted preview: run the preview, then accept its digest")
    if conn.execute(f"SELECT 1 FROM {ACCEPTANCES} WHERE digest = ?", (digest,)).fetchone() is None:
        if conn.execute(f"SELECT 1 FROM {ACCEPTANCES}").fetchone() is not None:
            raise CutoverError(f"the database changed since the accepted preview; its digest is now {digest}. "
                               "Run the preview again and accept it")
        raise CutoverError("no accepted preview: run the preview, then accept its digest")
    _require_unblocked(body)
    return body, digest


def _hold_store_lock(path: Path):
    """The store lock, exclusively. Any open connection holds it shared, so an open database refuses this."""
    handle = os.fdopen(os.open(sqlite_startup.lock_path(path), os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise CutoverError("another process has the database open (the gateway or a tool). Stop it, then apply again") from None
    return handle


def _refuse_while_gateway_runs(directory: Path) -> None:
    """The gateway holds ``.owner.lock`` in its private directory for its whole life (gateway/private_tmp.py)."""
    lock_file = directory / ".owner.lock"
    if not lock_file.exists():
        return
    fd = os.open(lock_file, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise CutoverError("the gateway is running (its private directory is locked). Stop it, then apply again") from None
    finally:
        os.close(fd)


def _backup(conn: sqlite3.Connection, path: Path) -> Path:
    target = path.with_name(f"{path.name}.{BACKUP_PREFIX}{int(time.time())}")
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # private from the first byte
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def _change(conn: sqlite3.Connection, body: dict[str, Any], digest: str) -> tuple[list[dict], list[dict]]:
    now = int(time.time())
    revoked, removed = [], []
    for app in body["apps"]:
        for key in app["keys"]:
            if key["action"] == "revoke":
                conn.execute("UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, key["id"]))
                revoked.append({"id": key["id"], "prefix": key["prefix"], "app_id": app["app_id"], "created_by": key["created_by"]})
        for entry in app["trusted_addresses"]:
            if entry["action"] == "remove":
                conn.execute("DELETE FROM app_trusted_addresses WHERE id = ?", (entry["id"],))
                removed.append({"id": entry["id"], "address": entry["address"], "app_id": app["app_id"], "created_by": entry["created_by"]})
    app_binding_guard.install(conn)
    admin_store._record_audit_in_transaction(
        conn, ACTION, actor_kind="system", actor_id=None, actor_label=f"cli:{_operator()}",
        target_kind="app_binding", target_id=digest,
        metadata={"keys_revoked": [key["id"] for key in revoked], "addresses_removed": [entry["id"] for entry in removed]},
        now=now,
    )
    return revoked, removed


def _report(backup: Path, revoked: list[dict], removed: list[dict]) -> list[str]:
    def show(value: Any) -> str:
        return "NULL" if value is None else repr(value)

    lines = ["App-binding cut-over applied.", f"  backup: {backup}", f"  keys revoked: {len(revoked)}"]
    lines += [f"    {key['prefix']} on App {key['app_id']}, created_by {show(key['created_by'])}" for key in revoked]
    lines.append(f"  addresses removed: {len(removed)}")
    lines += [f"    {entry['address']} on App {entry['app_id']}, created_by {show(entry['created_by'])}" for entry in removed]
    lines += [f"  guards installed: {len(app_binding_guard.GUARDS)}", "  ownership version: recorded"]
    return lines


def _operator() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.app_binding_cutover",
                                     description="Accept, then apply, the ownership cut-over for App keys and trusted addresses.")
    commands = parser.add_subparsers(dest="command", required=True)
    accept_cmd = commands.add_parser("accept", help="record that the owner accepts the preview with this digest")
    accept_cmd.add_argument("--admin-db", required=True, type=Path)
    accept_cmd.add_argument("--digest", required=True, help="the digest the preview printed (sha256:...)")
    accept_cmd.add_argument("--by", default=None, help="who accepts it (default: this operating-system user)")
    apply_cmd = commands.add_parser("apply", help="apply the accepted cut-over; the gateway must be stopped")
    apply_cmd.add_argument("--admin-db", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "accept":
            print(accept(args.admin_db, args.digest, args.by or _operator()))
        else:
            print("\n".join(apply(args.admin_db)))
    except CutoverError as exc:
        print(f"cut-over refused: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        print(f"cut-over failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
