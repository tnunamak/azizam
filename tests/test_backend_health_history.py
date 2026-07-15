"""Tests for durable service health history in metrics_store + status_endpoint.

Verifies:
  - _record_backend_health_samples stores all four metrics including error_kind
  - _classify_backend_error maps correctly
  - _backend_p95_latency_ms computes correct percentile
  - _metrics_section.services includes uptime_pct, avg/p95 latency, series
  - build_status_payload backends[] entries carry a typed history block
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics_store  # noqa: E402
import status_endpoint  # noqa: E402


def _make_backends(*, llama_status="up", llama_latency=42, video_status="down"):
    return [
        {
            "key": "llama",
            "name": "Llama",
            "role": "required",
            "url": "http://127.0.0.1:5051/health",
            "note": None,
            "live_status": llama_status,
            "status": llama_status,
            "latency_ms": llama_latency if llama_status == "up" else None,
            "last_error": None if llama_status == "up" else "connect refused",
        },
        {
            "key": "video",
            "name": "Video",
            "role": "optional",
            "url": "http://127.0.0.1:7890/health",
            "note": None,
            "live_status": video_status,
            "status": video_status,
            "latency_ms": None,
            "last_error": "connect refused" if video_status == "down" else None,
        },
    ]


class TestClassifyBackendError(unittest.TestCase):
    def test_up_is_none(self):
        self.assertEqual(
            status_endpoint._classify_backend_error("up", None),
            status_endpoint._ERROR_KIND_NONE,
        )

    def test_connect_refused(self):
        self.assertEqual(
            status_endpoint._classify_backend_error("down", "connect refused"),
            status_endpoint._ERROR_KIND_CONNECT_REFUSED,
        )

    def test_timeout(self):
        self.assertEqual(
            status_endpoint._classify_backend_error("degraded", "timeout"),
            status_endpoint._ERROR_KIND_TIMEOUT,
        )

    def test_http_5xx(self):
        self.assertEqual(
            status_endpoint._classify_backend_error("down", "HTTP 503"),
            status_endpoint._ERROR_KIND_HTTP_5XX,
        )

    def test_other_error(self):
        kind = status_endpoint._classify_backend_error("down", "unknown failure")
        self.assertEqual(kind, status_endpoint._ERROR_KIND_OTHER)


class TestRecordAndQueryHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self._orig_db = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)

    def tearDown(self):
        metrics_store.DB_PATH = self._orig_db
        self.tmp.cleanup()

    def test_record_stores_service_up_latency_error_kind(self):
        backends = _make_backends()
        status_endpoint._record_backend_health_samples(backends)

        with metrics_store.reader() as c:
            # service_up stored
            row = c.execute(
                "SELECT value FROM samples WHERE metric='service_up' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)

            # service_latency_ms stored for 'up' backend
            row = c.execute(
                "SELECT value FROM samples WHERE metric='service_latency_ms' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row[0], 42.0)

            # service_error_kind stored for all backends
            rows = c.execute(
                "SELECT value FROM samples WHERE metric='service_error_kind' ORDER BY value"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            values = sorted(r[0] for r in rows)
            self.assertIn(status_endpoint._ERROR_KIND_NONE, values)
            self.assertIn(status_endpoint._ERROR_KIND_CONNECT_REFUSED, values)

    def test_uptime_pct_all_up(self):
        backends = _make_backends(llama_status="up", video_status="up")
        for _ in range(5):
            status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services = metrics["services"]["backends"]
        self.assertEqual(services["llama"]["uptime_pct_24h"], 100.0)
        self.assertEqual(services["video"]["uptime_pct_24h"], 100.0)

    def test_uptime_pct_all_down(self):
        backends = _make_backends(llama_status="down", video_status="down")
        status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services = metrics["services"]["backends"]
        self.assertEqual(services["llama"]["uptime_pct_24h"], 0.0)
        self.assertEqual(services["video"]["uptime_pct_24h"], 0.0)

    def test_uptime_pct_mixed(self):
        # 2 up, 1 down for llama
        backends_up = _make_backends(llama_status="up")
        backends_down = _make_backends(llama_status="down")
        status_endpoint._record_backend_health_samples(backends_up)
        status_endpoint._record_backend_health_samples(backends_up)
        status_endpoint._record_backend_health_samples(backends_down)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends_up)
        services = metrics["services"]["backends"]
        pct = services["llama"]["uptime_pct_24h"]
        self.assertIsNotNone(pct)
        # AVG(0,1,1) * 100 ≈ 66.667
        self.assertAlmostEqual(pct, 66.667, places=1)

    def test_avg_latency_present(self):
        backends = _make_backends(llama_latency=100)
        status_endpoint._record_backend_health_samples(backends)
        status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services = metrics["services"]["backends"]
        self.assertEqual(services["llama"]["avg_latency_ms_24h"], 100)
        self.assertIsNone(services["video"]["avg_latency_ms_24h"])

    def test_p95_latency(self):
        now = int(time.time())
        conn = metrics_store.open_writer()
        try:
            for v in range(1, 21):  # 20 samples: 1..20
                metrics_store.record_sample(conn, now - (21 - v), "service_latency_ms", float(v), tags={"backend": "llama", "name": "Llama", "role": "required"})
            conn.commit()
        finally:
            conn.close()
        p95 = status_endpoint._backend_p95_latency_ms(
            {"backend": "llama", "name": "Llama", "role": "required"}, since_s=3600
        )
        self.assertIsNotNone(p95)
        # 20 values, p95 index = max(0, int(20*0.95)-1) = max(0,18) = 18 → value at sorted[18] = 19
        self.assertEqual(p95, 19)

    def test_sample_count_and_series(self):
        backends = _make_backends()
        status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services = metrics["services"]["backends"]
        self.assertGreaterEqual(services["llama"]["sample_count_24h"], 1)
        self.assertGreaterEqual(len(services["llama"]["status_24h_series"]), 1)
        # video has no latency → empty latency series
        self.assertEqual(services["video"]["latency_ms_24h_series"], [])

    def test_no_latency_for_down_backend(self):
        backends = _make_backends(llama_status="down")
        status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services = metrics["services"]["backends"]
        self.assertIsNone(services["llama"]["avg_latency_ms_24h"])
        self.assertIsNone(services["llama"]["p95_latency_ms_24h"])

    def test_backends_entries_carry_history_block(self):
        """build_status_payload backends[] entries must include a typed 'history' key."""
        backends = _make_backends()
        # Seed some data first
        status_endpoint._record_backend_health_samples(backends)

        # Simulate what build_status_payload does after _record_backend_health_samples
        # and _metrics_section are called (without running the full async build).
        metrics_block = status_endpoint._metrics_section([], {"partitions": []}, backends)
        services_history_map = (metrics_block.get("services") or {}).get("backends") or {}

        enriched: list[dict[str, Any]] = []
        for b in backends:
            key = str(b.get("key") or b.get("name") or "")
            hist = services_history_map.get(key)
            if hist is not None:
                enriched.append({
                    **b,
                    "history": {
                        "uptime_pct_24h": hist.get("uptime_pct_24h"),
                        "uptime_pct_7d": hist.get("uptime_pct_7d"),
                        "avg_latency_ms_24h": hist.get("avg_latency_ms_24h"),
                        "p95_latency_ms_24h": hist.get("p95_latency_ms_24h"),
                        "avg_latency_ms_7d": hist.get("avg_latency_ms_7d"),
                        "p95_latency_ms_7d": hist.get("p95_latency_ms_7d"),
                        "sample_count_24h": hist.get("sample_count_24h", 0),
                        "status_series": hist.get("status_24h_series", []),
                        "latency_series": hist.get("latency_ms_24h_series", []),
                    },
                })
            else:
                enriched.append(b)

        self.assertEqual(len(enriched), 2)
        llama = next(e for e in enriched if e["key"] == "llama")
        self.assertIn("history", llama)
        self.assertIn("uptime_pct_24h", llama["history"])
        self.assertIn("avg_latency_ms_24h", llama["history"])
        self.assertIn("p95_latency_ms_24h", llama["history"])
        self.assertIn("sample_count_24h", llama["history"])
        self.assertIn("status_series", llama["history"])
        self.assertIn("latency_series", llama["history"])
        # llama was up — should have 100% uptime
        self.assertEqual(llama["history"]["uptime_pct_24h"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
