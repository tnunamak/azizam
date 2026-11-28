"""Persistence ownership, overload, shutdown, rollup coverage and retention."""

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
        raw_old, minute_old = now - metrics_store.RAW_RETAIN_S - 1, now - metrics_store.MINUTE_RETAIN_S - 1
        tags = '{"gpu":0}'
        rows = [
            (now - 60, "x", "", 1.0), (now - 50, "x", "", 7.0),
            (now - 40, "x", tags, -2.0), (now - 30, "x", tags, 4.0),
            (now - 20, "x", tags, 10.0), (now, "x", "", 999.0),
            (raw_old, "old", "", 5.0),
        ]
        writer = self.writer()
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO samples_1min (ts, metric, tags, avg_value, min_value, max_value, count) "
                         "VALUES (?, 'x', '', 3, 2, 4, 7)", (now - 120,))
            conn.execute("INSERT INTO samples_1min (ts, metric, tags, avg_value, min_value, max_value, count) "
                         "VALUES (?, 'old', '', 5, 5, 5, 1)", (minute_old,))
            # Hourly rollups are the only long-term copy; prune must keep them.
            conn.execute("INSERT INTO samples_1hour VALUES (?, 'old', '', 5, 5, 5, 1)", (now - 400 * 24 * 3600,))
        self.assertTrue(writer.record_samples(rows))
        for operation in ("minute", "hour", "minute", "hour", "prune"):
            self.assertTrue(writer.maintain(operation, now))
        writer.close()  # drains accepted commands even without an explicit flush
        with sqlite3.connect(self.db) as actual:
            # Old rows that never had a rollup are summarized before they expire.
            self.assertEqual(actual.execute("SELECT * FROM samples ORDER BY ts, tags").fetchall(), [
                (now - 60, "x", "", 1.0, 1), (now - 50, "x", "", 7.0, 1),
                (now - 40, "x", tags, -2.0, 1), (now - 30, "x", tags, 4.0, 1),
                (now - 20, "x", tags, 10.0, 1), (now, "x", "", 999.0, 0),
            ])
            self.assertEqual(actual.execute("SELECT * FROM samples_1min ORDER BY ts, tags").fetchall(), [
                (raw_old - raw_old % 60, "old", "", 5.0, 5.0, 5.0, 1, 1),
                (now - 120, "x", "", 3.0, 2.0, 4.0, 7, 1),
                (now - 60, "x", "", 4.0, 1.0, 7.0, 2, 1),
                (now - 60, "x", tags, 4.0, -2.0, 10.0, 3, 1),
            ])
            self.assertEqual(actual.execute("SELECT * FROM samples_1hour ORDER BY ts, tags").fetchall(), [
                (now - 400 * 24 * 3600, "old", "", 5.0, 5.0, 5.0, 1),
                (minute_old - minute_old % 3600, "old", "", 5.0, 5.0, 5.0, 1),
                (raw_old - raw_old % 3600, "old", "", 5.0, 5.0, 5.0, 1),
                (now - 3600, "x", "", 29 / 9, 1.0, 7.0, 9),
                (now - 3600, "x", tags, 4.0, -2.0, 10.0, 3),
            ])

    def test_missed_rollups_catch_up_before_prune(self):
        now = 50_004_000
        start = now - 10 * 24 * 3600  # gateway down for days: no rollup ran
        samples = [(start + i * 600, "gpu", "", float(i)) for i in range(5)]
        writer = self.writer()
        self.assertTrue(writer.record_samples(samples))
        self.assertTrue(writer.maintain("prune", now))  # before any rollup: keeps everything
        writer.flush()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone(), (5,))
        for operation in ("minute", "hour", "prune"):
            self.assertTrue(writer.maintain(operation, now))
        writer.close()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone(), (0,))
            self.assertEqual(conn.execute(
                "SELECT SUM(count), SUM(avg_value * count), MIN(min_value), MAX(max_value) FROM samples_1hour"
            ).fetchone(), (5, 10.0, 0.0, 4.0))

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
            # Nonblocking means these return while the writer gate stays closed;
            # a blocking put would stay stuck here until the watchdog expires.
            results = []
            overflow = threading.Thread(target=lambda: results.extend([
                writer.record_samples([(3, "drop", "", 3), (3, "drop", "", 4)]),
                writer.maintain("minute", 60),
            ]))
            overflow.start()
            overflow.join(2)
            self.assertFalse(overflow.is_alive())
            self.assertEqual(results, [False, False])
            self.assertEqual(writer.stats["dropped_samples"], 2)
            self.assertEqual(writer.stats["dropped_commands"], 2)
            release.set()
            overflow.join()
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

    def test_samples_with_state_commit_together_or_not_at_all(self):
        writer = self.writer()
        committed = writer.record_samples_with_state([(1, "x", "", 1.0)], {"cursor": "c1"})
        self.assertIs(committed.wait(5), True)
        with patch.object(metrics_store, "write_sampler_state", side_effect=sqlite3.OperationalError("disk I/O error")):
            failed = writer.record_samples_with_state([(2, "x", "", 2.0)], {"cursor": "c2"})
            self.assertIs(failed.wait(5), False)
        with writer._lock:
            writer._accepting = False
        self.assertIsNone(writer.record_samples_with_state([(3, "x", "", 3.0)], {"cursor": "c3"}))
        with writer._lock:
            writer._accepting = True
        writer._error = None  # the injected failure above is the only one expected
        writer.flush()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT ts FROM samples").fetchall(), [(1,)])
        self.assertEqual(metrics_store.read_sampler_state(self.db, "cursor"), "c1")

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
                    # newer_release stays closed: only the snapshot may be awaited.
                    self.assertTrue(flushed.wait(2))
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
    async def test_real_health_request_answers_while_metrics_write_is_in_flight(self):
        import proxy

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "metrics.db"
            entered = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            original = metrics_store.record_samples

            def blocked(conn, rows):
                entered.set()
                release.wait(5)  # watchdog only; the test releases explicitly
                original(conn, rows)
                finished.set()

            writer = metrics_writer.MetricsWriter(db)
            await asyncio.to_thread(writer.wait_ready)
            try:
                with patch.object(metrics_store, "record_samples", blocked), patch.object(metrics_writer, "_writer", writer):
                    metrics_sampler.record_request("request", 1, key=None, path="/health", status=200)
                    deadline = time.monotonic() + 2
                    while not entered.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.001)
                    self.assertTrue(entered.is_set())
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://test") as client:
                        # Finite only so a hang fails; the oracle is the gated write below.
                        response = await asyncio.wait_for(client.get("/health"), timeout=5)
                    self.assertEqual(response.status_code, 200)
                    # The write was still gated when the route answered.
                    self.assertFalse(finished.is_set())
                    release.set()
                    await asyncio.to_thread(writer.close)
            finally:
                release.set()
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

        entered = threading.Event()
        release = threading.Event()

        def disk(_path):
            seen.append(("disk", threading.get_ident()))
            entered.set()
            release.wait(2)  # watchdog only; the test releases explicitly
            return 10_000_000_000

        def probe(kind):
            def run(_writer, _now, *_config):
                seen.append((kind, threading.get_ident()))
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
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # The disk probe is held closed, yet the loop still runs: it is off-loop.
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({name for name, _ in seen}, {"disk", "fast", "slow"})
        self.assertTrue(all(tid != main_thread for _, tid in seen))
        self.assertEqual(writer.maintain.call_args_list, [
            unittest.mock.call("minute", 86400), unittest.mock.call("hour", 86400), unittest.mock.call("prune", 86400),
        ])
