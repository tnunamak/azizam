"""SQLite-backed durable store for route receipts.

Receipts are compact diagnostics about routing decisions. This module persists
the *internal* audience shape of a receipt so admin/client views can be derived
later by re-filtering, without ever having stored raw prompts, image bytes,
secrets, or request bodies.

Layout mirrors ``responses_store.py``: stdlib ``sqlite3``, WAL mode, integrity
check on startup, short-lived connections. Single writer is not required —
receipt write volume is low (one per gateway request).

Public surface
--------------

- ``init_db(path)``                — create the file, run schema, integrity-check
- ``record(receipt_or_dict, ...)`` — persist one receipt (rejects unsafe shapes)
- ``get(request_id, ...)``         — fetch one receipt as a dict (by audience)
- ``recent(limit, ...)``           — list recent receipts (by audience)
- ``prune(retain_s, ...)``         — delete rows older than the retention window
- ``count(...)``                   — total row count

The on-disk shape stores the full internal dict as JSON. ``get`` and ``recent``
re-render the dict through ``RouteReceipt.to_dict``-equivalent filtering for
the requested audience.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from gateway.receipts import CLIENT_VISIBLE_FIELDS, RouteReceipt
from gateway.redaction import assert_safe_mapping, assert_safe_top_level

_default_db = Path(__file__).resolve().parent.parent / "data" / "receipts.db"
DB_PATH = Path(os.environ["AI_GATEWAY_RECEIPTS_DB"]) if os.environ.get("AI_GATEWAY_RECEIPTS_DB") else _default_db

# Retention: 14 days. Receipts are diagnostics, not audit logs; the cap keeps
# the database bounded on a long-running gateway.
RETAIN_S = 14 * 24 * 3600

# v2 adds the ``app_id`` column: the durable id of the owning App, so a user
# can be shown ONLY their own receipts. ``app_profile`` (a shared permission
# profile id) and ``api_key_name`` (a non-unique App name) are NOT safe owner
# keys; ``app_id`` is. Receipts are 14-day diagnostics, not audit logs, so the
# v1→v2 upgrade recreates the table rather than preserving old (unattributed)
# rows — see ``_exec_schema``.
SCHEMA_VERSION = 2

_AUDIENCES = ("internal", "admin", "client")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
  request_id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  protocol TEXT,
  intent TEXT,
  path TEXT,
  app_profile TEXT,
  app_id TEXT,
  api_key_name TEXT,
  status TEXT,
  job_id TEXT,
  parent_request_id TEXT,
  receipt_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_created_at ON receipts(created_at);
CREATE INDEX IF NOT EXISTS receipts_app_profile ON receipts(app_profile);
CREATE INDEX IF NOT EXISTS receipts_app_id ON receipts(app_id);
CREATE INDEX IF NOT EXISTS receipts_job_id ON receipts(job_id);
CREATE INDEX IF NOT EXISTS receipts_parent ON receipts(parent_request_id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")


def _exec_schema(conn: sqlite3.Connection) -> None:
    # Read the on-disk version BEFORE running the full schema. The schema's
    # ``CREATE INDEX ... ON receipts(app_id)`` would fail against a pre-v2 table
    # that lacks the column, so an older table must be dropped first.
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);")
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    db_version = int(row[0]) if row is not None else None

    if db_version is not None and db_version != SCHEMA_VERSION:
        if db_version > SCHEMA_VERSION:
            # A newer on-disk version than the code understands: refuse rather
            # than risk writing a shape we don't know how to read back.
            raise sqlite3.DatabaseError(
                f"receipt_store schema version mismatch: db={db_version} "
                f"code={SCHEMA_VERSION} (downgrade not supported)"
            )
        # Older schema: recreate, don't migrate. Receipts are short-lived
        # diagnostics (14-day retention), not an audit log, and pre-v2 rows have
        # no owning-App id — they cannot be safely attributed to a user.
        # Backfilling from the non-unique ``api_key_name`` would risk
        # attributing one user's calls to another, the exact leak ``app_id``
        # exists to prevent. So old rows are discarded deliberately.
        print(
            f"[receipt_store] upgrading schema {db_version}->{SCHEMA_VERSION}; "
            "recreating receipts table (old diagnostics discarded)",
            flush=True,
        )
        conn.executescript("DROP TABLE IF EXISTS receipts;\nDELETE FROM schema_version;\n")
        db_version = None

    conn.executescript(_SCHEMA)
    if db_version is None:
        # Fresh DB or just-recreated: stamp the current version (the schema's
        # CREATE TABLE IF NOT EXISTS leaves schema_version empty).
        if conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone() is None:
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
    conn.commit()


def init_db(path: Path = DB_PATH) -> None:
    """Create the data dir, run the schema, integrity-check, replace if broken."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            test = sqlite3.connect(str(path), timeout=5.0)
            try:
                _apply_pragmas(test)
                row = test.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    raise sqlite3.DatabaseError(
                        f"integrity_check returned: {row[0] if row else 'empty'}"
                    )
            finally:
                test.close()
        except sqlite3.DatabaseError as e:
            broken = path.with_name(f"{path.name}.broken-{int(time.time())}")
            print(f"[receipt_store] DB corrupt ({e}); moving aside to {broken}", flush=True)
            try:
                shutil.move(str(path), str(broken))
            except Exception as mv_err:  # noqa: BLE001
                print(f"[receipt_store] failed to move broken db: {mv_err}", flush=True)
                try:
                    path.unlink()
                except Exception:
                    pass

    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        _apply_pragmas(conn)
        _exec_schema(conn)
    finally:
        conn.close()


