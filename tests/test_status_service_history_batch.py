"""The batched service-health history equals the per-backend queries it replaced.

`_metrics_section_queries` used to run `query_aggregate` / `query_series` with a
tag filter once per backend and metric. Each of those read every row of the
metric and filtered tags in Python, so the cost grew with backends x rows. The
history now comes from one grouped read per metric, window and table.

The OLD_* functions below are the previous code, copied as the oracle.
"""

from __future__ import annotations

import random
import sqlite3
from functools import partial
from typing import Any, Mapping, Optional
from unittest.mock import patch

import pytest

import metrics_store
import status_endpoint

NOW = 1_800_000_000
DAY = 86400
WEEK = 7 * DAY
# Seconds before NOW: both window edges (cutoff is inclusive), 15-minute series
# bucket edges, and rows just outside each window.
EDGE_AGES = (
    0, 61, 899, 900, 901, 3600, 43200, DAY - 1, DAY, DAY + 1, 2 * DAY,
    WEEK - 1, WEEK, WEEK + 1,
)
HISTORY_METRICS = ("service_up", "service_status_code", "service_latency_ms")


def backend_tags(key: str, role: str = "required") -> dict[str, str]:
    return status_endpoint._backend_history_tags({"key": key, "name": key, "role": role})


def backend(key: str, role: str = "required") -> dict[str, str]:
    return {"key": key, "name": key, "role": role}


# --- The oracle: the per-backend code this change replaced -----------------


def old_services_block(metrics_store_module, backends, *, conn=None) -> dict[str, Any]:
    query_aggregate = partial(metrics_store_module.query_aggregate, conn=conn)
    query_series = partial(metrics_store_module.query_series, conn=conn)

    def _agg_fallback(metric: str, since_s: int, op: str, tags: Optional[dict] = None):
        try:
            v = query_aggregate(metric, since_s=since_s, tags_filter=tags, op=op, table="samples_1min")
            if v is not None:
                return v
        except Exception:
            pass
        try:
            return query_aggregate(metric, since_s=since_s, tags_filter=tags, op=op)
        except Exception:
            return None

    def _series_fallback(metric: str, since_s: int, points: int, op: str = "avg",
                         tags: Optional[dict] = None):
        try:
            s = query_series(metric, since_s=since_s, points=points, tags_filter=tags,
                             table="samples_1min", op=op)
            if s:
                return s
        except Exception:
            pass
        try:
            return query_series(metric, since_s=since_s, points=points, tags_filter=tags, op=op)
        except Exception:
            return []

    services_history: dict[str, Any] = {"window_s": 86400, "backends": {}}
    try:
        for b in backends or []:
            if not isinstance(b, Mapping):
                continue
            key = str(b.get("key") or b.get("name") or "")
            if not key:
                continue
            tags = status_endpoint._backend_history_tags(b)
            uptime_24h = _agg_fallback("service_up", since_s=86400, op="avg", tags=tags)
            uptime_7d = _agg_fallback("service_up", since_s=7 * 86400, op="avg", tags=tags)
            sample_count_24h = _agg_fallback("service_status_code", since_s=86400, op="count", tags=tags)
            avg_latency_24h = _agg_fallback("service_latency_ms", since_s=86400, op="avg", tags=tags)
            p95_latency_24h = status_endpoint._backend_p95_latency_ms(tags, since_s=86400, conn=conn)
            avg_latency_7d = _agg_fallback("service_latency_ms", since_s=7 * 86400, op="avg", tags=tags)
            p95_latency_7d = status_endpoint._backend_p95_latency_ms(tags, since_s=7 * 86400, conn=conn)
            services_history["backends"][key] = {
                "key": key,
                "name": b.get("name") or key,
                "role": b.get("role"),
                "uptime_pct_24h": round(float(uptime_24h) * 100.0, 3)
                if isinstance(uptime_24h, (int, float)) else None,
                "uptime_pct_7d": round(float(uptime_7d) * 100.0, 3)
                if isinstance(uptime_7d, (int, float)) else None,
                "sample_count_24h": int(sample_count_24h)
                if isinstance(sample_count_24h, (int, float)) else 0,
                "avg_latency_ms_24h": int(avg_latency_24h) if isinstance(avg_latency_24h, (int, float)) else None,
                "p95_latency_ms_24h": p95_latency_24h,
                "avg_latency_ms_7d": int(avg_latency_7d) if isinstance(avg_latency_7d, (int, float)) else None,
                "p95_latency_ms_7d": p95_latency_7d,
                "status_24h_series": _series_fallback(
                    "service_status_code", since_s=86400, points=96, tags=tags, op="avg"),
                "latency_ms_24h_series": _series_fallback(
                    "service_latency_ms", since_s=86400, points=96, tags=tags, op="avg"),
            }
    except Exception as e:  # noqa: BLE001
        services_history["error"] = str(e)
    return services_history


