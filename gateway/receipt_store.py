"""SQLite-backed durable store for route receipts.

Receipts keep routing, usage and spend metadata. This module persists
the *internal* audience shape of a receipt so admin/client views can be derived
later by re-filtering, without ever having stored raw prompts, image bytes,
secrets, or request bodies.

Layout mirrors ``responses_store.py``: stdlib ``sqlite3``, WAL mode, integrity
check on startup, short-lived connections. Single writer is not required —
receipt write volume is low (one per gateway request).

Public surface
--------------

- ``init_db(path)``                — create the file, run schema, integrity-check
- ``begin(receipt_or_dict, ...)``  — durably accept an outer receipt once
- ``finalize(receipt_or_dict, ...)`` — atomically terminalize an accepted receipt
- ``record(receipt_or_dict, ...)`` — legacy terminal-only compatibility write
- ``get(request_id, ...)``         — fetch one receipt as a dict (by audience)
- ``recent(limit, ...)``           — list recent receipts (by audience)
- ``prune(retain_s, ...)``         — delete rows older than the retention window
- ``history_floor(path)``         — persisted start of complete receipt history
- ``count(...)``                   — total row count

The on-disk shape stores the full internal dict as JSON. ``get`` and ``recent``
re-render the dict through ``RouteReceipt.to_dict``-equivalent filtering for
the requested audience.
"""

from __future__ import annotations

from gateway.compat import get_env
from gateway.sqlite_startup import connect, store_startup

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from gateway.receipts import PHASE_FIELDS, RouteReceipt, serialize_receipt_for_audience
from gateway.redaction import assert_safe_mapping, assert_safe_top_level
from gateway.ownership import stored_owner_key

_default_db = Path(__file__).resolve().parent.parent / "data" / "receipts.db"
DB_PATH = Path(get_env("AZIZAM_RECEIPTS_DB")) if get_env("AZIZAM_RECEIPTS_DB") else _default_db

# Usage and spend history is durable by default. Expiry is an owner choice.
RETAIN_S = None
RETENTION_SETTING_KEY = "receipt_retention"
RETENTION_DAYS = (None, 365, 90, 30)
STALE_AFTER_MS = 5 * 60 * 1000

# v2 adds the ``app_id`` column: the durable id of the owning App, so a user
# can be shown ONLY their own receipts. ``app_profile`` (a shared permission
# profile id) and ``api_key_name`` (a non-unique App name) are NOT safe owner
# keys; ``app_id`` is. Existing unattributed rows keep a NULL app_id so
# owner history survives without guessing a member identity.
SCHEMA_VERSION = 2

