"""SQLite-backed persistence for the OpenAI Responses API.

The Responses API supports server-side conversation state. The runtime
defaults to `store: true` for people whose prompt memory is enabled;
callers without a person must opt in and have a stable credential scope.
Stored responses let a later request with `previous_response_id` chain off
the conversation. We also need this for
GET/DELETE /v1/responses/{id}.

Layout mirrors metrics_store.py: stdlib sqlite3, WAL mode, integrity check
on startup, short-lived reader connections, one long-lived writer is
optional but not required (this store sees << 1 write/sec).

Saved conversations remain until explicit deletion or a privacy erasure request.
"""

from __future__ import annotations

import json
from gateway.compat import get_env
from gateway.sqlite_startup import connect, store_startup
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

_default_db = Path(__file__).parent / "data" / "responses.db"
DB_PATH = Path(get_env("AZIZAM_RESPONSES_DB", _default_db))

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  model TEXT,
  previous_response_id TEXT,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  api_key_name TEXT,
  person_id TEXT,
  app_id TEXT,
  owner_key_hash TEXT
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(responses)")}
    for column in ("person_id", "app_id", "owner_key_hash"):
        if column not in columns:
            conn.execute(f"ALTER TABLE responses ADD COLUMN {column} TEXT")
    # Older rows have no trustworthy identity. Leave their scope unset; the
    # runtime hides them instead of guessing ownership from display names.
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
    conn.commit()


def init_db(path: Path = DB_PATH) -> None:
    """Check existing data safely, then initialize the schema."""
    with store_startup(path):
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
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


def save(
    response_id: str,
    *,
    created_at: int,
    model: Optional[str],
    previous_response_id: Optional[str],
    request_json: dict,
    response_json: dict,
    api_key_name: Optional[str] = None,
    person_id: Optional[str] = None,
    app_id: Optional[str] = None,
    owner_key_hash: Optional[str] = None,
    path: Path = DB_PATH,
) -> None:
    """Persist a response. INSERT OR REPLACE so retries are idempotent."""
    encoded_request = json.dumps(request_json, ensure_ascii=False)
    encoded_response = json.dumps(response_json, ensure_ascii=False)
    if not any(isinstance(value, str) and value.strip() for value in (app_id, owner_key_hash)):
        raise ValueError("Saved responses require a stable App or credential scope")
    with _conn(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO responses "
            "(id, created_at, model, previous_response_id, request_json, response_json, api_key_name, "
            "person_id, app_id, owner_key_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                response_id,
                int(created_at),
                model,
                previous_response_id,
                encoded_request,
                encoded_response,
                api_key_name,
                person_id,
                app_id,
                owner_key_hash,
            ),
        )
        conn.commit()


def get(response_id: str, path: Path = DB_PATH) -> Optional[dict]:
    """Return the stored row as a dict, or None."""
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT id, created_at, model, previous_response_id, request_json, response_json, api_key_name, "
            "person_id, app_id, owner_key_hash "
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
            "person_id": row[7],
            "app_id": row[8],
            "owner_key_hash": row[9],
        }


def delete(response_id: str, path: Path = DB_PATH) -> bool:
    """Delete a stored response. Returns True if the row existed."""
    with _conn(path) as conn:
        cur = conn.execute("DELETE FROM responses WHERE id = ?", (response_id,))
        conn.commit()
        return cur.rowcount > 0


def delete_all(path: Path = DB_PATH) -> int:
    """Discard stored conversations when the household disables prompt retention."""
    with _conn(path) as conn:
        conn.execute("PRAGMA secure_delete=ON")
        cur = conn.execute("DELETE FROM responses")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cur.rowcount


def delete_all_before(cutoff: int, path: Path = DB_PATH) -> int:
    with _conn(path) as conn:
        conn.execute("PRAGMA secure_delete=ON")
        cur = conn.execute("DELETE FROM responses WHERE created_at<=?", (int(cutoff),))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cur.rowcount


def delete_person(person_id: str, path: Path = DB_PATH) -> int:
    """Discard one person's stored conversations without removing media."""
    return _delete_person_rows(person_id, None, path)


def delete_person_before(person_id: str, cutoff: int, path: Path = DB_PATH) -> int:
    """Discard this person's conversations started at or before an erasure."""
    return _delete_person_rows(person_id, cutoff, path)


def _delete_person_rows(person_id: str, cutoff: int | None, path: Path) -> int:
    if not Path(path).exists():
        return 0
    with _conn(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(responses)")}
        if "person_id" not in columns:
            # An absent store has no conversations. Pre-scope legacy rows
            # have no trustworthy person and remain invisible until pruning.
            return 0
        conn.execute("PRAGMA secure_delete=ON")
        if cutoff is None:
            cur = conn.execute("DELETE FROM responses WHERE person_id=?", (person_id,))
        else:
            cur = conn.execute(
                "DELETE FROM responses WHERE person_id=? AND created_at<=?", (person_id, cutoff),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cur.rowcount


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


def count(path: Path = DB_PATH) -> int:
    with _conn(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return int(row[0]) if row else 0


def _safe_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None