# --- Seeding ---------------------------------------------------------------


class Seeder:
    def __init__(self, conn: sqlite3.Connection, seed: int = 1234):
        self.conn = conn
        self.rnd = random.Random(seed)

    def raw(self, tags: Any, metric: str, ages=EDGE_AGES) -> None:
        tags_json = tags if isinstance(tags, str) else metrics_store._serialize_tags(tags)
        for age in ages:
            for _ in range(2):  # more than one sample per bucket
                value = round(self.rnd.uniform(1.0, 400.0), 3)
                self.conn.execute(
                    "INSERT INTO samples (ts, metric, tags, value) VALUES (?,?,?,?)",
                    (NOW - age + self.rnd.randrange(0, 5), metric, tags_json, value),
                )

    def rollup(self, tags: Any, metric: str, ages=EDGE_AGES, count: Optional[int] = None) -> None:
        tags_json = tags if isinstance(tags, str) else metrics_store._serialize_tags(tags)
        for age in ages:
            avg = round(self.rnd.uniform(1.0, 400.0), 3)
            self.conn.execute(
                "INSERT INTO samples_1min (ts, metric, tags, avg_value, min_value, max_value, count)"
                " VALUES (?,?,?,?,?,?,?)",
                (NOW - age, metric, tags_json, avg, avg - 0.5, avg + 0.75,
                 self.rnd.randint(1, 6) if count is None else count),
            )

    def both(self, tags: Any, metrics=HISTORY_METRICS, ages=EDGE_AGES) -> None:
        for metric in metrics:
            self.raw(tags, metric, ages)
            self.rollup(tags, metric, ages)


