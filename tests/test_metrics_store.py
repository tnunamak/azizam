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
        """Startup must stay O(1): the full integrity scan is a separate, background call."""
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
