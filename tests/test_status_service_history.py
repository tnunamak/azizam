from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics_store  # noqa: E402
import status_endpoint  # noqa: E402


class StatusServiceHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self._orig_db = metrics_store.DB_PATH
        metrics_store.DB_PATH = self.db
        metrics_store.init_db(self.db)

    def tearDown(self) -> None:
        metrics_store.DB_PATH = self._orig_db
        self.tmp.cleanup()

    def test_backend_probe_samples_feed_service_history_contract(self) -> None:
        backends = [
            {
                "key": "llama",
                "name": "Llama",
                "role": "required",
                "live_status": "up",
                "latency_ms": 42,
                "last_error": None,
            },
            {
                "key": "video",
                "name": "Video",
                "role": "optional",
                "live_status": "down",
                "latency_ms": None,
                "last_error": "connect refused",
            },
        ]

        status_endpoint._record_backend_health_samples(backends)
        metrics = status_endpoint._metrics_section([], {"partitions": []}, backends)

        services = metrics["services"]["backends"]
        self.assertEqual(services["llama"]["uptime_pct_24h"], 100.0)
        self.assertEqual(services["video"]["uptime_pct_24h"], 0.0)
        self.assertGreaterEqual(services["llama"]["sample_count_24h"], 1)
        self.assertGreaterEqual(len(services["llama"]["status_24h_series"]), 1)
        self.assertGreaterEqual(len(services["llama"]["latency_ms_24h_series"]), 1)
        self.assertEqual(services["video"]["latency_ms_24h_series"], [])

    def test_probe_exposes_service_id_from_backend_key(self) -> None:
        class FakeClient:
            async def get(self, _url: str):
                return SimpleNamespace(status_code=200)

        row = asyncio.run(
            status_endpoint._probe(
                FakeClient(),
                "svc_tts",
                "Voxtral TTS",
                "http://127.0.0.1:8123/health",
                "optional",
                None,
            )
        )

        self.assertEqual(row["key"], "svc_tts")
        self.assertEqual(row["service_id"], "svc_tts")
        self.assertEqual(row["live_status"], "up")

    def test_auth_key_app_id_enrichment_only_when_unique(self) -> None:
        rows = [
            {"name": "paperless", "requests_24h": 12},
            {"name": "shared-name", "requests_24h": 3},
            {"name": "unknown", "requests_24h": 1},
        ]

        status_endpoint._attach_unique_app_ids_to_auth_keys(
            rows,
            {
                "paperless": {"app_paperless"},
                "shared-name": {"app_a", "app_b"},
            },
        )

        self.assertEqual(rows[0]["app_id"], "app_paperless")
        self.assertNotIn("app_id", rows[1])
        self.assertNotIn("app_id", rows[2])


if __name__ == "__main__":
    unittest.main()