def seed_scenarios(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """One backend per case the fallback logic has to keep apart."""
    s = Seeder(conn)

    # Rollup and raw rows for every metric.
    s.both(backend_tags("full"))

    # A filter that matches several stored tag sets: the exact set plus supersets
    # with different counts. A same-named set with another role must not match.
    multi = backend_tags("multi")
    s.both(multi)
    s.both({**multi, "shard": "1"})
    s.both({**multi, "shard": "2", "zone": "b"}, ages=EDGE_AGES[::2])
    s.both(backend_tags("multi", role="optional"))

    s.rollup(backend_tags("rollup_only"), "service_up")
    s.rollup(backend_tags("rollup_only"), "service_status_code")
    s.rollup(backend_tags("rollup_only"), "service_latency_ms")

    for metric in HISTORY_METRICS:
        s.raw(backend_tags("raw_only"), metric)

    # Rollup for one metric, raw for the others.
    s.rollup(backend_tags("mixed"), "service_up")
    s.raw(backend_tags("mixed"), "service_status_code")
    s.raw(backend_tags("mixed"), "service_latency_ms")

    # Rollup rows that carry no samples cannot answer an average: raw does.
    for metric in HISTORY_METRICS:
        s.rollup(backend_tags("zero_count"), metric, count=0)
        s.raw(backend_tags("zero_count"), metric)

    # Rollup rows older than 24 h only: the 24 h window needs raw, the 7 d one does not.
    old = tuple(a for a in EDGE_AGES if a > DAY)
    for metric in HISTORY_METRICS:
        s.rollup(backend_tags("old_rollup"), metric, ages=old)
        s.raw(backend_tags("old_rollup"), metric, ages=tuple(a for a in EDGE_AGES if a <= DAY))

    # Only rows on the window edges.
    for metric in HISTORY_METRICS:
        s.raw(backend_tags("edge"), metric, ages=(DAY - 1, DAY, DAY + 1, WEEK - 1, WEEK, WEEK + 1))
        s.rollup(backend_tags("edge"), metric, ages=(DAY, DAY + 1, WEEK, WEEK + 1))

    # Rows that match no backend: no tags, and tags that are not JSON.
    for metric in HISTORY_METRICS:
        s.both("", (metric,))
        s.both("not-json", (metric,))
    conn.commit()

    keys = ["full", "multi", "rollup_only", "raw_only", "mixed", "zero_count",
            "old_rollup", "edge", "absent"]
    return [backend(k) for k in keys]


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    with metrics_store.reader(db) as conn:
        backends = seed_scenarios(conn)
    return db, backends


def run_both(db, backends):
    with (
        patch.object(metrics_store, "DB_PATH", db),
        patch.object(metrics_store.time, "time", return_value=NOW),
        metrics_store.reader(db) as conn,
    ):
        expected = old_services_block(metrics_store, backends, conn=conn)
        actual = status_endpoint._metrics_section_queries(
            metrics_store, None, None, backends, conn=conn,
        )["services"]
    return expected, actual


# --- Equivalence -----------------------------------------------------------


def test_services_block_equals_the_per_backend_queries(seeded_db):
    db, backends = seeded_db
    expected, actual = run_both(db, backends)

    assert actual == expected
    # The oracle is only meaningful if the seeded cases produce real, differing answers.
    by_key = expected["backends"]
    assert set(by_key) == {b["key"] for b in backends}
    assert by_key["absent"]["uptime_pct_24h"] is None
    assert by_key["absent"]["sample_count_24h"] == 0
    assert by_key["absent"]["status_24h_series"] == []
    for key in ("full", "multi", "rollup_only", "raw_only", "mixed", "zero_count", "old_rollup", "edge"):
        assert by_key[key]["uptime_pct_7d"] is not None, key
        assert by_key[key]["status_24h_series"], key
        assert by_key[key]["latency_ms_24h_series"], key
    assert by_key["raw_only"]["sample_count_24h"] > 0
    assert len({tuple(by_key[k]["latency_ms_24h_series"]) for k in by_key}) > 5


def test_a_filter_matching_several_tag_sets_weights_by_count(seeded_db):
    """multi spans three stored tag sets: the answer must not average their averages."""
    db, backends = seeded_db
    _, actual = run_both(db, backends)
    with metrics_store.reader(db) as conn:
        rows = conn.execute(
            "SELECT tags, avg_value, count FROM samples_1min WHERE metric='service_latency_ms' AND ts >= ?",
            (NOW - DAY,),
        ).fetchall()
    tags = backend_tags("multi")
    matching = [r for r in rows if metrics_store._matches_tags(r[0], tags)]
    assert len({r[0] for r in matching}) == 3
    weighted = sum(r[1] * r[2] for r in matching) / sum(r[2] for r in matching)
    assert actual["backends"]["multi"]["avg_latency_ms_24h"] == int(weighted)
    naive = sum(r[1] for r in matching) / len(matching)
    assert int(naive) != int(weighted), "seed lost its power to tell a weighted average from an average of averages"


def test_a_repeated_key_keeps_its_last_entry(seeded_db):
    db, backends = seeded_db
    backends = backends + [backend("full", role="optional")]
    expected, actual = run_both(db, backends)
    assert actual == expected
    assert actual["backends"]["full"]["role"] == "optional"


def test_skipped_backends_are_skipped(seeded_db):
    db, backends = seeded_db
    backends = [None, {"name": ""}, "text", *backends]
    expected, actual = run_both(db, backends)
    assert actual == expected


def test_a_stored_tags_value_that_is_not_an_object_falls_back_like_before(tmp_path):
    """Tags such as '[1]' make the tag match raise; the old code then fell back to raw."""
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    with metrics_store.reader(db) as conn:
        s = Seeder(conn)
        s.rollup("[1]", "service_up")
        s.raw(backend_tags("a"), "service_up")
        s.both(backend_tags("a"), ("service_status_code", "service_latency_ms"))
        conn.commit()
    expected, actual = run_both(db, [backend("a")])
    assert actual == expected
    assert actual["backends"]["a"]["uptime_pct_24h"] is not None


def test_missing_rollup_table_falls_back_like_before(tmp_path):
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    with metrics_store.reader(db) as conn:
        Seeder(conn).both(backend_tags("a"))
        conn.execute("DROP TABLE samples_1min")
        conn.commit()
    expected, actual = run_both(db, [backend("a")])
    assert actual == expected
    assert actual["backends"]["a"]["uptime_pct_7d"] is not None


# --- The grouped helpers against the per-filter queries ---------------------


@pytest.mark.parametrize("table", ["samples", "samples_1min", "samples_1hour"])
@pytest.mark.parametrize("op", ["avg", "min", "max", "sum", "count"])
def test_reduce_aggregate_matches_query_aggregate(tmp_path, table, op):
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    filters = [
        backend_tags("multi"), {"backend": "multi"}, {"shard": "1"}, {"zone": "b"},
        backend_tags("absent"), {"backend": "multi", "shard": "9"},
    ]
    with metrics_store.reader(db) as conn:
        s = Seeder(conn)
        for tags in (backend_tags("multi"), {**backend_tags("multi"), "shard": "1"},
                     {**backend_tags("multi"), "shard": "2", "zone": "b"}):
            if table == "samples":
                s.raw(tags, "m")
            else:
                conn.executemany(
                    f"INSERT INTO {table} (ts, metric, tags, avg_value, min_value, max_value, count)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [(NOW - a, "m", metrics_store._serialize_tags(tags),
                      s.rnd.uniform(1, 9), 0.5, 11.0, s.rnd.randint(1, 5)) for a in EDGE_AGES],
                )
        conn.commit()
        with patch.object(metrics_store.time, "time", return_value=NOW):
            groups = metrics_store.query_aggregate_by_tags("m", since_s=DAY, table=table, conn=conn)
            for filt in filters + [None]:
                want = metrics_store.query_aggregate(
                    "m", since_s=DAY, tags_filter=filt, op=op, table=table, conn=conn)
                got = metrics_store.reduce_aggregate(groups, tags_filter=filt, op=op)
                if want is None:
                    assert got is None
                else:
                    assert got == pytest.approx(want)


