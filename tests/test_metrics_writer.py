"""Persistence ownership, overload, shutdown, and pre-queue SQL equivalence."""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

import metrics_sampler
import metrics_store
from gateway import metrics_writer


def legacy_rollups(conn, now):
    """Frozen SQL from fd837b9, independent of the new maintenance dispatch."""
    for source, dest, width, aggregates in (
        ("samples", "samples_1min", 60, "AVG(value), MIN(value), MAX(value), COUNT(*)"),
        ("samples_1min", "samples_1hour", 3600,
         "SUM(avg_value*count)*1.0/NULLIF(SUM(count),0), MIN(min_value), MAX(max_value), SUM(count)"),
    ):
        end = now - now % width
        start = end - width
        if conn.execute(f"SELECT 1 FROM {dest} WHERE ts = ? LIMIT 1", (start,)).fetchone():
            continue
        rows = conn.execute(
            f"SELECT metric, tags, {aggregates} FROM {source} "
            "WHERE ts >= ? AND ts < ? GROUP BY metric, tags", (start, end),
        ).fetchall()
        conn.executemany(f"INSERT INTO {dest} VALUES (?, ?, ?, ?, ?, ?, ?)", [(start, *r) for r in rows])
    conn.commit()


class MetricsWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "metrics.db"

    def writer(self, **kwargs):
        writer = metrics_writer.MetricsWriter(self.db, **kwargs)
        self.addCleanup(writer.close)
        writer.wait_ready()
        return writer

    def test_queue_preserves_legacy_rows_rollups_and_retention(self):
        now = 50_004_000  # hour-aligned; worker wall clock is deliberately unrelated
        tags = '{"gpu":0}'
        rows = [
            (now - 60, "x", "", 1.0), (now - 50, "x", "", 7.0),
            (now - 40, "x", tags, -2.0), (now - 30, "x", tags, 4.0),
            (now - 20, "x", tags, 10.0), (now, "x", "", 999.0),
            (now - metrics_store.RAW_RETAIN_S - 1, "old", "", 5.0),
        ]
        golden_db = Path(self.tmp.name) / "golden.db"
        metrics_store.init_db(golden_db)
        writer = self.writer()
        for db in (golden_db, self.db):
            with sqlite3.connect(db) as conn:
                conn.execute("INSERT INTO samples_1min VALUES (?, 'x', '', 3, 2, 4, 7)", (now - 120,))
                conn.execute("INSERT INTO samples_1min VALUES (?, 'old', '', 5, 5, 5, 1)",
                             (now - metrics_store.MINUTE_RETAIN_S - 1,))
                conn.execute("INSERT INTO samples_1hour VALUES (?, 'old', '', 5, 5, 5, 1)",
                             (now - metrics_store.HOUR_RETAIN_S - 1,))
        with sqlite3.connect(golden_db) as conn:
            metrics_store.record_samples(conn, rows)  # original synchronous insert path
            legacy_rollups(conn, now)
            legacy_rollups(conn, now)  # duplicate buckets must stay unchanged
            for table, retention in (("samples", metrics_store.RAW_RETAIN_S),
                                     ("samples_1min", metrics_store.MINUTE_RETAIN_S),
                                     ("samples_1hour", metrics_store.HOUR_RETAIN_S)):
                conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - retention,))
        self.assertTrue(writer.record_samples(rows))
        for operation in ("minute", "hour", "minute", "hour", "prune"):
            self.assertTrue(writer.maintain(operation, now))
        writer.close()  # drains accepted commands even without an explicit flush
        with sqlite3.connect(golden_db) as expected, sqlite3.connect(self.db) as actual:
            for table in ("samples", "samples_1min", "samples_1hour"):
                sql = f"SELECT * FROM {table} ORDER BY ts, metric, tags, 4"
                self.assertEqual(actual.execute(sql).fetchall(), expected.execute(sql).fetchall(), table)
            self.assertEqual(actual.execute(
                "SELECT avg_value, count FROM samples_1hour WHERE metric='x' AND tags=''"
            ).fetchone(), (29 / 9, 9))

    def test_overflow_drops_newest_and_counts_rows_without_blocking(self):
        entered = threading.Event()
        release = threading.Event()
        original = metrics_store.record_samples

        def blocked(conn, rows):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release writer")
            original(conn, rows)

        with patch.object(metrics_store, "record_samples", blocked):
            writer = self.writer(capacity=1)
            self.addCleanup(release.set)
            self.assertTrue(writer.record_sample(1, "first", 1))
            self.assertTrue(entered.wait(2))
            self.assertTrue(writer.record_sample(2, "second", 2))
            before = time.monotonic()
            self.assertFalse(writer.record_samples([(3, "drop", "", 3), (3, "drop", "", 4)]))
            self.assertFalse(writer.maintain("minute", 60))
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertEqual(writer.stats["dropped_samples"], 2)
            self.assertEqual(writer.stats["dropped_commands"], 2)
            release.set()
            writer.close()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT metric FROM samples ORDER BY ts").fetchall(),
                             [("first",), ("second",)])

    def test_sqlite_connection_and_checkpoint_never_cross_threads(self):
        calls = []

        class TrackedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                calls.append((threading.get_ident(), sql))
                return super().execute(sql, *args)

            def executemany(self, sql, *args):
                calls.append((threading.get_ident(), sql))
                return super().executemany(sql, *args)

            def close(self):
                calls.append((threading.get_ident(), "close"))
                return super().close()

        def open_writer(path):
            calls.append((threading.get_ident(), "open"))
            return sqlite3.connect(path, isolation_level=None, factory=TrackedConnection)

        with patch.object(metrics_store, "open_writer", open_writer):
            writer = self.writer()
            writer.record_sample(1, "x", 2)
            writer.maintain("minute", 60)
            writer.close()
        self.assertEqual(len({tid for tid, _ in calls}), 1)
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertIn("PRAGMA wal_checkpoint(TRUNCATE);", [sql for _, sql in calls])
        self.assertEqual(calls[-1][1], "close")

    def test_shutdown_flushes_every_accepted_sample_and_rejects_late_submissions(self):
        writer = self.writer()
        accepted = sum(writer.record_sample(i, "x", i) for i in range(200))
        writer.close()
        self.assertFalse(writer.record_sample(201, "late", 0))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], accepted)
        self.assertFalse(writer._thread.is_alive())

    def test_shutdown_drains_sample_admitted_after_worker_observes_empty_queue(self):
        empty_seen = threading.Event()
        resume = threading.Event()
        closing = threading.Event()
        base_queue = queue.Queue

        class PausedEmpty(base_queue):
            def get(self, *args, **kwargs):
                try:
                    return super().get(*args, **kwargs)
                except queue.Empty:
                    if not empty_seen.is_set():
                        empty_seen.set()
                        if not resume.wait(5):
                            raise RuntimeError("test did not release empty observation")
                    raise

        with patch.object(metrics_writer.queue, "Queue", PausedEmpty):
            writer = self.writer()
        try:
            self.assertTrue(empty_seen.wait(2))
            self.assertTrue(writer.record_sample(1, "accepted", 7))
            join = writer._thread.join

            def observed_join(*args, **kwargs):
                closing.set()
                return join(*args, **kwargs)

            with patch.object(writer._thread, "join", observed_join):
                closer = threading.Thread(target=writer.close)
                closer.start()
                try:
                    self.assertTrue(closing.wait(2))
                finally:
                    resume.set()
                    closer.join(5)
            self.assertFalse(closer.is_alive())
        finally:
            resume.set()
            writer.close()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT ts, metric, value FROM samples").fetchall(),
                             [(1, "accepted", 7)])
        self.assertEqual(writer.stats["dropped_samples"], 0)
        self.assertEqual(writer.stats["write_errors"], 0)

    def test_persistence_failure_is_reported_by_flush_and_close(self):
        writer = self.writer()
        # Avoid the successful-close cleanup registered by writer() for this error case.
        self._cleanups.pop()
        with patch.object(metrics_store, "record_samples", side_effect=sqlite3.DatabaseError("test failure")):
            writer.record_sample(1, "x", 2)
            with self.assertRaisesRegex(RuntimeError, "persistence failed"):
                writer.flush()
            with self.assertRaisesRegex(RuntimeError, "persistence failed"):
                writer.close()
        self.assertEqual(writer.stats["write_errors"], 1)
        self.assertFalse(writer._thread.is_alive())

    def test_flush_waits_for_its_snapshot_without_waiting_for_newer_samples(self):
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        flushed = threading.Event()
        newer_entered = threading.Event()
        newer_release = threading.Event()
        original = metrics_store.record_samples

        def blocked(conn, rows):
            event, gate = (entered, release) if rows[0][0] == 1 else (newer_entered, newer_release)
            event.set()
            if not gate.wait(5):
                raise RuntimeError("test did not release writer")
            original(conn, rows)

        with patch.object(metrics_store, "record_samples", blocked):
            writer = self.writer()
            writer.record_sample(1, "first", 1)
            self.assertTrue(entered.wait(2))
            wait_for = writer._drained.wait_for

            def wait(predicate):
                waiting.set()  # target was captured while holding the admission lock
                return wait_for(predicate)

            def flush():
                writer.flush()
                flushed.set()

            with patch.object(writer._drained, "wait_for", wait):
                flusher = threading.Thread(target=flush)
                flusher.start()
                try:
                    self.assertTrue(waiting.wait(2))
                    writer.record_sample(2, "newer", 2)
                    release.set()
                    self.assertTrue(newer_entered.wait(2))
                    self.assertTrue(flushed.wait(0.1))
                finally:
                    release.set()
                    newer_release.set()
                    flusher.join(5)
                    writer.close()

    def test_restart_cannot_create_another_owner_while_shutdown_drains(self):
        entered = threading.Event()
        release = threading.Event()
        original = metrics_store.record_samples

        def blocked(conn, rows):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release writer")
            original(conn, rows)

        with (
            patch.object(metrics_store, "record_samples", blocked),
            patch.object(metrics_store, "DB_PATH", self.db),
            patch.object(metrics_writer, "_writer", None),
        ):
            writer = metrics_writer.start_writer()
            writer.wait_ready()
            writer.record_sample(1, "first", 1)
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=metrics_writer.close_writer)
            closer.start()
            try:
                deadline = time.monotonic() + 2
                while writer.stats["accepting"] and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertFalse(writer.stats["accepting"])
                with self.assertRaisesRegex(RuntimeError, "still closing"):
                    metrics_writer.start_writer()
                self.assertIs(metrics_writer.get_writer(), writer)
            finally:
                release.set()
                closer.join(5)
                writer.close()
            replacement = metrics_writer.start_writer()
            replacement.wait_ready()
            self.assertIsNot(replacement, writer)
            metrics_writer.close_writer()


class WriterResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_health_request_answers_while_write_sleeps_one_second(self):
        import proxy

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.db"
            entered = threading.Event()
            original = metrics_store.record_samples

            def slow(conn, rows):
                entered.set()
                time.sleep(1.0)
                original(conn, rows)

            writer = metrics_writer.MetricsWriter(db)
            await asyncio.to_thread(writer.wait_ready)
            try:
                with patch.object(metrics_store, "record_samples", slow), patch.object(metrics_writer, "_writer", writer):
                    metrics_sampler.record_request("request", 1, key=None, path="/health", status=200)
                    deadline = time.monotonic() + 2
                    while not entered.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.001)
                    self.assertTrue(entered.is_set())
                    started = time.monotonic()
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://test") as client:
                        response = await asyncio.wait_for(client.get("/health"), timeout=0.1)
                    elapsed = time.monotonic() - started
                    self.assertEqual(response.status_code, 200)
                    self.assertLess(elapsed, 0.1)
                    print(f"health during 1s metrics write: {elapsed * 1000:.3f} ms")
                    await asyncio.to_thread(writer.close)
            finally:
                await asyncio.to_thread(writer.close)

    async def test_sampler_probes_and_disk_check_run_off_loop(self):
        from unittest.mock import AsyncMock

        main_thread = threading.get_ident()
        seen = []
        first_sleep = True
        original_sleep = asyncio.sleep

        async def sleep(_delay):
            nonlocal first_sleep
            if first_sleep:
                first_sleep = False
                return
            raise asyncio.CancelledError

        def disk(_path):
            seen.append(("disk", threading.get_ident()))
            time.sleep(0.05)
            return 10_000_000_000

        def probe(kind):
            def run(_writer, _now):
                seen.append((kind, threading.get_ident()))
                time.sleep(0.05)
            return run

        writer = unittest.mock.Mock()
        with (
            patch.object(metrics_sampler, "get_writer", return_value=writer),
            patch.object(metrics_sampler, "close_writer"),
            patch.object(metrics_sampler.time, "time", return_value=86400.0),
            patch.object(metrics_sampler.asyncio, "sleep", sleep),
            patch.object(metrics_store, "disk_avail_bytes", disk),
            patch.object(metrics_sampler, "_emit_fast", probe("fast")),
            patch.object(metrics_sampler, "_emit_slow", probe("slow")),
            patch.object(metrics_sampler, "_emit_fast_async", AsyncMock()),
            patch.dict(metrics_sampler._state, {"last_minute_rolled": 0, "last_hour_rolled": 0, "last_prune_day": 0}),
        ):
            task = asyncio.create_task(metrics_sampler.sampler_loop())
            before = time.monotonic()
            await original_sleep(0.01)
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertFalse(task.done())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({name for name, _ in seen}, {"disk", "fast", "slow"})
        self.assertTrue(all(tid != main_thread for _, tid in seen))
        self.assertEqual(writer.maintain.call_args_list, [
            unittest.mock.call("minute", 86400), unittest.mock.call("hour", 86400), unittest.mock.call("prune", 86400),
        ])