_AUDIENCES = ("internal", "admin", "client")
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "disconnected", "timed_out"})
_IMMUTABLE_ACCEPTED_FIELDS = frozenset(
    {
        "request_id", "protocol", "path", "intent", "app_profile", "app_id",
        "api_key_name", "requested_model", "stream",
        "effective_output_limit", "accepted_at_ms", "parent_request_id", "visibility", "owner_key_hash",
        "source_kind", "source_label", "client_hint",
        "attempt_number",
    }
)

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
    # Add columns before creating their indexes. Existing history stays intact;
    # a missing owning-App id remains NULL and is excluded from member feeds.
    # Inspect and add columns under one write lock: a second initializer waits,
    # then sees the columns instead of failing on a duplicate ALTER. The
    # CREATE ... IF NOT EXISTS script after the commit is safe to repeat.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);")
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        db_version = int(row[0]) if row is not None else None
        if db_version is not None and db_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"receipt_store schema version mismatch: db={db_version} "
                f"code={SCHEMA_VERSION} (downgrade not supported)"
            )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts)")}
        if columns:
            for column in ("protocol", "intent", "path", "app_profile", "app_id",
                           "api_key_name", "status", "job_id", "parent_request_id"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE receipts ADD COLUMN {column} TEXT")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM schema_version")  # one row, even after concurrent first runs
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()

    # This additive metadata survives retention changes without recreating v2
    # receipts. A pre-upgrade store may already have lost history to pruning;
    # do not promise coverage before its earliest survivor (or now if empty).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("""CREATE TABLE IF NOT EXISTS receipt_history (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        floor INTEGER NOT NULL
    )""")
    earliest = conn.execute("SELECT MIN(created_at) FROM receipts").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO receipt_history(singleton, floor) VALUES (1, ?)",
                 (earliest if earliest is not None else int(time.time()),))
    conn.commit()


def init_db(path: Path = DB_PATH) -> None:
    """Check existing data safely, then initialize the schema."""
    with store_startup(path):
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            # Fresh databases can reclaim pages without a full rebuild. Existing
            # files need the explicit offline compaction command to enable this.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            _apply_pragmas(conn)
            _exec_schema(conn)
        finally:
            conn.close()


@contextmanager
def _conn(path: Path = DB_PATH):
    conn = connect(path, timeout=5.0)
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


def _coerce_to_internal_dict(receipt_or_dict: Any) -> dict[str, Any]:
    if isinstance(receipt_or_dict, RouteReceipt):
        data = receipt_or_dict.to_dict("internal")
    elif isinstance(receipt_or_dict, Mapping):
        data = dict(receipt_or_dict)
    else:
        raise TypeError(
            f"receipt must be RouteReceipt or mapping, got {type(receipt_or_dict).__name__}"
        )
    if data.get("app_id") or data.get("owner_key_hash"):
        data["owner_key_hash"] = stored_owner_key(data.get("owner_key_hash"), app_id=data.get("app_id"))
    return data


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


def _assert_lifecycle_data(data: Mapping[str, Any], *, terminal: bool) -> None:
    _assert_safe(data)
    status = data.get("status")
    if terminal:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"receipt terminal status must be one of {sorted(_TERMINAL_STATUSES)}")
    elif status != "in_progress":
        raise ValueError("receipt begin status must be in_progress")
    cap = data.get("effective_output_limit")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        raise ValueError("receipt effective_output_limit must be a non-negative integer or None")
    for field in ("accepted_at_ms", "updated_at_ms"):
        value = data.get(field)
        if terminal and field == "accepted_at_ms" and value is None:
            # A concurrent/retried finalizer may reconstruct only terminal
            # facts; the accepted timestamp is restored from the begun row.
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"receipt {field} must be a non-negative integer")


def _row_values(data: Mapping[str, Any], *, created_at: int) -> tuple[Any, ...]:
    return (
        data["request_id"], created_at, data.get("protocol"), data.get("intent"),
        data.get("path"), data.get("app_profile"), data.get("app_id"),
        data.get("api_key_name"), data.get("status"), data.get("job_id"),
        data.get("parent_request_id"), json.dumps(data, ensure_ascii=False, sort_keys=True),
    )


def begin(
    receipt: RouteReceipt | Mapping[str, Any],
    *,
    now_ms: int | None = None,
    path: Path = DB_PATH,
) -> str:
    """Insert an accepted outer receipt once, without replacing an existing row."""
    if isinstance(receipt, RouteReceipt):
        receipt.begin(now_ms=now_ms)
    data = _coerce_to_internal_dict(receipt)
    if not isinstance(receipt, RouteReceipt):
        timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
        data = dict(data)
        data["status"] = "in_progress"
        data.setdefault("accepted_at_ms", timestamp)
        data.setdefault("updated_at_ms", timestamp)
        data.setdefault("attempt_number", 1)
    _assert_lifecycle_data(data, terminal=False)
    created_at = int(data["accepted_at_ms"] // 1000)
    with _conn(path) as conn:
        conn.execute(
            "INSERT INTO receipts (request_id, created_at, protocol, intent, path, app_profile, "
            "app_id, api_key_name, status, job_id, parent_request_id, receipt_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO NOTHING",
            _row_values(data, created_at=created_at),
        )
        conn.commit()
    return str(data["request_id"])


def update_phases(receipt: RouteReceipt, *, path: Path = DB_PATH) -> None:
    """Checkpoint observed phases without replacing concurrent terminal facts."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT receipt_json FROM receipts WHERE request_id=? AND status='in_progress'",
                           (receipt.request_id,)).fetchone()
        if row is None:
            return
        data = json.loads(row[0])
        if data.get("attempt_number") is None:
            return
        for name in PHASE_FIELDS:
            if data.get(name) is None:
                data[name] = getattr(receipt, name)
        conn.execute("UPDATE receipts SET receipt_json=? WHERE request_id=?",
                     (json.dumps(data, ensure_ascii=False, sort_keys=True), receipt.request_id))
        conn.commit()


