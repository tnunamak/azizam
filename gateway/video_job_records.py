"""Durable store for ``/v1/videos`` job records.

``proxy.py`` keeps one small record per video job (model, size,
owner key hash, tombstone, extension lineage). Prompt text lives only in the
maker-scoped asset prompt table. This record used to live only in process
memory, so a restart lost the owner of every video. That made old ids readable
by any API key, and it lost the source an extension needs.

The records live in the media-jobs SQLite file (``media_jobs.DB_PATH``) in an
additive table. The migration only adds objects (``CREATE TABLE IF NOT EXISTS``)
and does not change ``media_jobs_schema_version``, so an older gateway build can
still open the same file after a rollback. It ignores the new table.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from gateway import media_jobs
from gateway.ownership import stored_owner_key
from gateway.sqlite_startup import connect

MIGRATION_NAME = "video_job_records_v1"

_DDL = """
CREATE TABLE IF NOT EXISTS media_jobs_migrations (
  name TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS video_job_records (
  prompt_id TEXT PRIMARY KEY,
  owner_key_hash TEXT,
  created_at INTEGER NOT NULL,
  deleted_at INTEGER,
  record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS video_job_records_created_at ON video_job_records(created_at);
"""


def migrate(conn: sqlite3.Connection) -> bool:
    """Apply the migration. Return True when it ran, False when already applied."""
    conn.executescript(_DDL)
    row = conn.execute(
        "SELECT 1 FROM media_jobs_migrations WHERE name = ?", (MIGRATION_NAME,)
    ).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO media_jobs_migrations(name, applied_at) VALUES (?, ?)",
        (MIGRATION_NAME, int(time.time())),
    )
    conn.commit()
    return True


def _connect(path: Path | None) -> sqlite3.Connection:
    target = Path(path) if path else Path(media_jobs.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target, timeout=5.0)
    media_jobs._apply_pragmas(conn)
    conn.execute("PRAGMA secure_delete=ON")
    migrate(conn)
    return conn


def save(prompt_id: str, record: Mapping[str, Any], *, path: Path | None = None) -> None:
    record = dict(record)
    record.pop("prompt", None)
    record["owner_key_hash"] = stored_owner_key(record.get("owner_key_hash"), app_id=record.get("app_id"))
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO video_job_records "
            "(prompt_id, owner_key_hash, created_at, deleted_at, record_json) VALUES (?, ?, ?, ?, ?)",
            (
                prompt_id,
                record.get("owner_key_hash"),
                int(record.get("created_at") or time.time()),
                record.get("deleted_at"),
                json.dumps(dict(record), ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def remove(prompt_id: str, *, path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM video_job_records WHERE prompt_id = ?", (prompt_id,))
        conn.commit()
    finally:
        conn.close()


def load_all(*, path: Path | None = None) -> dict[str, dict]:
    """Return every stored record, oldest first."""
    conn = _connect(path)
    try:
        # Remove only the legacy prompt from the current JSON in one statement;
        # rewriting a snapshot read earlier could undo a concurrent save.
        conn.execute(
            "UPDATE video_job_records SET record_json = json_remove(record_json, '$.prompt') "
            "WHERE json_valid(record_json) AND json_type(record_json) = 'object' "
            "AND json_type(record_json, '$.prompt') IS NOT NULL"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT prompt_id, record_json, created_at FROM video_job_records ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for prompt_id, blob, created_at in rows:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if isinstance(data, dict):
            data.pop("prompt", None)
            out[prompt_id] = data
    return out
