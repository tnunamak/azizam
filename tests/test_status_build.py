"""Status polling must leave the request loop free and reuse work in flight."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import status_endpoint as status


class StatusBuildTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(status, "_CACHE", {"ts": 0.0, "data": None}))
        if hasattr(status, "_BUILD_TASK"):
            self.patches.enter_context(patch.object(status, "_BUILD_TASK", None))

    def isolate_sections(self):
        """Exercise the actual builder without host, service or filesystem probes."""
        proxy = SimpleNamespace(_video_progress={}, VOICE_MAP={})
        self.patches.enter_context(patch.dict(sys.modules, {"__main__": proxy, "proxy": proxy}))
        for name, result in {
            "_backend_grid": [{"key": "test", "name": "Test", "role": "required", "live_status": "up"}],
            "_comfy_queue_and_stats": (None, None),
            "_llama_props_and_slots": (None, None),
            "_enriched_aux_health": {},
        }.items():
            self.patches.enter_context(patch.object(status, name, AsyncMock(return_value=result)))
        for name, result in {
            "_run": (1, "", "unavailable"), "_gpus_raw": [],
            "_vram_attribution": {}, "_ram_attribution": {}, "_repo_state": {},
            "_comfy_custom_nodes": [], "_disk_partitions": [], "_dir_summary": {},
            "_read_sensors": {}, "_cpu_usage_quick": {}, "_recent_renders_videos": [],
            "_recent_renders_images": [], "_read_vram_estimates": {}, "_read_gpu_budget": {},
            "_record_backend_health_samples": None, "_metrics_section": {},
            "_daisy_audit_summary": {}, "_daisy_pi_context": {},
            "_daisy_telegram_bridge": {}, "_workflow_drift_status": {},
        }.items():
            self.patches.enter_context(patch.object(status, name, return_value=result))
        return proxy

    async def test_blocking_section_does_not_delay_loop(self):
        self.isolate_sections()
        started = threading.Event()

        def blocking_metrics(*_args):
            started.set()
            time.sleep(1.0)
            return {"available": True}

        self.patches.enter_context(patch.object(status, "_metrics_section", blocking_metrics))
        build = asyncio.create_task(status.get_cached_status({}))
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        self.assertFalse(build.done(), "the event loop could not run until the section finished")
        before = time.monotonic()
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        self.assertLess(time.monotonic() - before, 0.1)
        payload = await build
        self.assertEqual(payload["metrics"], {"available": True})

    async def test_concurrent_callers_share_one_build(self):
        release = asyncio.Event()
        calls = 0

        async def build(_config):
            nonlocal calls
            calls += 1
            await release.wait()
            return {"cache_age_s": 0, "backends": []}

        self.patches.enter_context(patch.object(status, "build_status_payload", build))
        tasks = [asyncio.create_task(status.get_cached_status({})) for _ in range(12)]
        await asyncio.sleep(0.02)
        release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(calls, 1)
        self.assertTrue(all(row == results[0] for row in results))

    async def test_failure_releases_all_waiters_and_can_retry(self):
        release = asyncio.Event()
        calls = 0

        async def build(_config):
            nonlocal calls
            calls += 1
            await release.wait()
            raise RuntimeError("failed build")

        self.patches.enter_context(patch.object(status, "build_status_payload", build))
        tasks = [asyncio.create_task(status.get_cached_status({})) for _ in range(3)]
        await asyncio.sleep(0.02)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(row, RuntimeError) for row in results))
        self.assertEqual(calls, 1)
        self.assertIsNone(status._CACHE["data"])
        with patch.object(status, "build_status_payload", AsyncMock(return_value={"ok": True})):
            self.assertEqual(await status.get_cached_status({}), {"ok": True})

    async def test_cancelled_caller_does_not_cancel_shared_build(self):
        release = asyncio.Event()
        build_fn = AsyncMock(side_effect=lambda _config: None)

        async def build(_config):
            await release.wait()
            return {"ok": True}

        build_fn.side_effect = build
        self.patches.enter_context(patch.object(status, "build_status_payload", build_fn))
        first = asyncio.create_task(status.get_cached_status({}))
        second = asyncio.create_task(status.get_cached_status({}))
        await asyncio.sleep(0.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, {"ok": True})
        self.assertEqual(build_fn.await_count, 1)


class VideoProbeCacheTests(unittest.TestCase):
    def test_unchanged_file_hits_and_nanosecond_mtime_or_size_invalidates(self):
        with TemporaryDirectory() as directory, patch.object(status, "_FFPROBE_CACHE", {}):
            video = Path(directory) / "test_0001_.mp4"
            video.write_bytes(b"video")
            probe_json = json.dumps({"format": {"duration": "1"}, "streams": []})
            with patch.object(status, "_VIDEO_DIR", Path(directory)), patch.object(
                status, "_run", return_value=(0, probe_json, "")
            ) as run:
                first = status._recent_renders_videos()
                self.assertEqual(first, status._recent_renders_videos())
                self.assertEqual(run.call_count, 1)
                old_stat = video.stat()
                os.utime(video, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1))
                status._recent_renders_videos()
                self.assertEqual(run.call_count, 2)
                video.write_bytes(b"longer video")
                os.utime(video, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1))
                status._recent_renders_videos()
                self.assertEqual(run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
