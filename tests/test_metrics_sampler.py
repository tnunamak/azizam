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
        now = 1_800_000_000
        gpus = [
            {
                "index": 2,
                "memory_used_mb": 256,
                "memory_total_mb": 1024,
                "instantaneous_util_pct": 37,
                "temp_c": 61,
            },
            {"index": None, "memory_used_mb": 999},
        ]
        with (
            patch.dict(metrics_sampler._state, {
                "nvkms_pending": None, "nvkms_cursor": None,
            }),
            patch.object(
                metrics_sampler, "_read_new_nvkms_failures", return_value=None,
            ) as journal,
            patch.object(
                metrics_sampler.status_endpoint, "_run",
                side_effect=AssertionError("unexpected host command"),
            ) as host,
            patch.object(
                metrics_sampler.status_endpoint, "_gpus_raw", return_value=gpus,
            ),
            patch.object(
                metrics_sampler.status_endpoint, "_read_sensors",
                return_value={"cpu_max_c": 71, "nvme_max_c": 45, "dimm_max_c": None},
            ),
            patch.object(
                metrics_sampler.status_endpoint, "_load_average",
                return_value=(1.5, 2.0, 3.0),
            ),
            patch.object(
                metrics_sampler.status_endpoint, "_ram_attribution",
                return_value={
                    "total_kb": 4096, "free_kb": 1024, "available_kb": 2048,
                },
            ),
        ):
            metrics_sampler._emit_fast(self._writer, now)
            self._writer.flush()
            journal.assert_called_once_with()
            host.assert_not_called()

        expected = [
            (now, "vram_used_mb", '{"gpu":2}', 256.0),
            (now, "vram_pct", '{"gpu":2}', 25.0),
            (now, "vram_free_mb", '{"gpu":2}', 768.0),
            (now, "gpu_util_pct", '{"gpu":2}', 37.0),
            (now, "gpu_temp_c", '{"gpu":2}', 61.0),
            (now, "cpu_temp_c", "", 71.0),
            (now, "nvme_temp_c", "", 45.0),
            (now, "load_avg_1min", "", 1.5),
            (now, "ram_total_kb", "", 4096.0),
            (now, "ram_free_kb", "", 1024.0),
            (now, "ram_avail_kb", "", 2048.0),
        ]
        with metrics_store.reader(self.db) as conn:
            actual = conn.execute(
                "SELECT ts, metric, tags, value FROM samples ORDER BY metric, tags",
            ).fetchall()
        self.assertEqual(actual, sorted(expected, key=lambda row: (row[1], row[2])))

    def test_emit_slow_does_not_crash(self) -> None:
        now = 1_800_000_000
        config = {"backends": {"fixture": {}}}
        gpus = [{"index": 2}]
        with (
            patch.object(
                metrics_sampler.status_endpoint, "_run",
                side_effect=AssertionError("unexpected host command"),
            ) as host,
            patch.object(
                metrics_sampler.status_endpoint, "_disk_partitions",
                return_value=[{
                    "target": "/data", "avail_bytes": 1024, "pct_used": 25,
                }],
            ),
            patch.object(
                metrics_sampler.status_endpoint, "_ram_attribution",
                return_value={"rows": [{"service": "fixture", "rss_kb": 4096}]},
            ) as ram,
            patch.object(
                metrics_sampler.status_endpoint, "_gpus_raw", return_value=gpus,
            ),
            patch.object(
                metrics_sampler.status_endpoint, "_vram_attribution",
                return_value={"per_gpu": [{
                    "index": 2,
                    "services": [
                        {"service": "fixture", "used_mb": 128},
                        {"service": "__free", "used_mb": 512},
                    ],
                }]},
            ) as vram,
            patch.object(
                metrics_sampler.status_endpoint, "_journal_xids",
                return_value={
                    "by_code_60min": {"109": 2, "31": 1},
                    "by_code_24h": {"109": 5},
                },
            ),
        ):
            metrics_sampler._emit_slow(self._writer, now, config)
            self._writer.flush()
            ram.assert_called_once_with(config)
            vram.assert_called_once_with(gpus, config)
            host.assert_not_called()

        expected = [
            (now, "disk_avail_bytes", '{"mount":"/data"}', 1024.0),
            (now, "disk_pct_used", '{"mount":"/data"}', 25.0),
            (now, "service_rss_kb", '{"service":"fixture"}', 4096.0),
            (now, "service_vram_mb", '{"gpu":2,"service":"fixture"}', 128.0),
            (now, "xid_109_60min", "", 2.0),
            (now, "xid_total_60min", "", 3.0),
            (now, "xid_109_24h", "", 5.0),
        ]
        with metrics_store.reader(self.db) as conn:
            actual = conn.execute(
                "SELECT ts, metric, tags, value FROM samples ORDER BY metric, tags",
            ).fetchall()
        self.assertEqual(actual, sorted(expected, key=lambda row: (row[1], row[2])))


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
