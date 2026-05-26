"""SQLite-backed metrics persistence layer.

Single tag-based samples table with two pre-aggregated rollups (1min, 1hour).
Stdlib sqlite3 only. WAL mode for concurrent read while one writer appends.

Layout:
    samples         - raw 10s samples, retained 7 days
    samples_1min    - per-minute rollup, retained 30 days
    samples_1hour   - per-hour rollup, retained 365 days

All public functions accept an optional connection. When omitted, a short-lived
read connection is auto-opened. The sampler holds one long-lived writer via
``open_writer()``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

_default_db = Path(__file__).parent / "data" / "metrics.db"
DB_PATH = Path(os.environ["AI_GATEWAY_METRICS_DB"]) if os.environ.get("AI_GATEWAY_METRICS_DB") else _default_db

# Retention windows (seconds).
RAW_RETAIN_S = 7 * 24 * 3600
MINUTE_RETAIN_S = 30 * 24 * 3600
HOUR_RETAIN_S = 365 * 24 * 3600

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER NOT NULL,
  metric TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_metric_ts ON samples(metric, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS samples_1min (
  ts INTEGER NOT NULL,
  metric TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  avg_value REAL NOT NULL,
  min_value REAL NOT NULL,
  max_value REAL NOT NULL,
  count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_1min_metric_ts ON samples_1min(metric, ts);

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
            print(f"[metrics_store] DB corrupt ({e}); moving aside to {broken}", flush=True)
            try:
                shutil.move(str(path), str(broken))
            except Exception as mv_err:  # noqa: BLE001
                print(f"[metrics_store] failed to move broken db: {mv_err}", flush=True)
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


def open_writer(path: Path = DB_PATH) -> sqlite3.Connection:
    """Long-lived writer connection. Caller owns it; close on shutdown."""
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    _apply_pragmas(conn)
    return conn


@contextmanager
def reader(path: Path = DB_PATH):
    """Short-lived read connection. WAL makes this cheap."""
    conn = sqlite3.connect(str(path), timeout=2.0)
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
            filtered = [r for r in rows if _matches_tags(r[0], tags_filter)]
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
    now = int(time.time())
    cutoff = now - since_s
    bucket_s = max(1, since_s // points)

    if table == "samples":
        if tags_filter is None:
            sql = (
                "SELECT (ts/?)*?, AVG(value) FROM samples "
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
                    if not _matches_tags(tag_json, tags_filter):
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
                if not _matches_tags(tag_json, tags_filter):
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


def downsample_minute(conn: Optional[sqlite3.Connection] = None) -> int:
    """Aggregate raw samples from last fully-elapsed minute into samples_1min.

    Returns the number of rollup rows written.
    """
    now = int(time.time())
    # Aggregate the minute that just finished, i.e. [now - now%60 - 60, now - now%60).
    bucket_end = now - (now % 60)
    bucket_start = bucket_end - 60

    def _run(c: sqlite3.Connection) -> int:
        # Skip if we already rolled up this minute.
        existing = c.execute(
            "SELECT 1 FROM samples_1min WHERE ts = ? LIMIT 1",
            (bucket_start,),
        ).fetchone()
        if existing:
            return 0
        rows = c.execute(
            "SELECT metric, tags, AVG(value), MIN(value), MAX(value), COUNT(*) "
            "FROM samples WHERE ts >= ? AND ts < ? GROUP BY metric, tags",
            (bucket_start, bucket_end),
        ).fetchall()
        if not rows:
            return 0
        c.executemany(
            "INSERT INTO samples_1min (ts, metric, tags, avg_value, min_value, max_value, count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(bucket_start, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
        )
        c.commit() if hasattr(c, "commit") else None
        return len(rows)

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def downsample_hour(conn: Optional[sqlite3.Connection] = None) -> int:
    """Aggregate samples_1min from last fully-elapsed hour into samples_1hour."""
    now = int(time.time())
    bucket_end = now - (now % 3600)
    bucket_start = bucket_end - 3600

    def _run(c: sqlite3.Connection) -> int:
        existing = c.execute(
            "SELECT 1 FROM samples_1hour WHERE ts = ? LIMIT 1",
            (bucket_start,),
        ).fetchone()
        if existing:
            return 0
        # Weighted average: SUM(avg_value*count) / SUM(count).
        rows = c.execute(
            "SELECT metric, tags, "
            "SUM(avg_value*count)*1.0/NULLIF(SUM(count),0), "
            "MIN(min_value), MAX(max_value), SUM(count) "
            "FROM samples_1min WHERE ts >= ? AND ts < ? GROUP BY metric, tags",
            (bucket_start, bucket_end),
        ).fetchall()
        if not rows:
            return 0
        c.executemany(
            "INSERT INTO samples_1hour (ts, metric, tags, avg_value, min_value, max_value, count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(bucket_start, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
        )
        c.commit() if hasattr(c, "commit") else None
        return len(rows)

    if conn is not None:
        return _run(conn)
    with reader(DB_PATH) as c:
        return _run(c)


def prune_old(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Delete rows older than each table's retention window. Returns counts."""
    now = int(time.time())
    raw_cut = now - RAW_RETAIN_S
    min_cut = now - MINUTE_RETAIN_S
    hr_cut = now - HOUR_RETAIN_S

    def _run(c: sqlite3.Connection) -> dict:
        out: dict[str, int] = {}
        cur = c.execute("DELETE FROM samples WHERE ts < ?", (raw_cut,))
        out["raw"] = cur.rowcount or 0
        cur = c.execute("DELETE FROM samples_1min WHERE ts < ?", (min_cut,))
        out["minute"] = cur.rowcount or 0
        cur = c.execute("DELETE FROM samples_1hour WHERE ts < ?", (hr_cut,))
        out["hour"] = cur.rowcount or 0
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
            row = c.execute("SELECT MIN(ts), COUNT(*) FROM samples").fetchone()
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
