"""SQLite-backed persistence for the OpenAI Responses API.

The Responses API supports server-side conversation state. When a client
sends `store: true` (default) the response is persisted so a later request
with `previous_response_id` can chain off it. We also need this for
GET/DELETE /v1/responses/{id}.

Layout mirrors metrics_store.py: stdlib sqlite3, WAL mode, integrity check
on startup, short-lived reader connections, one long-lived writer is
optional but not required (this store sees << 1 write/sec).

Retention: 7 days. A background pruner deletes older rows.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path(__file__).parent / "data" / "responses.db"

# Retention window for stored responses (seconds).
RETAIN_S = 7 * 24 * 3600

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  model TEXT,
  previous_response_id TEXT,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  api_key_name TEXT
);
CREATE INDEX IF NOT EXISTS responses_created_at ON responses(created_at);
CREATE INDEX IF NOT EXISTS responses_previous ON responses(previous_response_id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")


def _exec_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
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
            print(f"[responses_store] DB corrupt ({e}); moving aside to {broken}", flush=True)
            try:
                shutil.move(str(path), str(broken))
            except Exception as mv_err:  # noqa: BLE001
                print(f"[responses_store] failed to move broken db: {mv_err}", flush=True)
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


def save(
    response_id: str,
    *,
    created_at: int,
    model: Optional[str],
    previous_response_id: Optional[str],
    request_json: dict,
    response_json: dict,
    api_key_name: Optional[str] = None,
    path: Path = DB_PATH,
) -> None:
    """Persist a response. INSERT OR REPLACE so retries are idempotent."""
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO responses "
            "(id, created_at, model, previous_response_id, request_json, response_json, api_key_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                response_id,
                int(created_at),
                model,
                previous_response_id,
                json.dumps(request_json, ensure_ascii=False),
                json.dumps(response_json, ensure_ascii=False),
                api_key_name,
            ),
        )
        conn.commit()


def get(response_id: str, path: Path = DB_PATH) -> Optional[dict]:
    """Return the stored row as a dict, or None."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT id, created_at, model, previous_response_id, request_json, response_json, api_key_name "
            "FROM responses WHERE id = ?",
            (response_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "created_at": row[1],
            "model": row[2],
            "previous_response_id": row[3],
            "request_json": _safe_loads(row[4]),
            "response_json": _safe_loads(row[5]),
            "api_key_name": row[6],
        }


def delete(response_id: str, path: Path = DB_PATH) -> bool:
    """Delete a stored response. Returns True if the row existed."""
    with _conn(path) as conn:
        cur = conn.execute("DELETE FROM responses WHERE id = ?", (response_id,))
        conn.commit()
        return cur.rowcount > 0


def walk_chain(
    response_id: str,
    *,
    max_depth: int = 100,
    path: Path = DB_PATH,
) -> list[dict]:
    """Walk previous_response_id pointers oldest-first.

    Returns list of stored rows ordered from earliest ancestor to the
    given id (inclusive). Stops at max_depth or when a pointer dangles.
    """
    chain: list[dict] = []
    cur_id: Optional[str] = response_id
    seen: set[str] = set()
    while cur_id and cur_id not in seen and len(chain) < max_depth:
        seen.add(cur_id)
        row = get(cur_id, path=path)
        if row is None:
            break
        chain.append(row)
        cur_id = row.get("previous_response_id")
    chain.reverse()
    return chain


def prune(retain_s: int = RETAIN_S, path: Path = DB_PATH) -> int:
    """Delete rows older than the retention window. Returns deleted count."""
    cutoff = int(time.time()) - int(retain_s)
    with _conn(path) as conn:
        cur = conn.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def count(path: Path = DB_PATH) -> int:
    with _conn(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return int(row[0]) if row else 0


def _safe_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None
