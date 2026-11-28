import sys
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import status_endpoint as status
import test_status_build as _base


class StatusEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    setUp = _base.StatusBuildTests.setUp
    isolate_sections = _base.StatusBuildTests.isolate_sections

    async def test_real_builder_keeps_history_when_a_host_probe_fails(self):
        self.isolate_sections()
        # Eliminate the remaining host/filesystem reads used by the builder.
        for name, result in {
            "_build_models": {},
            "_capabilities": [],
            "_image_capabilities": [],
            "_journal_xids": {},
            "_uptime_since": None,
            "_kernel_recent_events": [],
            "_reboot_history": [],
            "_proxy_restart_count": 0,
            "_journal_disk_usage": 0,
            "_images_budget": {},
            "_status_config_snapshot": {},
            "_status_auth_key_names": ([], {}),
        }.items():
            self.patches.enter_context(patch.object(status, name, return_value=result))
        self.patches.enter_context(patch.object(status, "_HOTSPOT_DIRS", []))
        self.patches.enter_context(patch.object(
            status, "_gpu_workloads", AsyncMock(return_value=[]),
        ))
        self.patches.enter_context(patch.object(
            status, "_gpus_raw", side_effect=OSError("fixture sensor failed"),
        ))
        fake_firehose = SimpleNamespace(
            snapshot_video_meta=lambda: {},
            snapshot_per_key=lambda _names: [],
            snapshot_window=lambda: {},
            snapshot_recent_events=lambda _n: [],
        )
        # Patch the module reference, not the process-wide importlib function.
        self.patches.enter_context(patch.object(
            status, "importlib",
            SimpleNamespace(import_module=lambda _name: fake_firehose),
        ))
        history = {
            "uptime_pct_24h": 75.0, "uptime_pct_7d": 80.0,
            "avg_latency_ms_24h": 12, "p95_latency_ms_24h": 20,
            "avg_latency_ms_7d": 15, "p95_latency_ms_7d": 25,
            "sample_count_24h": 4,
            "status_24h_series": [(100, 1.0), (200, 0.0)],
            "latency_ms_24h_series": [(100, 12.0)],
        }
        self.patches.enter_context(patch.object(
            status, "_metrics_section",
            return_value={"available": True, "services": {"backends": {"test": history}}},
        ))

        payload = await status.build_status_payload({})

        self.assertIn("fixture sensor failed", payload["errors"]["gpus"])
        self.assertEqual(payload["backends"][0]["live_status"], "up")
        self.assertEqual(payload["banner"], "ok")
        self.assertEqual(payload["backends"][0]["history"], {
            "uptime_pct_24h": 75.0, "uptime_pct_7d": 80.0,
            "avg_latency_ms_24h": 12, "p95_latency_ms_24h": 20,
            "avg_latency_ms_7d": 15, "p95_latency_ms_7d": 25,
            "sample_count_24h": 4,
            "status_series": [(100, 1.0), (200, 0.0)],
            "latency_series": [(100, 12.0)],
        })