@pytest.mark.parametrize("table", ["samples", "samples_1min", "samples_1hour"])
@pytest.mark.parametrize("op", ["avg", "min", "max", "sum", "count"])
def test_reduce_series_matches_query_series(tmp_path, table, op):
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    with metrics_store.reader(db) as conn:
        s = Seeder(conn)
        for tags in (backend_tags("multi"), {**backend_tags("multi"), "shard": "1"}):
            if table == "samples":
                s.raw(tags, "m")
            else:
                conn.executemany(
                    f"INSERT INTO {table} (ts, metric, tags, avg_value, min_value, max_value, count)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [(NOW - a, "m", metrics_store._serialize_tags(tags),
                      s.rnd.uniform(1, 9), 0.5, 11.0, s.rnd.randint(1, 5)) for a in EDGE_AGES],
                )
        conn.commit()
        with patch.object(metrics_store.time, "time", return_value=NOW):
            groups = metrics_store.query_series_by_tags(
                "m", since_s=DAY, points=96, table=table, conn=conn)
            for filt in (backend_tags("multi"), {"shard": "1"}, {"backend": "nobody"}):
                want = metrics_store.query_series(
                    "m", since_s=DAY, points=96, tags_filter=filt, table=table, op=op, conn=conn)
                got = metrics_store.reduce_series(groups, tags_filter=filt, op=op)
                assert [t for t, _ in got] == [t for t, _ in want]
                assert [v for _, v in got] == pytest.approx([v for _, v in want])


# --- Statement count -------------------------------------------------------


class Counted:
    """A connection whose executed statements are counted."""

    def __init__(self, db):
        self.conn = sqlite3.connect(str(db))
        self.statements: list[str] = []
        self.conn.set_trace_callback(self.statements.append)

    def reset(self) -> None:
        self.statements.clear()


def seeded_many(tmp_path, n: int):
    db = tmp_path / "metrics.db"
    metrics_store.init_db(db)
    rnd = random.Random(7)
    with metrics_store.reader(db) as conn:
        s = Seeder(conn, seed=99)
        for i in range(n):
            tags = backend_tags(f"svc{i:02d}")
            # Alternate so some backends need the raw fallback and some do not.
            kind = i % 3
            for metric in HISTORY_METRICS:
                if kind in (0, 2):
                    s.raw(tags, metric, ages=EDGE_AGES[::3])
                if kind in (0, 1):
                    s.rollup(tags, metric, ages=EDGE_AGES[::3])
        conn.commit()
    return db, [backend(f"svc{i:02d}") for i in range(n)], rnd


def count_statements(db, backends, fn) -> int:
    counted = Counted(db)
    try:
        with patch.object(metrics_store.time, "time", return_value=NOW):
            fn(counted.conn, backends)
        return len(counted.statements)
    finally:
        counted.conn.close()


def test_history_statements_do_not_grow_with_backends(tmp_path):
    db, backends, _ = seeded_many(tmp_path, 30)
    # Both lists include backends that need the raw fallback (index 2 is raw-only).
    few, many = backends[1:3], backends

    def batch(conn, bks):
        filters = {b["key"]: status_endpoint._backend_history_tags(b) for b in bks}
        status_endpoint._service_history_batch(metrics_store, filters, conn=conn)

    two = count_statements(db, few, batch)
    thirty = count_statements(db, many, batch)
    assert two == thirty
    # 7 reads x (rollup + raw fallback).
    assert thirty <= 14

    def old(conn, bks):
        old_services_block(metrics_store, bks, conn=conn)

    old_thirty = count_statements(db, many, old)
    assert old_thirty > 10 * thirty, "the counter would not notice a per-backend loop"


def test_full_section_statements_grow_only_by_the_p95_lookups(tmp_path):
    """Two exact-tag p95 queries per backend remain; nothing else scales with backends."""
    db, backends, _ = seeded_many(tmp_path, 30)

    def section(conn, bks):
        status_endpoint._metrics_section_queries(metrics_store, None, None, bks, conn=conn)

    few, many = backends[1:3], backends
    two = count_statements(db, few, section)
    thirty = count_statements(db, many, section)
    assert thirty - two == 2 * (len(many) - len(few))
