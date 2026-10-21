"""SQLite-backed metrics persistence layer.

Single tag-based samples table with two pre-aggregated rollups (1min, 1hour).
Stdlib sqlite3 only. WAL mode for concurrent read while one writer appends.

Layout:
    samples         - raw 10s samples, kept 7 days once covered by hourly history
    samples_1min    - per-minute rollup, kept 30 days once rolled into samples_1hour
    samples_1hour   - per-hour rollup, no automatic expiry

``rolled = 1`` marks a raw or minute row whose values a coarser rollup row
holds. Rollups set it in the transaction that writes the summary, and pruning
deletes only marked rows, so a missed or failed rollup never loses data.

All public functions accept an optional connection. When omitted, a short-lived
read connection is auto-opened. ``gateway.metrics_writer`` owns the long-lived writer on a dedicated thread.
Direct callers of ``open_writer()`` must keep their connection on one thread.
"""

from __future__ import annotations

import json
from gateway.compat import get_env
from gateway.sqlite_startup import connect, connect_readonly, is_corruption, store_startup
import shutil
import sqlite3
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

_default_db = Path(__file__).parent / "data" / "metrics.db"
DB_PATH = Path(get_env("AZIZAM_METRICS_DB", _default_db))

# Retention windows (seconds). Only rows marked rolled expire; hourly rollups
# are the history and have no window.
RAW_RETAIN_S = 7 * 24 * 3600
MINUTE_RETAIN_S = 30 * 24 * 3600

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER NOT NULL,
  metric TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL,
  rolled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS samples_metric_ts ON samples(metric, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
-- Exact-tag latency percentiles need a different access path from metric/time
-- telemetry. Keep this covering index small by excluding unrelated samples.
CREATE INDEX IF NOT EXISTS samples_latency_tags_ts ON samples(tags, ts, value)
  WHERE metric = 'service_latency_ms';

CREATE TABLE IF NOT EXISTS samples_1min (
  ts INTEGER NOT NULL,
  metric TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  avg_value REAL NOT NULL,
  min_value REAL NOT NULL,
  max_value REAL NOT NULL,
  count INTEGER NOT NULL,
  rolled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS samples_1min_metric_ts ON samples_1min(metric, ts);
CREATE INDEX IF NOT EXISTS samples_1min_ts ON samples_1min(ts);
CREATE INDEX IF NOT EXISTS samples_unrolled_ts ON samples(ts) WHERE rolled = 0;
CREATE INDEX IF NOT EXISTS samples_1min_unrolled_ts ON samples_1min(ts) WHERE rolled = 0;

CREATE TABLE IF NOT EXISTS samples_1hour (
  ts INTEGER NOT NULL,
  metric TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  avg_value REAL NOT NULL,
  min_value REAL NOT NULL,
  max_value REAL NOT NULL,
  count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_1hour_metric_ts ON samples_1hour(metric, ts);
CREATE INDEX IF NOT EXISTS samples_1hour_ts ON samples_1hour(ts);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- Sampler positions committed in the same transaction as the samples they
-- produced (e.g. the kernel-journal cursor NVKMS failures were counted up to).
CREATE TABLE IF NOT EXISTS sampler_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")


def _add_rollup_marks(conn: sqlite3.Connection) -> None:
    """Quarantine unverifiable pre-v6 coverage instead of guessing it.

    rolled = -1 retains legacy fine rows without appending them to existing
    summaries or pruning them. Even a matching coarse bucket cannot establish
    which observations it covers, especially after partial legacy pruning.
    Reconciliation requires evidence this store does not have. New rows use 0.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        legacy = row is None or row[0] < SCHEMA_VERSION
        for table in ("samples", "samples_1min"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns:
                continue
            if "rolled" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN rolled INTEGER NOT NULL DEFAULT 0")
            if legacy:
                conn.execute(f"UPDATE {table} SET rolled = -1")
        # Commit the version with quarantine so retry never freezes new rows.
        if row is None:
            conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        elif legacy:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _exec_schema(conn: sqlite3.Connection) -> None:
    _add_rollup_marks(conn)
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def _integrity_marker(path: Path) -> Path:
    return path.with_name(f"{path.name}.integrity-failed")


def init_db(path: Path = DB_PATH) -> None:
    """Create the data dir and run the schema; quarantine the file if it is corrupt.

    Startup checks only the header/schema; new indexes are built once during schema migration.
    Schema statements fail on a file whose header or schema pages are broken. The full scan
    (``PRAGMA integrity_check``, ~12 s on a 346 MB file) is ``verify_integrity``, which the caller
    runs in a background thread. When that scan finds damage it leaves a marker file; the next
    ``init_db`` sees the marker, rechecks it, and preserves confirmed damage before replacing it.
    """
    marker = _integrity_marker(path)
    # Recheck a previous failure before acting on it; the marker can be stale.
    with store_startup(path, full_check=marker.exists()):
        marker.unlink(missing_ok=True)
        from gateway import private_io
        private_io.prepare_private_database(path)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            _apply_pragmas(conn)
            _exec_schema(conn)
        finally:
            conn.close()


def verify_integrity(path: Path = DB_PATH) -> Optional[str]:
    """Run the full ``PRAGMA integrity_check``. Slow: call it off the event loop.

    Returns None when the file is sound, else the first problem found. A failure also writes
    ``<db>.integrity-failed`` so the next ``init_db`` rechecks it and quarantines it if still corrupt. The live file is never
    swapped here: the sampler holds a writer connection to it.
    """
    try:
        conn = connect_readonly(path, timeout=5.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        problem = None if row and row[0] == "ok" else f"integrity_check returned: {row[0] if row else 'empty'}"
    except sqlite3.OperationalError:
        return None  # a busy or missing file is not evidence of damage
    except sqlite3.DatabaseError as e:
        if not is_corruption(e):
            raise
        problem = str(e)
    if problem is not None:
        print(f"[metrics_store] {problem}; it will be moved aside at the next start", flush=True)
        try:
            _integrity_marker(path).write_text(problem)
        except OSError as e:
            print(f"[metrics_store] could not write integrity marker: {e}", flush=True)
    return problem


def open_writer(path: Optional[Path] = None) -> sqlite3.Connection:
    """Long-lived writer connection. Caller owns it; close on shutdown."""
    conn = connect(path if path is not None else DB_PATH, timeout=5.0, isolation_level=None)
    _apply_pragmas(conn)
    return conn


@contextmanager
def reader(path: Optional[Path] = None):
    """Short-lived read connection. WAL makes this cheap."""
    conn = connect(path if path is not None else DB_PATH, timeout=2.0)
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


def _serialize_tags(tags: Optional[dict]) -> str:
    if not tags:
        return ""
    # Sort keys for deterministic representation so identical tag sets always
    # serialize to identical strings (allows GROUP BY tags later).
    return json.dumps(tags, sort_keys=True, separators=(",", ":"))


def record_sample(
    conn: Optional[sqlite3.Connection],
    ts: int,
    metric: str,
    value: float,
    tags: Optional[dict] = None,
) -> None:
    """Insert one sample. Auto-opens a connection if none provided."""
    tags_json = _serialize_tags(tags)
    if conn is None:
        with reader() as rconn:
            rconn.execute(
                "INSERT INTO samples (ts, metric, tags, value) VALUES (?, ?, ?, ?)",
                (int(ts), metric, tags_json, float(value)),
            )
            rconn.commit()
        return
    conn.execute(
        "INSERT INTO samples (ts, metric, tags, value) VALUES (?, ?, ?, ?)",
        (int(ts), metric, tags_json, float(value)),
    )


def record_samples(conn: sqlite3.Connection, samples: Iterable[tuple]) -> None:
    """Batch insert. Format: (ts, metric, tags_json, value)."""
    conn.executemany(
        "INSERT INTO samples (ts, metric, tags, value) VALUES (?, ?, ?, ?)",
        samples,
    )


def write_sampler_state(conn: sqlite3.Connection, state: dict[str, str]) -> None:
    """Upsert sampler positions; call inside the transaction of their samples."""
    conn.executemany(
        "INSERT INTO sampler_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        state.items(),
    )


def read_sampler_state(path: Path, key: str) -> Optional[str]:
    """The committed value of one sampler position, or None."""
    with reader(path) as conn:
        row = conn.execute("SELECT value FROM sampler_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _matches_tags(tag_json: str, filt: Optional[dict]) -> bool:
    if not filt:
        return True
    if not tag_json:
        return False
    try:
        decoded = json.loads(tag_json)
    except Exception:
        return False
    return all(decoded.get(k) == v for k, v in filt.items())


def query_aggregate(
    metric: str,
    *,
    since_s: int,
    tags_filter: Optional[dict] = None,
    op: str = "avg",
    table: str = "samples",
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[float]:
    """Single aggregate over [now-since_s, now]. op in {avg, min, max, sum, count}.

    For pre-aggregated tables (samples_1min, samples_1hour) the rollup column
    matching ``op`` is used so the result is correct rather than an "avg of avgs".
    """
    cutoff = int(time.time()) - since_s
    op = op.lower()
    if op not in ("avg", "min", "max", "sum", "count"):
        raise ValueError(f"unsupported op: {op}")

    if table == "samples":
        col = {"avg": "AVG(value)", "min": "MIN(value)", "max": "MAX(value)",
               "sum": "SUM(value)", "count": "COUNT(*)"}[op]
    elif table in ("samples_1min", "samples_1hour"):
        col = {
            "avg": "SUM(avg_value*count)/SUM(count)",
            "min": "MIN(min_value)",
            "max": "MAX(max_value)",
            "sum": "SUM(avg_value*count)",
            "count": "SUM(count)",
        }[op]
    else:
        raise ValueError(f"unknown table: {table}")

    sql = f"SELECT {col} FROM {table} WHERE metric = ? AND ts >= ?"
    params: list[Any] = [metric, cutoff]
    # Rollups repeat a small set of tags across thousands of timestamps. Decode
    # each tag set once per query, bounded and isolated from other callers/filters.
    matches_tags = lru_cache(maxsize=1024)(lambda tags: _matches_tags(tags, tags_filter))
    if tags_filter is None:
        # No filter: SQL handles directly.
        def _run(c: sqlite3.Connection) -> Optional[float]:
            row = c.execute(sql, params).fetchone()
            return row[0] if row else None
    else:
        # We have to fetch matching rows and filter in Python because tags
        # is JSON. With sane retention this stays well under a million rows.
        sql_rows = (
            f"SELECT tags, value FROM {table} WHERE metric = ? AND ts >= ?"
            if table == "samples"
            else f"SELECT tags, avg_value, min_value, max_value, count FROM {table} "
                 f"WHERE metric = ? AND ts >= ?"
        )

        def _run(c: sqlite3.Connection) -> Optional[float]:
            rows = c.execute(sql_rows, params).fetchall()
            filtered = [r for r in rows if matches_tags(r[0])]
            if not filtered:
                return None
            if table == "samples":
                values = [r[1] for r in filtered]
                if op == "avg":
                    return sum(values) / len(values)
                if op == "min":
                    return min(values)
                if op == "max":
                    return max(values)
                if op == "sum":
                    return sum(values)
                if op == "count":
                    return float(len(values))
            else:
                if op == "avg":
                    total = sum(r[1] * r[4] for r in filtered)
                    cnt = sum(r[4] for r in filtered)
                    return total / cnt if cnt else None
                if op == "min":
                    return min(r[2] for r in filtered)
                if op == "max":
                    return max(r[3] for r in filtered)
                if op == "sum":
                    return sum(r[1] * r[4] for r in filtered)
                if op == "count":
                    return float(sum(r[4] for r in filtered))
            return None

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


# op -> SQL aggregate for the unfiltered raw path; an unknown op falls back to AVG
# exactly as the tag-filtered path does.
_RAW_SQL_AGGREGATES = {"avg": "AVG", "min": "MIN", "max": "MAX", "sum": "SUM", "count": "COUNT"}


def query_series(
    metric: str,
    *,
    since_s: int,
    points: int = 60,
    tags_filter: Optional[dict] = None,
    table: str = "samples",
    op: str = "avg",
    conn: Optional[sqlite3.Connection] = None,
) -> list[tuple[int, float]]:
    """Down-sample to N points for sparklines. Returns [(ts, value), ...]."""
    if points <= 0:
        return []
    if table not in ("samples", "samples_1min", "samples_1hour"):
        raise ValueError(f"unknown samples table: {table!r}")
    now = int(time.time())
    cutoff = now - since_s
    bucket_s = max(1, since_s // points)
    matches_tags = lru_cache(maxsize=1024)(lambda tags: _matches_tags(tags, tags_filter))

    if table == "samples":
        if tags_filter is None:
            agg = _RAW_SQL_AGGREGATES.get(op, "AVG")
            sql = (
                f"SELECT (ts/?)*?, {agg}(value) FROM samples "
                "WHERE metric = ? AND ts >= ? "
                "GROUP BY ts/? ORDER BY 1"
            )

            def _run(c: sqlite3.Connection) -> list[tuple[int, float]]:
                rows = c.execute(sql, (bucket_s, bucket_s, metric, cutoff, bucket_s)).fetchall()
                return [(int(r[0]), float(r[1])) for r in rows if r[1] is not None]
        else:
            def _run(c: sqlite3.Connection) -> list[tuple[int, float]]:
                rows = c.execute(
                    "SELECT ts, tags, value FROM samples WHERE metric = ? AND ts >= ?",
                    (metric, cutoff),
                ).fetchall()
                buckets: dict[int, list[float]] = {}
                for ts, tag_json, v in rows:
                    if not matches_tags(tag_json):
                        continue
                    bk = (ts // bucket_s) * bucket_s
                    buckets.setdefault(bk, []).append(v)
                if op == "max":
                    return sorted((bk, max(vals)) for bk, vals in buckets.items())
                if op == "min":
                    return sorted((bk, min(vals)) for bk, vals in buckets.items())
                if op == "sum":
                    return sorted((bk, sum(vals)) for bk, vals in buckets.items())
                if op == "count":
                    return sorted((bk, float(len(vals))) for bk, vals in buckets.items())
                return sorted(
                    (bk, sum(vals) / len(vals)) for bk, vals in buckets.items()
                )
    else:
        # 1min / 1hour rollup: weight by count for proper averages.
        def _run(c: sqlite3.Connection) -> list[tuple[int, float]]:
            rows = c.execute(
                f"SELECT ts, tags, avg_value, min_value, max_value, count "
                f"FROM {table} WHERE metric = ? AND ts >= ?",
                (metric, cutoff),
            ).fetchall()
            buckets: dict[int, list[tuple[float, float, float, int]]] = {}
            for ts, tag_json, avg_v, mn_v, mx_v, cnt in rows:
                if not matches_tags(tag_json):
                    continue
                bk = (ts // bucket_s) * bucket_s
                buckets.setdefault(bk, []).append((avg_v, mn_v, mx_v, cnt))
            out: list[tuple[int, float]] = []
            for bk, vals in sorted(buckets.items()):
                if op == "max":
                    out.append((bk, max(v[2] for v in vals)))
                elif op == "min":
                    out.append((bk, min(v[1] for v in vals)))
                elif op == "sum":
                    out.append((bk, sum(v[0] * v[3] for v in vals)))
                elif op == "count":
                    out.append((bk, float(sum(v[3] for v in vals))))
                else:
                    total = sum(v[0] * v[3] for v in vals)
                    cnt = sum(v[3] for v in vals)
                    if cnt:
                        out.append((bk, total / cnt))
            return out

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def _roll_up(c: sqlite3.Connection, source: str, target: str, width: int, before: int) -> int:
    """Atomically summarize and mark unrolled rows, including late arrivals.

    Own a write transaction on autocommit connections. Inside a caller's
    transaction, a savepoint isolates failure without committing caller work.
    Legacy rows with unknown coverage (-1) require explicit reconciliation.
    """
    value = "AVG(value), MIN(value), MAX(value), COUNT(*)" if source == "samples" else (
        "SUM(avg_value*count)*1.0/NULLIF(SUM(count),0), MIN(min_value), MAX(max_value), SUM(count)")
    owned = not c.in_transaction
    if owned:
        c.execute("BEGIN IMMEDIATE")
    else:
        c.execute("SAVEPOINT metrics_rollup")
    try:
        cur = c.execute(
            f"INSERT INTO {target} (ts, metric, tags, avg_value, min_value, max_value, count) "
            f"SELECT ts - ts % {width}, metric, tags, {value} FROM {source} "
            f"WHERE rolled = 0 AND ts < ? GROUP BY ts - ts % {width}, metric, tags",
            (before,),
        )
        written = cur.rowcount or 0
        c.execute(f"UPDATE {source} SET rolled = 1 WHERE rolled = 0 AND ts < ?", (before,))
        if owned:
            c.commit()
        else:
            c.execute("RELEASE SAVEPOINT metrics_rollup")
        return written
    except BaseException:
        if owned:
            c.rollback()
        else:
            c.execute("ROLLBACK TO SAVEPOINT metrics_rollup")
            c.execute("RELEASE SAVEPOINT metrics_rollup")
        raise


def downsample_minute(conn: Optional[sqlite3.Connection] = None, *, now: Optional[int] = None) -> int:
    """Roll unrolled raw samples from every fully elapsed minute into samples_1min.

    Returns the number of rollup rows written.
    """
    now = int(time.time()) if now is None else int(now)

    def _run(c: sqlite3.Connection) -> int:
        return _roll_up(c, "samples", "samples_1min", 60, now - now % 60)

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def downsample_hour(conn: Optional[sqlite3.Connection] = None, *, now: Optional[int] = None) -> int:
    """Roll unrolled minute rows from every fully elapsed hour into samples_1hour."""
    now = int(time.time()) if now is None else int(now)

    def _run(c: sqlite3.Connection) -> int:
        return _roll_up(c, "samples_1min", "samples_1hour", 3600, now - now % 3600)

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def prune_old(conn: Optional[sqlite3.Connection] = None, *, now: Optional[int] = None) -> dict:
    """Delete old raw and minute rows only after hourly coverage. Returns counts."""
    now = int(time.time()) if now is None else int(now)
    raw_cut = now - RAW_RETAIN_S
    min_cut = now - MINUTE_RETAIN_S

    def _run(c: sqlite3.Connection) -> dict:
        out: dict[str, int] = {}
        # A raw mark proves minute coverage. Wait until every retained minute
        # contribution for that bucket is itself covered by hourly history.
        # Missing minute rows were already pruned only after their hourly mark.
        cur = c.execute(
            "DELETE FROM samples WHERE ts < ? AND rolled = 1 AND NOT EXISTS ("
            "SELECT 1 FROM samples_1min m WHERE m.metric = samples.metric "
            "AND m.tags = samples.tags AND m.ts = samples.ts - samples.ts % 60 "
            "AND m.rolled != 1)", (raw_cut,),
        )
        out["raw"] = cur.rowcount or 0
        cur = c.execute("DELETE FROM samples_1min WHERE ts < ? AND rolled = 1", (min_cut,))
        out["minute"] = cur.rowcount or 0
        if hasattr(c, "commit"):
            c.commit()
        return out

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def disk_avail_bytes(path: Path = DB_PATH) -> int:
    """Free bytes on the partition holding the DB."""
    target = path.parent if path.parent.exists() else Path("/")
    try:
        usage = shutil.disk_usage(str(target))
        return usage.free
    except Exception:
        return 0


def db_health(path: Path = DB_PATH) -> dict:
    """Return file size + oldest sample + total rows for the status page."""
    out: dict[str, Any] = {
        "path": str(path),
        "size_bytes": None,
        "oldest_sample_ts": None,
        "total_rows": None,
        "recent_write_ok": None,
    }
    try:
        if path.exists():
            out["size_bytes"] = path.stat().st_size
    except Exception:
        pass
    try:
        with reader(path) as c:
            # Separate scalar aggregates keep one read snapshot while allowing
            # SQLite's indexed MIN lookup and fast COUNT opcode.
            row = c.execute(
                "SELECT (SELECT MIN(ts) FROM samples), (SELECT COUNT(*) FROM samples)"
            ).fetchone()
            if row:
                out["oldest_sample_ts"] = row[0]
                out["total_rows"] = row[1]
            recent = c.execute(
                "SELECT MAX(ts) FROM samples"
            ).fetchone()
            if recent and recent[0]:
                out["recent_write_ok"] = (int(time.time()) - int(recent[0])) < 120
            else:
                out["recent_write_ok"] = False
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out
