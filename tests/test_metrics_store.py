"""Standalone tests for metrics_store. Run via:

    python3 tests/test_metrics_store.py

Exits non-zero on failure. No pytest dependency required.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure repo root on sys.path before importing metrics_store.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics_store  # noqa: E402


class TestMetricsStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        # Patch DB_PATH for the helpers that auto-open connections.
        self._orig = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)

    def tearDown(self) -> None:
        metrics_store.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_round_trip_insert_and_query(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            now = int(time.time())
            metrics_store.record_samples(conn, [
                (now, "x", "", 1.0),
                (now, "x", "", 2.0),
                (now, "x", "", 3.0),
            ])
            self.assertAlmostEqual(
                metrics_store.query_aggregate("x", since_s=10, op="avg") or 0.0,
                2.0,
            )
            self.assertEqual(metrics_store.query_aggregate("x", since_s=10, op="max"), 3.0)
            self.assertEqual(metrics_store.query_aggregate("x", since_s=10, op="min"), 1.0)
            self.assertEqual(metrics_store.query_aggregate("x", since_s=10, op="count"), 3.0)
        finally:
            conn.close()

    def test_tag_filter(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            now = int(time.time())
            metrics_store.record_sample(conn, now, "y", 10.0, tags={"key": "alpha"})
            metrics_store.record_sample(conn, now, "y", 20.0, tags={"key": "beta"})
            metrics_store.record_sample(conn, now, "y", 30.0, tags={"key": "alpha"})
            self.assertEqual(
                metrics_store.query_aggregate(
                    "y", since_s=10, tags_filter={"key": "alpha"}, op="avg"
                ),
                20.0,
            )
            self.assertEqual(
                metrics_store.query_aggregate(
                    "y", since_s=10, tags_filter={"key": "beta"}, op="max"
                ),
                20.0,
            )
        finally:
            conn.close()

    def test_tag_matching_is_cached_per_query_without_changing_results(self) -> None:
        from unittest.mock import patch

        now = int(time.time())
        with metrics_store.reader(self.db) as conn:
            rows = [(now - i, "cache", '{"key":"alpha","extra":1}', float(i))
                    for i in range(20)]
            rows += [(now, "cache", '{"key":"beta"}', 99.0),
                     (now, "cache", "invalid-json", 99.0)]
            metrics_store.record_samples(conn, rows)
            conn.commit()
            for op in ("avg", "min", "max", "sum", "count"):
                with patch.object(metrics_store.json, "loads", wraps=metrics_store.json.loads) as loads:
                    value = metrics_store.query_aggregate(
                        "cache", since_s=60, tags_filter={"key": "alpha"}, op=op, conn=conn
                    )
                    self.assertEqual(loads.call_count, 3)
                expected = {"avg": 9.5, "min": 0.0, "max": 19.0,
                            "sum": 190.0, "count": 20.0}[op]
                self.assertEqual(value, expected)
                with patch.object(metrics_store.json, "loads", wraps=metrics_store.json.loads) as loads:
                    series = metrics_store.query_series(
                        "cache", since_s=60, points=1, tags_filter={"key": "alpha"},
                        op=op, conn=conn
                    )
                    self.assertEqual(loads.call_count, 3)
                    self.assertTrue(series)
            # A subsequent query must use its own filter rather than a cached prior decision.
            self.assertEqual(metrics_store.query_aggregate(
                "cache", since_s=60, tags_filter={"key": "beta"}, conn=conn
            ), 99.0)

    def test_tagged_rollups_preserve_weighted_values_and_cache_matching(self) -> None:
        from unittest.mock import patch

        for table in ("samples_1min", "samples_1hour"):
            with metrics_store.reader(self.db) as conn:
                conn.executemany(
                    f"INSERT INTO {table} (ts, metric, tags, avg_value, min_value, max_value, count) VALUES (?,?,?,?,?,?,?)",
                    [(1750, "rollup", '{"key":"alpha","extra":1}', 10, 8, 12, 2),
                     (1760, "rollup", '{"key":"alpha","extra":1}', 20, 16, 24, 3),
                     (1760, "rollup", '{"key":"beta"}', 99, 99, 99, 1),
                     (1760, "rollup", "invalid-json", 99, 99, 99, 1)],
                )
                conn.commit()
                for op, expected in (("avg", 16), ("min", 8), ("max", 24),
                                     ("sum", 80), ("count", 5)):
                    with self.subTest(table=table, op=op), patch.object(metrics_store.time, "time", return_value=1800):
                        with patch.object(metrics_store.json, "loads", wraps=metrics_store.json.loads) as loads:
                            value = metrics_store.query_aggregate(
                                "rollup", since_s=600, tags_filter={"key": "alpha"},
                                table=table, op=op, conn=conn
                            )
                            self.assertEqual(value, expected)
                            self.assertEqual(loads.call_count, 3)
                        with patch.object(metrics_store.json, "loads", wraps=metrics_store.json.loads) as loads:
                            series = metrics_store.query_series(
                                "rollup", since_s=600, points=1, tags_filter={"key": "alpha"},
                                table=table, op=op, conn=conn
                            )
                            self.assertEqual(series, [(1200, expected)])
                            self.assertEqual(loads.call_count, 3)

    def test_index_migration_is_idempotent_and_covers_latency_lookup(self) -> None:
        import sqlite3

        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP INDEX samples_latency_tags_ts")
            conn.execute("UPDATE schema_version SET version=1")
            conn.execute("INSERT INTO samples (ts, metric, tags, value) VALUES (100,'service_latency_ms','{}',42)")
        metrics_store.init_db(self.db)
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], metrics_store.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT value FROM samples").fetchone()[0], 42)
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT value FROM samples "
                "WHERE metric='service_latency_ms' AND ts>=? AND tags=? ORDER BY value",
                (0, "{}"),
            ).fetchall()
            self.assertTrue(any("COVERING INDEX samples_latency_tags_ts" in row[3] for row in plan), plan)

    def test_rollup_timestamp_indexes_migrate_without_changing_rows(self) -> None:
        import sqlite3

        with sqlite3.connect(self.db) as conn:
            for table in ("samples_1min", "samples_1hour"):
                conn.execute(f"DROP INDEX {table}_ts")
                conn.execute(f"INSERT INTO {table} (ts, metric, tags, avg_value, min_value, max_value, count) VALUES (60, 'x', '', 2, 1, 3, 2)")
            conn.execute("UPDATE schema_version SET version=2")
        metrics_store.init_db(self.db)
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], metrics_store.SCHEMA_VERSION)
            for table in ("samples_1min", "samples_1hour"):
                self.assertEqual(conn.execute(
                    f"SELECT ts, metric, tags, avg_value, min_value, max_value, count FROM {table}"
                ).fetchall(), [(60, 'x', '', 2, 1, 3, 2)])
                for sql, params in (
                    (f"SELECT 1 FROM {table} WHERE ts = ? LIMIT 1", (60,)),
                    (f"SELECT * FROM {table} WHERE ts >= ? AND ts < ?", (0, 120)),
                    (f"DELETE FROM {table} WHERE ts < ?", (0,)),
                ):
                    plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                    self.assertTrue(any(f"SEARCH {table} USING" in row[3] and f"{table}_ts" in row[3]
                                        for row in plan), plan)

    def test_a_version_3_db_gains_the_sampler_state_table_without_changing_rows(self) -> None:
        import sqlite3

        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE sampler_state")
            conn.execute("INSERT INTO samples (ts, metric, tags, value) VALUES (60, 'x', '', 2)")
            conn.execute("UPDATE schema_version SET version=3")
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT ts, metric, tags, value FROM samples").fetchall(), [(60, "x", "", 2.0)])
            metrics_store.write_sampler_state(conn, {"k": "v1"})
            metrics_store.write_sampler_state(conn, {"k": "v2"})
        self.assertEqual(metrics_store.read_sampler_state(self.db, "k"), "v2")
        self.assertIsNone(metrics_store.read_sampler_state(self.db, "missing"))

    def test_db_health_empty_and_old_samples(self) -> None:
        health = metrics_store.db_health(self.db)
        self.assertIsNone(health["oldest_sample_ts"])
        self.assertEqual(health["total_rows"], 0)
        self.assertFalse(health["recent_write_ok"])
        with metrics_store.reader(self.db) as conn:
            metrics_store.record_samples(conn, [(100, "old", "", 1), (200, "old", "", 2)])
            conn.commit()
        health = metrics_store.db_health(self.db)
        self.assertEqual(health["oldest_sample_ts"], 100)
        self.assertEqual(health["total_rows"], 2)
        self.assertFalse(health["recent_write_ok"])

    def test_query_series_has_exact_raw_aggregates(self) -> None:
        from unittest.mock import patch

        conn = metrics_store.open_writer(self.db)
        try:
            metrics_store.record_samples(conn, [
                (1199, "series", "", 9999.0),
                (1200, "series", "", 2.0),
                (1210, "series", "", 6.0),
                (1259, "series", "", 10.0),
                (1260, "series", "", -3.0),
                (1319, "series", "", 9.0),
                (1210, "other", "", 99999.0),
            ])
            expected = {
                "avg": [(1200, 6.0), (1260, 3.0)],
                "min": [(1200, 2.0), (1260, -3.0)],
                "max": [(1200, 10.0), (1260, 9.0)],
                "sum": [(1200, 18.0), (1260, 6.0)],
                "count": [(1200, 3.0), (1260, 2.0)],
            }
            with patch.object(metrics_store.time, "time", return_value=1800):
                # Known defect (reported, production unchanged): the unfiltered
                # SQL path ignores min/max/sum/count and always averages, so
                # only "avg" is pinned for tags_filter=None.
                for tags_filter in (None, {}):
                    for op, rows in expected.items():
                        if tags_filter is None and op != "avg":
                            continue
                        with self.subTest(tags_filter=tags_filter, op=op):
                            self.assertEqual(
                                metrics_store.query_series(
                                    "series",
                                    since_s=600,
                                    points=10,
                                    tags_filter=tags_filter,
                                    op=op,
                                    conn=conn,
                                ),
                                rows,
                            )
                self.assertEqual(
                    metrics_store.query_series(
                        "series", since_s=600, points=0, conn=conn,
                    ),
                    [],
                )
        finally:
            conn.close()

    def test_query_series_has_exact_filtered_raw_aggregates(self) -> None:
        from unittest.mock import patch

        conn = metrics_store.open_writer(self.db)
        try:
            metrics_store.record_samples(conn, [
                (1199, "series", "", 9999.0),
                (1200, "series", "", 2.0),
                (1210, "series", "", 6.0),
                (1259, "series", "", 10.0),
                (1260, "series", "", -3.0),
                (1319, "series", "", 9.0),
                (1210, "other", "", 99999.0),
            ])
            expected = {
                "avg": [(1200, 6.0), (1260, 3.0)],
                "min": [(1200, 2.0), (1260, -3.0)],
                "max": [(1200, 10.0), (1260, 9.0)],
                "sum": [(1200, 18.0), (1260, 6.0)],
                "count": [(1200, 3.0), (1260, 2.0)],
            }
            with patch.object(metrics_store.time, "time", return_value=1800):
                # Known defect: the unfiltered (tags_filter=None) raw branch
                # ignores op and always averages; only the filtered path is pinned.
                for tags_filter in ({},):
                    for op, rows in expected.items():
                        with self.subTest(tags_filter=tags_filter, op=op):
                            self.assertEqual(
                                metrics_store.query_series(
                                    "series", since_s=600, points=10,
                                    tags_filter=tags_filter, op=op, conn=conn,
                                ),
                                rows,
                            )
                self.assertEqual(
                    metrics_store.query_series(
                        "series", since_s=600, points=0, conn=conn,
                    ),
                    [],
                )
        finally:
            conn.close()

    def test_downsample_minute(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            now = int(time.time())
            last_min_start = ((now // 60) - 1) * 60
            for i in range(6):
                metrics_store.record_sample(conn, last_min_start + i * 10, "rl", float(i + 1))
            n = metrics_store.downsample_minute(conn)
            self.assertEqual(n, 1)
            with metrics_store.reader(self.db) as c:
                row = c.execute(
                    "SELECT avg_value, min_value, max_value, count "
                    "FROM samples_1min WHERE metric='rl'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[1], 1.0)
            self.assertEqual(row[2], 6.0)
            self.assertEqual(row[3], 6)
            self.assertAlmostEqual(row[0], 3.5)
        finally:
            conn.close()

    def test_init_db_does_not_scan_the_whole_file(self) -> None:
        """After migration, startup leaves the integrity scan to its background caller."""
        import sqlite3
        from unittest.mock import patch

        statements: list[str] = []
        real_connect = sqlite3.connect

        class Tracing(sqlite3.Connection):
            def execute(self, sql, *args):  # type: ignore[override]
                statements.append(str(sql))
                return super().execute(sql, *args)

        with patch.object(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Tracing, **k)):
            metrics_store.init_db(self.db)
        self.assertFalse([s for s in statements if "integrity_check" in s or "quick_check" in s], statements)

    def test_verify_integrity_passes_on_a_sound_file(self) -> None:
        self.assertIsNone(metrics_store.verify_integrity(self.db))
        self.assertFalse(metrics_store._integrity_marker(self.db).exists())

    def test_a_damaged_file_found_in_the_background_is_replaced_at_next_start(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            metrics_store.record_samples(conn, [(int(time.time()), "x", "", float(i)) for i in range(5000)])
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        # Overwrite a data page in the middle of the file: the header and schema stay readable.
        size = self.db.stat().st_size
        with open(self.db, "r+b") as handle:
            handle.seek(size // 2)
            handle.write(b"\xff" * 4096)
        problem = metrics_store.verify_integrity(self.db)
        self.assertIsNotNone(problem)
        self.assertTrue(metrics_store._integrity_marker(self.db).exists())
        # The live file is untouched until the next start.
        self.assertTrue(self.db.exists())
        metrics_store.init_db(self.db)
        self.assertFalse(metrics_store._integrity_marker(self.db).exists())
        self.assertEqual(len(list(self.db.parent.glob("metrics.db.broken-*"))), 1)
        self.assertIsNone(metrics_store.verify_integrity(self.db))

    def test_a_file_that_is_not_a_database_is_replaced_at_startup(self) -> None:
        self.db.write_bytes(b"this is not an sqlite database" * 200)
        metrics_store.init_db(self.db)
        self.assertEqual(len(list(self.db.parent.glob("metrics.db.broken-*"))), 1)
        self.assertIsNone(metrics_store.verify_integrity(self.db))

    def test_prune_old(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            now = int(time.time())
            # Sample 8 days old expires only once hourly history holds it.
            metrics_store.record_sample(conn, now - 8 * 86400, "p", 1.0)
            # Sample 1h old should survive.
            metrics_store.record_sample(conn, now - 3600, "p", 2.0)
            self.assertEqual(metrics_store.prune_old(conn), {"raw": 0, "minute": 0})
            metrics_store.downsample_minute(conn)
            self.assertEqual(metrics_store.prune_old(conn), {"raw": 0, "minute": 0})
            metrics_store.downsample_hour(conn)
            counts = metrics_store.prune_old(conn)
            self.assertEqual(counts["raw"], 1)
            with metrics_store.reader(self.db) as c:
                rows = c.execute(
                    "SELECT count(*) FROM samples WHERE metric='p'"
                ).fetchone()
                self.assertEqual(c.execute(
                    "SELECT ts, avg_value, count FROM samples_1min WHERE metric='p' ORDER BY ts"
                ).fetchall(), [((now - 8 * 86400) // 60 * 60, 1.0, 1), ((now - 3600) // 60 * 60, 2.0, 1)])
            self.assertEqual(rows[0], 1)
        finally:
            conn.close()

    def test_version_4_rows_are_kept_pending_coverage_reconciliation(self) -> None:
        import sqlite3

        old = int(time.time()) - 40 * 86400
        self.db.unlink()
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE samples (ts INTEGER NOT NULL, metric TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', value REAL NOT NULL);
                CREATE TABLE samples_1min (ts INTEGER NOT NULL, metric TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
                  avg_value REAL NOT NULL, min_value REAL NOT NULL, max_value REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE samples_1hour (ts INTEGER NOT NULL, metric TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
                  avg_value REAL NOT NULL, min_value REAL NOT NULL, max_value REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (4);
            """)
            minute = old - old % 60
            # The old rollup covered metric "a" in this minute but missed "b".
            conn.executemany("INSERT INTO samples VALUES (?, ?, '', ?)", [(minute, "a", 1.0), (minute, "b", 2.0)])
            conn.execute("INSERT INTO samples_1min VALUES (?, 'a', '', 1, 1, 1, 1)", (minute,))
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT metric, rolled FROM samples ORDER BY metric").fetchall(),
                             [("a", -1), ("b", -1)])
            self.assertEqual(metrics_store.prune_old(conn), {"raw": 0, "minute": 0})
            metrics_store.downsample_minute(conn)
            metrics_store.downsample_hour(conn)
            self.assertEqual(metrics_store.prune_old(conn), {"raw": 0, "minute": 0})
            self.assertEqual(conn.execute(
                "SELECT metric, count FROM samples_1hour ORDER BY metric").fetchall(), [])

    def test_db_health(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            metrics_store.record_sample(conn, int(time.time()), "h", 1.0)
        finally:
            conn.close()
        h = metrics_store.db_health(self.db)
        self.assertTrue(h["recent_write_ok"])
        self.assertGreater(h["total_rows"] or 0, 0)



class TestRetentionRegressions(unittest.TestCase):
    """Offline SQLite/fake regressions for the four retention review findings."""

    def test_rollups_lock_out_late_writes_and_rollback_injected_failure(self):
        import sqlite3

        class Hooked:
            def __init__(self, conn, source, hook):
                self.conn, self.source, self.hook = conn, source, hook

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                if sql.startswith(f"UPDATE {self.source} SET rolled"):
                    self.hook()
                return self.conn.execute(sql, *args)

        for source, target, width, rollup in (
            ("samples", "samples_1min", 60, metrics_store.downsample_minute),
            ("samples_1min", "samples_1hour", 3600, metrics_store.downsample_hour),
        ):
            for failure in (False, True):
                with self.subTest(source=source, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "metrics.db"
                    metrics_store.init_db(path)
                    conn = metrics_store.open_writer(path)
                    late = sqlite3.connect(path, isolation_level=None, timeout=0)
                    insert = (f"INSERT INTO {source} (ts,metric,tags,value) VALUES (1,'x','',?)"
                              if source == "samples" else
                              f"INSERT INTO {source} (ts,metric,tags,avg_value,min_value,max_value,count) "
                              "VALUES (1,'x','',?,?,?,1)")
                    values = lambda v: (v,) if source == "samples" else (v, v, v)
                    try:
                        self.assertIsNone(conn.isolation_level)
                        conn.execute(insert, values(2))
                        def between_statements():
                            if not failure:
                                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                                    late.execute(insert, values(7))
                            if failure:
                                raise RuntimeError("injected after summary")
                        hooked = Hooked(conn, source, between_statements)
                        if failure:
                            with self.assertRaisesRegex(RuntimeError, "injected"):
                                rollup(hooked, now=width)
                            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0], 0)
                            self.assertEqual(conn.execute(f"SELECT rolled FROM {source}").fetchone()[0], 0)
                            rollup(conn, now=width)
                        else:
                            rollup(hooked, now=width)
                        late.execute(insert, values(7))
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {source} WHERE rolled=0").fetchone()[0], 1)
                        rollup(conn, now=width)
                        rollup(conn, now=width)
                        self.assertEqual(conn.execute(
                            f"SELECT SUM(count), SUM(avg_value*count), MIN(min_value), MAX(max_value) FROM {target}"
                        ).fetchone(), (2, 9.0, 2.0, 7.0))
                    finally:
                        late.close()
                        conn.close()

    def test_raw_prune_requires_hourly_coverage_even_after_failed_hour_rollup(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            metrics_store.init_db(path)
            conn = metrics_store.open_writer(path)
            try:
                conn.execute("INSERT INTO samples(ts,metric,value) VALUES(1,'x',2)")
                metrics_store.downsample_minute(conn, now=4000000)
                self.assertEqual(metrics_store.prune_old(conn, now=4000000), {"raw": 0, "minute": 0})
                conn.execute("""CREATE TEMP TRIGGER fail_hour BEFORE UPDATE OF rolled ON samples_1min
                                BEGIN SELECT RAISE(ABORT, 'injected'); END""")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                    metrics_store.downsample_hour(conn, now=4000000)
                self.assertEqual(metrics_store.prune_old(conn, now=4000000), {"raw": 0, "minute": 0})
                conn.execute("DROP TRIGGER fail_hour")
                metrics_store.downsample_hour(conn, now=4000000)
                self.assertEqual(metrics_store.prune_old(conn, now=4000000), {"raw": 1, "minute": 1})
                self.assertEqual(conn.execute(
                    "SELECT count,avg_value*count,min_value,max_value FROM samples_1hour"
                ).fetchone(), (1, 2.0, 2.0, 2.0))
            finally:
                conn.close()

    def test_rollup_savepoint_preserves_caller_transaction_on_success_and_failure(self):
        import sqlite3
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "metrics.db"
                metrics_store.init_db(path)
                conn = metrics_store.open_writer(path)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("INSERT INTO sampler_state VALUES ('caller', 'pending')")
                    conn.execute("INSERT INTO samples(ts,metric,value) VALUES (1,'x',2)")
                    if fail:
                        conn.execute("""CREATE TEMP TRIGGER fail_mark BEFORE UPDATE OF rolled ON samples
                                        BEGIN SELECT RAISE(ABORT, 'injected'); END""")
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                            metrics_store.downsample_minute(conn, now=60)
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples_1min").fetchone()[0], 0)
                        self.assertEqual(conn.execute("SELECT rolled FROM samples").fetchone()[0], 0)
                    else:
                        metrics_store.downsample_minute(conn, now=60)
                    self.assertTrue(conn.in_transaction)
                    self.assertEqual(conn.execute("SELECT value FROM sampler_state").fetchone()[0], "pending")
                    conn.rollback()
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM sampler_state").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples_1min").fetchone()[0], 0)
                finally:
                    conn.close()

    def test_legacy_overlap_and_partial_pruning_are_retained_without_double_counting(self):
        import sqlite3
        for version in (4, 5):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "metrics.db"
                conn = sqlite3.connect(path)
                mark = ", rolled INTEGER NOT NULL DEFAULT 0" if version == 5 else ""
                try:
                    conn.executescript(f"""
                        CREATE TABLE schema_version(version INTEGER NOT NULL);
                        INSERT INTO schema_version VALUES ({version});
                        CREATE TABLE samples(ts INTEGER,metric TEXT,tags TEXT,value REAL{mark});
                        CREATE TABLE samples_1min(ts INTEGER,metric TEXT,tags TEXT,avg_value REAL,
                            min_value REAL,max_value REAL,count INTEGER{mark});
                        CREATE TABLE samples_1hour(ts INTEGER,metric TEXT,tags TEXT,avg_value REAL,
                            min_value REAL,max_value REAL,count INTEGER);
                        INSERT INTO samples(ts,metric,tags,value) VALUES (1,'late','',2),(2,'late','',9),
                            (1,'partial','',8);
                        INSERT INTO samples_1min(ts,metric,tags,avg_value,min_value,max_value,count)
                            VALUES (0,'late','',2,2,2,1),(0,'partial','',5,2,8,2),
                            (60,'late-minute','',9,9,9,1),(0,'late-minute','',2,2,2,1);
                        INSERT INTO samples_1hour VALUES (0,'late','',2,2,2,1),
                            (0,'partial','',5,2,8,2),(0,'late-minute','',2,2,2,1);
                    """)
                    if version == 5:
                        conn.execute("UPDATE samples SET rolled=1")
                        conn.execute("UPDATE samples_1min SET rolled=1")
                    conn.commit()
                    snapshots = {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                                 for table in ("samples", "samples_1min", "samples_1hour")}
                finally:
                    conn.close()
                metrics_store.init_db(path)
                metrics_store.init_db(path)
                conn = metrics_store.open_writer(path)
                try:
                    metrics_store.downsample_minute(conn, now=4000000)
                    metrics_store.downsample_hour(conn, now=4000000)
                    self.assertEqual(metrics_store.prune_old(conn, now=4000000), {"raw": 0, "minute": 0})
                    for table in ("samples", "samples_1min"):
                        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                        expected = [row[:-1] if version == 5 else row for row in snapshots[table]]
                        self.assertEqual([row[:-1] for row in rows], expected)
                        self.assertTrue(all(row[-1] == -1 for row in rows))
                    self.assertEqual(conn.execute("SELECT * FROM samples_1hour").fetchall(), snapshots["samples_1hour"])
                    conn.execute("INSERT INTO samples(ts,metric,tags,value) VALUES(1,'new','',11)")
                    metrics_store.init_db(path)
                    metrics_store.downsample_minute(conn, now=4000000)
                    metrics_store.downsample_hour(conn, now=4000000)
                    self.assertEqual(metrics_store.prune_old(conn, now=4000000), {"raw": 1, "minute": 1})
                    self.assertEqual(conn.execute(
                        "SELECT count,avg_value*count,min_value,max_value FROM samples_1hour WHERE metric='new'"
                    ).fetchone(), (1, 11.0, 11.0, 11.0))
                finally:
                    conn.close()

    def test_release_preserves_direct_and_parent_receipt_lookup(self):
        import ast
        import json
        import sqlite3
        from typing import Any, Iterable, Mapping
        from gateway import media_jobs, receipt_store

        # Exercise the real projection lookup helper without importing UI/services.
        source = ast.parse((ROOT / "gateway/me_media.py").read_text())
        helpers = [node for node in source.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("_job_receipt_ids", "_receipts_by_request_id")]
        namespace = {"Any": Any, "Mapping": Mapping, "Iterable": Iterable,
                     "Path": Path, "receipt_store": receipt_store}
        exec(compile(ast.Module(body=helpers, type_ignores=[]), "<receipt lookup>", "exec"), namespace)
        receipt_ids = namespace["_job_receipt_ids"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.db"
            media_jobs.init_db(path)
            receipts_path = Path(tmp) / "receipts.db"
            receipt_store.init_db(receipts_path)
            for key, parent, receipt_key in (
                ("direct", None, "direct"),
                ("imgjob_seeded_derived", "real-parent", "real-parent"),
            ):
                with self.subTest(key=key):
                    with sqlite3.connect(receipts_path) as conn:
                        conn.execute("INSERT INTO receipts(request_id,created_at,receipt_json) VALUES(?,?,?)",
                                     (receipt_key, 1, json.dumps({"request_id": receipt_key,
                                      "source_kind": "app", "source_label": "Original",
                                      "app_id": "old-app", "requested_model": "old"})))
                    old = media_jobs.create(media_jobs.MediaJob(
                        intent="image", request_id=key, parent_request_id=parent,
                        status=media_jobs.STATUS_SUCCEEDED, created_at=1, updated_at=1), path=path)
                    # Reproduce legacy JSON that predates the durable field.
                    with sqlite3.connect(path) as conn:
                        conn.execute("UPDATE media_jobs SET job_json=json_remove(job_json,'$.creating_receipt_id') WHERE job_id=?",
                                     (old.job_id,))
                    replacement = media_jobs.create(media_jobs.MediaJob(intent="image", request_id=key), path=path)
                    self.assertNotEqual(replacement.job_id, old.job_id)
                    retained = media_jobs.get(old.job_id, path=path)
                    self.assertEqual(retained["request_id"], "")
                    self.assertIn(receipt_key, receipt_ids(retained))
                    self.assertEqual(retained["creating_receipt_id"], receipt_key)
                    receipts = namespace["_receipts_by_request_id"](receipt_ids(retained), path=receipts_path)
                    receipt = next(receipts[k] for k in receipt_ids(retained) if k in receipts)
                    self.assertEqual(receipt["request_id"], receipt_key)
                    self.assertEqual(receipt["source"]["label"], "Original")
                    self.assertEqual(receipt["requested_model"], "old")
                    self.assertEqual(receipt["_stored_at"], 1)
                    # A stale worker must neither replace provenance nor reclaim the key.
                    old.creating_receipt_id = "wrong"
                    with sqlite3.connect(path) as conn:
                        media_jobs._write_job(conn, old)
                    retained = media_jobs.get(old.job_id, path=path)
                    self.assertEqual(retained["creating_receipt_id"], receipt_key)
                    self.assertEqual(retained["request_id"], "")
                    self.assertEqual(media_jobs.get_by_request_id(key, path=path)["job_id"], replacement.job_id)
            erased = media_jobs.create(media_jobs.MediaJob(
                intent="image", request_id="imgjob_seeded_erasure", parent_request_id="real-parent",
                created_at=1, updated_at=1), path=path)
            media_jobs.erase_prompt_derivatives({"*"}, before=int(time.time()) + 1, path=path)
            retained = media_jobs.get(erased.job_id, path=path)
            self.assertEqual(retained["request_id"], "")
            self.assertEqual(retained["creating_receipt_id"], "real-parent")
            self.assertIn("real-parent", receipt_ids(retained))
            self.assertEqual(receipt_ids({"creating_receipt_id": "real", "request_id": "derived",
                                         "parent_request_id": "real"})[0], "real")

    def test_video_concurrent_first_migration_and_setup_cleanup(self):
        import sqlite3
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import Mock, patch
        from gateway import video_job_records

        ddl_barrier = threading.Barrier(2)
        select_barrier = threading.Barrier(2)
        class Racing(sqlite3.Connection):
            def executescript(self, sql):
                result = super().executescript(sql)
                ddl_barrier.wait(timeout=10)
                return result

            def execute(self, sql, *args):
                cursor = super().execute(sql, *args)
                if sql.startswith("SELECT 1 FROM media_jobs_migrations"):
                    row = cursor.fetchone()
                    select_barrier.wait(timeout=10)
                    return Mock(fetchone=lambda: row)
                return cursor

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.db"
            # Legacy row exists but the migration marker does not.
            with sqlite3.connect(path) as conn:
                conn.executescript(video_job_records._DDL)
                conn.execute("INSERT INTO video_job_records VALUES ('v',NULL,1,NULL,?)",
                             ('{"prompt":"private","model":"old"}',))
            def initialize():
                conn = sqlite3.connect(path, factory=Racing, timeout=5)
                try:
                    return video_job_records.migrate(conn)
                finally:
                    conn.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: initialize(), range(2)))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(video_job_records.load_all(path=path), {"v": {"model": "old"}})
            for failure in ("pragmas", "migrate"):
                conn = Mock()
                with patch.object(video_job_records, "connect", return_value=conn), \
                     patch.object(video_job_records.media_jobs, "_apply_pragmas",
                                  side_effect=RuntimeError("setup") if failure == "pragmas" else None), \
                     patch.object(video_job_records, "migrate",
                                  side_effect=RuntimeError("setup") if failure == "migrate" else None):
                    with self.assertRaisesRegex(RuntimeError, "setup"):
                        video_job_records._connect(path)
                    conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
