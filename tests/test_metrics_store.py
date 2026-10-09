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
                    f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
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
            conn.execute("INSERT INTO samples VALUES (100,'service_latency_ms','{}',42)")
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
                conn.execute(f"INSERT INTO {table} VALUES (60, 'x', '', 2, 1, 3, 2)")
            conn.execute("UPDATE schema_version SET version=2")
        metrics_store.init_db(self.db)
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], metrics_store.SCHEMA_VERSION)
            for table in ("samples_1min", "samples_1hour"):
                self.assertEqual(conn.execute(f"SELECT * FROM {table}").fetchall(), [(60, 'x', '', 2, 1, 3, 2)])
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
            conn.execute("INSERT INTO samples VALUES (60, 'x', '', 2)")
            conn.execute("UPDATE schema_version SET version=3")
        metrics_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT * FROM samples").fetchall(), [(60, "x", "", 2.0)])
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

    def test_query_series(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            base = int(time.time()) - 600
            for i in range(60):
                metrics_store.record_sample(conn, base + i * 10, "z", float(i))
            series = metrics_store.query_series("z", since_s=600, points=10)
            # 10 buckets of 60s; values 0..59 partitioned, so 10 points expected.
            self.assertGreaterEqual(len(series), 1)
            # Boundary buckets can produce up to N+1 entries depending on
            # alignment of `now` against the 60s bucket size.
            self.assertLessEqual(len(series), 12)
            for ts, _v in series:
                self.assertIsInstance(ts, int)
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
            # Sample 8 days old should be pruned from raw.
            metrics_store.record_sample(conn, now - 8 * 86400, "p", 1.0)
            # Sample 1h old should survive.
            metrics_store.record_sample(conn, now - 3600, "p", 2.0)
            counts = metrics_store.prune_old(conn)
            self.assertEqual(counts["raw"], 1)
            with metrics_store.reader(self.db) as c:
                rows = c.execute(
                    "SELECT count(*) FROM samples WHERE metric='p'"
                ).fetchone()
            self.assertEqual(rows[0], 1)
        finally:
            conn.close()

    def test_db_health(self) -> None:
        conn = metrics_store.open_writer(self.db)
        try:
            metrics_store.record_sample(conn, int(time.time()), "h", 1.0)
        finally:
            conn.close()
        h = metrics_store.db_health(self.db)
        self.assertTrue(h["recent_write_ok"])
        self.assertGreater(h["total_rows"] or 0, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
