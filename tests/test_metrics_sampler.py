"""Standalone tests for metrics_sampler. Run via:

    python3 tests/test_metrics_sampler.py

Validates that the sampler's emit functions don't crash with missing
upstream data and that tag JSON round-trips correctly through SQLite.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics_store  # noqa: E402
import metrics_sampler  # noqa: E402


class TestMetricsSampler(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self._orig_path = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)
        # Force the sampler to use a fresh writer pointed at our temp db.
        metrics_sampler.close_writer()
        self._writer = metrics_sampler.start_writer()

    def tearDown(self) -> None:
        try:
            metrics_sampler.close_writer()
        except Exception:
            pass
        metrics_store.DB_PATH = self._orig_path
        self.tmp.cleanup()

    def test_emit_fast_does_not_crash(self) -> None:
        now = int(time.time())
        with (
            patch.object(metrics_sampler.status_endpoint, "_gpus_raw", return_value=[] ) as gpus,
            patch.object(metrics_sampler.status_endpoint, "_read_sensors", return_value={}) as sensors,
            patch.object(metrics_sampler.status_endpoint, "_load_average", return_value=(0.0, 0.0, 0.0)) as load,
            patch.object(metrics_sampler.status_endpoint, "_ram_attribution", return_value={}) as ram,
        ):
            metrics_sampler._emit_fast(self._writer, now)
        for probe in (gpus, sensors, load, ram):
            probe.assert_called_once_with()

    def test_emit_slow_does_not_crash(self) -> None:
        now = int(time.time())
        with (
            patch.object(metrics_sampler.status_endpoint, "_disk_partitions", return_value=[]) as disks,
            patch.object(metrics_sampler.status_endpoint, "_ram_attribution", return_value={}) as ram,
            patch.object(metrics_sampler.status_endpoint, "_gpus_raw", return_value=[]) as gpus,
            patch.object(metrics_sampler.status_endpoint, "_vram_attribution", return_value={}) as vram,
            patch.object(metrics_sampler.status_endpoint, "_journal_xids", return_value={}) as journal,
        ):
            metrics_sampler._emit_slow(self._writer, now)
        for probe in (disks, gpus, journal):
            probe.assert_called_once_with()
        ram.assert_called_once_with(None)
        vram.assert_called_once_with([], None)

    def test_record_request_writes_with_tags(self) -> None:
        metrics_sampler.record_request(
            "request",
            12.5,
            key="test-key",
            path="/v1/chat/completions",
            status=200,
        )
        self._writer.flush()
        # Read back.
        with metrics_store.reader(self.db) as c:
            rows = c.execute(
                "SELECT tags, value FROM samples WHERE metric='request'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        tags = json.loads(rows[0][0])
        self.assertEqual(tags["key"], "test-key")
        self.assertEqual(tags["path"], "/v1/chat/completions")
        self.assertEqual(tags["status"], 200)
        self.assertAlmostEqual(rows[0][1], 12.5)

    def test_record_request_safe_without_key(self) -> None:
        metrics_sampler.record_request(
            "request",
            5.0,
            key=None,
            path="/health",
            status=200,
        )
        self._writer.flush()
        with metrics_store.reader(self.db) as c:
            rows = c.execute(
                "SELECT tags FROM samples WHERE metric='request' AND value=5.0"
            ).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        # Anonymous request should have status + path but no `key`.
        tags = json.loads(rows[0][0])
        self.assertNotIn("key", tags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