@contextmanager
def _conn(path: Path = DB_PATH):
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


def _coerce_to_internal_dict(receipt_or_dict: Any) -> dict[str, Any]:
    if isinstance(receipt_or_dict, RouteReceipt):
        return receipt_or_dict.to_dict("internal")
    if isinstance(receipt_or_dict, Mapping):
        return dict(receipt_or_dict)
    raise TypeError(
        f"receipt must be RouteReceipt or mapping, got {type(receipt_or_dict).__name__}"
    )


def _assert_safe(data: Mapping[str, Any]) -> None:
    """Reject obviously unsafe shapes before we hit disk.

    Cheap structural check — not a substitute for callers building receipts
    correctly. Delegates the forbidden-key vocabulary to ``gateway.redaction``
    so receipts and media jobs share one contract.
    """
    if "request_id" not in data or not data["request_id"]:
        raise ValueError("receipt missing request_id")
    assert_safe_top_level("receipt", data)
    metadata = data.get("metadata")
    if metadata is None:
        return
    assert_safe_mapping("receipt.metadata", metadata)


def _filter_for_audience(data: Mapping[str, Any], audience: str) -> dict[str, Any]:
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    if audience == "internal":
        return dict(data)
    if audience == "admin":
        out = dict(data)
        out.pop("metadata", None)
        return out
    return {name: data[name] for name in CLIENT_VISIBLE_FIELDS if name in data}


def record(
    receipt: RouteReceipt | Mapping[str, Any],
    *,
    created_at: int | None = None,
    path: Path = DB_PATH,
) -> str:
    """Persist a receipt. Returns the stored request_id.

    Uses INSERT OR REPLACE so retries with the same request_id are idempotent.
    """
    data = _coerce_to_internal_dict(receipt)
    _assert_safe(data)
    ts = int(created_at if created_at is not None else time.time())
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO receipts "
            "(request_id, created_at, protocol, intent, path, app_profile, "
            " app_id, api_key_name, status, job_id, parent_request_id, "
            " receipt_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["request_id"],
                ts,
                data.get("protocol"),
                data.get("intent"),
                data.get("path"),
                data.get("app_profile"),
                data.get("app_id"),
                data.get("api_key_name"),
                data.get("status"),
                data.get("job_id"),
                data.get("parent_request_id"),
                json.dumps(data, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    return data["request_id"]


def get(
    request_id: str,
    *,
    audience: str = "internal",
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """Return the stored receipt filtered for ``audience``, or None."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT receipt_json, created_at FROM receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if not row:
        return None
    data = _safe_loads(row[0])
    if not isinstance(data, Mapping):
        return None
    out = _filter_for_audience(data, audience)
    out.setdefault("_stored_at", int(row[1]))
    return out


def recent(
    limit: int = 50,
    *,
    audience: str = "internal",
    app_profile: str | None = None,
    app_ids: list[str] | None = None,
    since_s: int | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` most-recent receipts, newest first.

    Optional filters: ``app_profile`` (exact match), ``app_ids`` (owner-scope:
    return only receipts whose ``app_id`` is in this allow-list), and
    ``since_s`` (only receipts written within the last N seconds).

    ``app_ids`` is a *positive* membership filter — it can only ever narrow the
    result to the given ids, never widen it. An empty list returns no rows
    (the caller owns nothing), and because ``app_id IN (...)`` never matches
    NULL, receipts with no owning App (unauthenticated, legacy keys) are
    excluded. This is the privacy boundary for the user-facing activity feed:
    pass the signed-in user's owned app_ids and they can see only their own.
    """
    if limit <= 0:
        return []
    if app_ids is not None and not app_ids:
        # The caller explicitly owns no Apps — never fall through to an
        # unfiltered query (that would leak everyone's receipts).
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if app_profile is not None:
        clauses.append("app_profile = ?")
        params.append(app_profile)
    if app_ids is not None:
        placeholders = ", ".join("?" for _ in app_ids)
        clauses.append(f"app_id IN ({placeholders})")
        params.extend(app_ids)
    if since_s is not None:
        clauses.append("created_at >= ?")
        params.append(int(time.time()) - int(since_s))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))

    with _conn(path) as conn:
        rows = conn.execute(
            f"SELECT receipt_json, created_at FROM receipts{where} "
            "ORDER BY created_at DESC, request_id DESC LIMIT ?",
            params,
        ).fetchall()

    out: list[dict[str, Any]] = []
    for receipt_json, created_at in rows:
        data = _safe_loads(receipt_json)
        if not isinstance(data, Mapping):
            continue
        filtered = _filter_for_audience(data, audience)
        filtered.setdefault("_stored_at", int(created_at))
        out.append(filtered)
    return out


def prune(retain_s: int = RETAIN_S, path: Path = DB_PATH) -> int:
    """Delete rows older than ``retain_s`` seconds. Returns deleted count."""
    cutoff = int(time.time()) - int(retain_s)
    with _conn(path) as conn:
        cur = conn.execute("DELETE FROM receipts WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def count(path: Path = DB_PATH) -> int:
    with _conn(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()
        return int(row[0]) if row else 0


def schema_version(path: Path = DB_PATH) -> int | None:
    with _conn(path) as conn:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else None


def _safe_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


__all__ = [
    "DB_PATH",
    "RETAIN_S",
    "SCHEMA_VERSION",
    "init_db",
    "record",
    "get",
    "recent",
    "prune",
    "count",
    "schema_version",
]