def finalize(
    receipt: RouteReceipt | Mapping[str, Any],
    *,
    now_ms: int | None = None,
    path: Path = DB_PATH,
) -> bool:
    """CAS-terminalize one begun receipt; terminal duplicates are no-ops."""
    if isinstance(receipt, RouteReceipt):
        if receipt.status not in _TERMINAL_STATUSES:
            raise ValueError("finish receipt with a terminal status before finalizing")
        receipt.updated_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    data = _coerce_to_internal_dict(receipt)
    _assert_lifecycle_data(data, terminal=True)
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT created_at, receipt_json, status FROM receipts WHERE request_id = ?",
            (data["request_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("cannot finalize receipt that was not begun")
        accepted = _safe_loads(row[1])
        if not isinstance(accepted, Mapping):
            raise ValueError("cannot finalize malformed stored receipt")
        merged = dict(data)
        for field in _IMMUTABLE_ACCEPTED_FIELDS:
            if field in accepted:
                merged[field] = accepted[field]
        for field in PHASE_FIELDS:
            if accepted.get(field) is not None:
                merged[field] = accepted[field]
        if accepted.get("attempt_number") is None:
            # A legacy in-progress row has no trustworthy attempt boundary.
            merged.update(dict.fromkeys((*PHASE_FIELDS, "attempt_number")))
        if merged.get("attempt_number") is not None and merged.get("finished_at_ms") is None:
            merged["finished_at_ms"] = data["updated_at_ms"]
        cost = accepted.get("cost")
        if isinstance(cost, Mapping) and cost.get("reconciliation_status") == "settled":
            # A reconciled cost recorded before the terminal write outranks the caller's estimate.
            merged = _with_cost(merged, cost)
        _assert_lifecycle_data(merged, terminal=True)
        cur = conn.execute(
            "UPDATE receipts SET protocol=?, intent=?, path=?, app_profile=?, app_id=?, api_key_name=?, "
            "status=?, job_id=?, parent_request_id=?, receipt_json=? "
            "WHERE request_id=? AND status='in_progress'",
            (
                merged.get("protocol"), merged.get("intent"), merged.get("path"),
                merged.get("app_profile"), merged.get("app_id"), merged.get("api_key_name"),
                merged["status"], merged.get("job_id"), merged.get("parent_request_id"),
                json.dumps(merged, ensure_ascii=False, sort_keys=True), merged["request_id"],
            ),
        )
        conn.commit()
    return cur.rowcount == 1


def _filter_for_audience(data: Mapping[str, Any], audience: str) -> dict[str, Any]:
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of {_AUDIENCES}")
    return serialize_receipt_for_audience(_with_source(data), audience)


def _with_source(data: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(data)
    app_id = out.get("app_id")
    source_kind = out.get("source_kind")
    if isinstance(app_id, str) and app_id:
        out["source_kind"] = str(source_kind or "app")
        if not out.get("source_label"):
            out["source_label"] = out.get("api_key_name") or "App"
    elif not source_kind:
        out["source_kind"] = "local-network"
        if not out.get("source_label"):
            out["source_label"] = "Local network · no key"
    label = out.get("source_label")
    out["source"] = {
        "kind": str(out.get("source_kind") or "unknown"),
        "label": str(label or "Unknown source"),
        "app_id": app_id if isinstance(app_id, str) and app_id else None,
        "client_hint": out.get("client_hint") if isinstance(out.get("client_hint"), str) else None,
    }
    return out


def _project_incomplete(
    data: Mapping[str, Any],
    *,
    now_ms: int | None,
    stale_after_ms: int,
) -> dict[str, Any]:
    """Render stale accepted work honestly without writing a guessed outcome."""
    out = dict(data)
    if out.get("status") != "in_progress":
        return out
    updated_at_ms = out.get("updated_at_ms")
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if (
        isinstance(updated_at_ms, int)
        and not isinstance(updated_at_ms, bool)
        and current_ms - updated_at_ms >= stale_after_ms
    ):
        out["status"] = "stale_incomplete"
    return out


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


def update_cost(
    request_id: str,
    cost: Mapping[str, Any],
    *,
    path: Path = DB_PATH,
) -> bool:
    """Replace the cost block on an existing persisted receipt."""

    if not request_id:
        raise ValueError("request_id is required")
    if not isinstance(cost, Mapping):
        raise TypeError("cost must be a mapping")
    assert_safe_mapping("receipt.cost", cost)
    if not path.exists():
        return False
    with _conn(path) as conn:
        # Read under the write lock, as finalize and update_phases do, so a
        # terminal write cannot land between this read and the update.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT receipt_json FROM receipts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        data = _safe_loads(row[0])
        if not isinstance(data, Mapping):
            conn.rollback()
            raise ValueError("cannot update malformed stored receipt")
        updated = _with_cost(data, cost)
        _assert_safe(updated)
        conn.execute(
            "UPDATE receipts SET receipt_json = ? WHERE request_id = ?",
            (json.dumps(updated, ensure_ascii=False, sort_keys=True), request_id),
        )
        conn.commit()
        return True


def _with_cost(data: Mapping[str, Any], cost: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``data`` with a reconciled cost block; only cost-owned fields change."""
    updated = dict(data)
    updated["remote"] = True
    updated["cost"] = dict(cost)
    metadata = updated.get("metadata")
    if isinstance(metadata, Mapping):
        metadata = dict(metadata)
        decisions = metadata.get("decisions")
        if isinstance(decisions, Mapping):
            decisions = dict(decisions)
            decisions["cost_usd"] = cost.get("usd")
            metadata["decisions"] = decisions
            updated["metadata"] = metadata
    return updated


def get(
    request_id: str,
    *,
    audience: str = "internal",
    now_ms: int | None = None,
    stale_after_ms: int = STALE_AFTER_MS,
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
    out = _filter_for_audience(
        _project_incomplete(data, now_ms=now_ms, stale_after_ms=stale_after_ms), audience
    )
    out.setdefault("_stored_at", int(row[1]))
    return out


def recent(
    limit: int = 50,
    *,
    audience: str = "internal",
    app_profile: str | None = None,
    app_ids: list[str] | None = None,
    owner_key_hashes: list[str] | None = None,
    include_null_app_id: bool = False,
    since_s: int | None = None,
    outer_only: bool = True,
    now_ms: int | None = None,
    stale_after_ms: int = STALE_AFTER_MS,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` most-recent receipts, newest first.

    Optional filters: ``app_profile`` (exact match), ``app_ids`` (member-scope:
    return only receipts whose ``app_id`` is in this allow-list), and
    ``since_s`` (only receipts written within the last N seconds).

    ``app_ids`` is a positive membership filter. An empty list returns no rows
    unless ``include_null_app_id`` is set for an owner-only instance feed.
    Scoped member views must leave ``include_null_app_id`` false so NULL-app
    receipts are never attributed to a person.
    """
    if limit <= 0:
        return []
    if (
        app_ids is not None or owner_key_hashes is not None
    ) and not app_ids and not owner_key_hashes and not include_null_app_id:
        # The caller explicitly owns no Apps — never fall through to an
        # unfiltered query (that would leak everyone's receipts).
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if app_profile is not None:
        clauses.append("app_profile = ?")
        params.append(app_profile)
    if app_ids is not None or owner_key_hashes is not None:
        ownership = []
        if app_ids:
            ownership.append(f"app_id IN ({','.join('?' for _ in app_ids)})")
            params.extend(app_ids)
        if owner_key_hashes:
            ownership.append(f"json_extract(receipt_json, '$.owner_key_hash') IN ({','.join('?' for _ in owner_key_hashes)})")
            params.extend(owner_key_hashes)
        if include_null_app_id:
            ownership.append("app_id IS NULL")
        clauses.append("(" + " OR ".join(ownership) + ")")
    if since_s is not None:
        clauses.append("created_at >= ?")
        params.append(int(time.time()) - int(since_s))
    if outer_only:
        clauses.append("parent_request_id IS NULL")
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
        filtered = _filter_for_audience(
            _project_incomplete(data, now_ms=now_ms, stale_after_ms=stale_after_ms), audience
        )
        filtered.setdefault("_stored_at", int(created_at))
        out.append(filtered)
    return out


def retention_days(admin_path: Path) -> int | None:
    """Read the owner choice on each maintenance run, including after restart."""
    from gateway import admin_store

    record = admin_store.get_operator_setting(RETENTION_SETTING_KEY, path=admin_path)
    days = record["value"].get("retention_days") if record else None
    if (days is not None and type(days) is not int) or days not in RETENTION_DAYS:
        raise ValueError("invalid receipt retention setting")
    return days


def prune(retain_s: int | None = RETAIN_S, path: Path = DB_PATH) -> int:
    """Delete expired history only when a finite window is explicitly supplied."""
    if retain_s is None:
        return 0
    cutoff = int(time.time()) - int(retain_s)
    with _conn(path) as conn:
        deleted = _delete_before(conn, cutoff)
        conn.commit()
        return deleted


def history_floor(path: Path = DB_PATH) -> int:
    """Earliest known-complete receipt history; persisted independently of expiry."""
    with _conn(path) as conn:
        return conn.execute("SELECT floor FROM receipt_history WHERE singleton = 1").fetchone()[0]


def _delete_before(conn: sqlite3.Connection, cutoff: int | None) -> int:
    """Advance coverage in the same transaction as deletion, never backward."""
    deleted = conn.execute("DELETE FROM receipts WHERE created_at < ?", (cutoff,)).rowcount
    if deleted:
        conn.execute("UPDATE receipt_history SET floor = MAX(floor, ?) WHERE singleton = 1",
                     (cutoff,))
    return deleted


def retention_preview(days: int | None, *, path: Path = DB_PATH) -> dict[str, Any]:
    cutoff = int(time.time()) - days * 86400 if days is not None else None
    with _conn(path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM receipts WHERE created_at < ?", (cutoff,)).fetchone()[0]
    return {"cutoff": cutoff, "delete_count": count}


class RetentionPreviewChanged(ValueError):
    """The owner must see a fresh count before saving."""


def save_retention(
    days: int | None, preview: Mapping[str, Any] | None,
    save_setting: Callable[[], Any], *, path: Path = DB_PATH,
) -> int:
    """Validate the preview under a write lock, save the choice, then delete.

    Holding the receipt lock also serializes hourly maintenance with saves.
    The two files cannot commit atomically: if deletion fails after the setting
    commits, maintenance retries the persisted choice at startup or hourly.
    """
    cutoff = None
    if days is not None:
        if not isinstance(preview, Mapping) or set(preview) != {"cutoff", "delete_count"}:
            raise RetentionPreviewChanged("Preview receipt deletion before saving.")
        cutoff = preview["cutoff"]
        count = preview["delete_count"]
        latest = int(time.time()) - days * 86400
        if (type(cutoff) is not int or type(count) is not int or count < 0
                or not latest - 900 <= cutoff <= latest):
            raise RetentionPreviewChanged("Preview expired. Review the new deletion count.")
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if cutoff is not None:
            count = conn.execute("SELECT COUNT(*) FROM receipts WHERE created_at < ?", (cutoff,)).fetchone()[0]
            if count != preview["delete_count"]:
                raise RetentionPreviewChanged("Receipt count changed. Review the new deletion count.")
        save_setting()
        deleted = _delete_before(conn, cutoff)
        conn.commit()
        return deleted


def maintain(admin_path: Path, *, path: Path = DB_PATH) -> int:
    """Apply saved expiry and reclaim at most 256 free pages off request paths."""
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        days = retention_days(admin_path)
        cutoff = int(time.time()) - days * 86400 if days is not None else None
        deleted = _delete_before(conn, cutoff)
        conn.commit()
        conn.execute("PRAGMA incremental_vacuum(256)").fetchall()
        return deleted


def compact(path: Path) -> dict[str, int | bool]:
    """Explicit offline maintenance; never called by the gateway or its API.

    SQLite holds the necessary locks during the rebuild, so concurrent writers
    wait or report busy rather than losing rows. Schedule during quiet hours.
    """
    if not path.is_file():
        raise ValueError("receipt database does not exist")
    with _conn(path) as conn:
        before = (conn.execute("PRAGMA page_count").fetchone()[0]
                  * conn.execute("PRAGMA page_size").fetchone()[0])
        before_files = _file_bytes(path)
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
        after = (conn.execute("PRAGMA page_count").fetchone()[0]
                 * conn.execute("PRAGMA page_size").fetchone()[0])
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after_files = _file_bytes(path)
    # Keep the original logical byte fields for existing command consumers.
    return {"before_bytes": before, "after_bytes": after,
            "before_logical_bytes": before, "after_logical_bytes": after,
            **{f"before_{key}": value for key, value in before_files.items()},
            **{f"after_{key}": value for key, value in after_files.items()},
            "checkpoint_pending": bool(checkpoint[0])}


def _file_bytes(path: Path) -> dict[str, int]:
    main = path.stat().st_size
    try:
        wal = Path(str(path) + "-wal").stat().st_size
    except FileNotFoundError:
        wal = 0
    return {"main_bytes": main, "wal_bytes": wal, "physical_bytes": main + wal}


def count(path: Path = DB_PATH, *, outer_only: bool = True) -> int:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM receipts" + (" WHERE parent_request_id IS NULL" if outer_only else "")
        ).fetchone()
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
    "STALE_AFTER_MS",
    "SCHEMA_VERSION",
    "init_db",
    "begin",
    "finalize",
    "record",
    "get",
    "recent",
    "prune",
    "history_floor",
    "retention_days",
    "retention_preview",
    "save_retention",
    "maintain",
    "compact",
    "count",
    "schema_version",
]
