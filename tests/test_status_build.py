"""Status polling must leave the request loop free and reuse work in flight."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from collections import OrderedDict
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
        started_at = []

        def blocking_metrics(*_args):
            started_at.append(time.monotonic())
            started.set()
            time.sleep(1.0)
            return {"available": True}

        self.patches.enter_context(patch.object(status, "_metrics_section", blocking_metrics))
        build = asyncio.create_task(status.get_cached_status({}))
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        self.assertLess(time.monotonic() - started_at[0], 0.1)
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

    async def test_worker_uses_detached_config_and_live_jobs_stay_on_loop(self):
        proxy = self.isolate_sections()
        proxy._video_progress = {"test": {"status": "running", "percent": 50}}
        config = {"images": {"default_model": "original"}}
        loop_thread = threading.get_ident()
        worker_started = threading.Event()
        worker_release = threading.Event()

        def models(worker_config, *_args, **_kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            worker_started.set()
            self.assertTrue(worker_release.wait(timeout=2))
            return {"model": worker_config["images"]["default_model"]}

        live_jobs = status._live_video_jobs

        def jobs(*args):
            self.assertEqual(threading.get_ident(), loop_thread)
            return live_jobs(*args)

        self.patches.enter_context(patch.object(status, "_build_models", models))
        self.patches.enter_context(patch.object(status, "_live_video_jobs", jobs))
        build = asyncio.create_task(status.get_cached_status(config))
        try:
            deadline = time.monotonic() + 2
            while not worker_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertTrue(worker_started.is_set())
            config["images"]["default_model"] = "edited"
            proxy._video_progress["test"]["percent"] = 75
        finally:
            worker_release.set()
        payload = await build
        self.assertEqual(payload["models"], {"model": "original"})
        self.assertEqual(payload["video_jobs"][0]["percent"], 75)

    async def test_cache_ttl_starts_when_build_finishes(self):
        clock = [100.0]

        async def build(_config):
            clock[0] += status._CACHE_TTL_S + 1
            return {"cache_age_s": 0}

        with patch.object(status, "time", SimpleNamespace(monotonic=lambda: clock[0])), patch.object(
            status, "build_status_payload", AsyncMock(side_effect=build)
        ) as build_fn:
            await status.get_cached_status({})
            clock[0] += 1
            self.assertEqual((await status.get_cached_status({}))["cache_age_s"], 1)
            self.assertEqual(build_fn.await_count, 1)

    async def test_disk_pressure_stat_does_not_delay_loop(self):
        self.isolate_sections()
        started = threading.Event()
        started_at = []

        def stat():
            started_at.append(time.monotonic())
            started.set()
            time.sleep(0.5)
            return SimpleNamespace(st_dev=1)

        def path(value):
            if value in ("/test-models", "/test-disk"):
                return SimpleNamespace(stat=stat)
            return Path(value)

        self.patches.enter_context(patch.object(status, "Path", side_effect=path))
        self.patches.enter_context(patch.object(status, "_disk_partitions", return_value=[
            {"target": "/test-disk", "pct_used": 90}
        ]))
        build = asyncio.create_task(status.get_cached_status({
            "model_routing": {"model_dir": "/test-models"}
        }))
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        self.assertLess(time.monotonic() - started_at[0], 0.1)
        self.assertFalse(build.done(), "disk stat blocked the loop until completion")
        before = time.monotonic()
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertIn("models", " ".join((await build)["system_health_reasons"]).lower())


class LifecycleProbeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_preflight_does_not_delay_loop(self):
        started = threading.Event()
        started_at = []
        driver = status.lifecycle_drivers.DockerDriver()

        def inspect(*_args, **_kwargs):
            started_at.append(time.monotonic())
            started.set()
            time.sleep(1.0)
            return SimpleNamespace(returncode=0, stderr=b"", stdout=b"/test")

        target = {"key": "test", "name": "Test", "role": "optional", "service": {
            "lifecycle": {"driver": "docker", "driver_config": {"container": "test"}}
        }}
        with patch.object(status.lifecycle_drivers, "driver_for_service", return_value=driver), patch.object(
            status.lifecycle_drivers.subprocess, "run", side_effect=inspect
        ), patch.object(driver, "status", AsyncMock(return_value="stopped")):
            probe = asyncio.create_task(status._lifecycle_probe(target, {}))
            deadline = time.monotonic() + 5
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            self.assertLess(time.monotonic() - started_at[0], 0.1)
            self.assertFalse(probe.done(), "Docker inspect blocked the loop until completion")
            before = time.monotonic()
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertEqual((await probe)["live_status"], "stopped")


class VideoProbeCacheTests(unittest.TestCase):
    def test_unchanged_file_hits_and_nanosecond_mtime_or_size_invalidates(self):
        with TemporaryDirectory() as directory, patch.object(status, "_FFPROBE_CACHE", OrderedDict()):
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

    def test_cache_is_bounded_and_recent_hits_survive_eviction(self):
        with patch.object(status, "_FFPROBE_CACHE", OrderedDict()), patch.object(
            status, "_FFPROBE_CACHE_MAX", 3
        ), patch.object(status, "_run", return_value=(0, '{"streams": []}', "")) as run:
            for index in range(3):
                status._ffprobe_video(Path(str(index)), size=1, mtime_ns=1)
            status._ffprobe_video(Path("0"), size=1, mtime_ns=1)
            status._ffprobe_video(Path("3"), size=1, mtime_ns=1)
            self.assertEqual(len(status._FFPROBE_CACHE), 3)
            status._ffprobe_video(Path("0"), size=1, mtime_ns=1)
            self.assertEqual(run.call_count, 4)
            status._ffprobe_video(Path("1"), size=1, mtime_ns=1)
            self.assertEqual(run.call_count, 5)

    def test_recent_video_render_rows_do_not_join_prompt_text(self):
        secret = "SECRET video prompt should not reach status"
        with TemporaryDirectory() as directory, patch.object(status, "_FFPROBE_CACHE", OrderedDict()):
            video = Path(directory) / "ltx-2.5_00001_.mp4"
            video.write_bytes(b"video")
            mtime = int(time.time())
            os.utime(video, (mtime, mtime))
            with patch.object(status, "_VIDEO_DIR", Path(directory)), patch.object(
                status, "_run", return_value=(0, '{"streams": []}', "")
            ):
                rows = status._recent_renders_videos(
                    video_meta={"pid-1": {"prompt_truncated": secret}},
                    recent_events=[
                        {
                            "type": "video_submit",
                            "alias": "ltx-2.5",
                            "prompt_id": "pid-1",
                            "ts": mtime,
                        }
                    ],
                )
        self.assertEqual(len(rows), 1)
        self.assertNotIn("prompt", rows[0])
        self.assertNotIn(secret, json.dumps(rows))

    def test_live_video_jobs_do_not_include_prompt_text(self):
        secret = "SECRET live video prompt should not reach status"
        proxy = SimpleNamespace(_video_progress={"pid-1": {"status": "running", "percent": 50}})

        jobs = status._live_video_jobs(
            proxy,
            {"pid-1": {"submitted_at": time.time(), "prompt_truncated": secret}},
        )

        self.assertEqual(len(jobs), 1)
        self.assertNotIn("prompt_truncated", jobs[0])
        self.assertNotIn(secret, json.dumps(jobs))

    def test_recent_image_render_rows_do_not_extract_prompt_metadata(self):
        secret = "SECRET embedded image prompt should not reach status"
        with TemporaryDirectory() as directory, patch.object(status, "_IMG_DIMS_CACHE", OrderedDict()):
            image = Path(directory) / "image.png"
            image.write_bytes(b"not really an image")
            image.with_suffix(".png.json").write_text(json.dumps({"prompt": secret}), encoding="utf-8")
            with patch.object(status, "_IMAGE_DIR", Path(directory)), patch.object(
                status, "_run", return_value=(1, "", "identify unavailable")
            ):
                rows = status._recent_renders_images()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("prompt", rows[0])
        self.assertNotIn(secret, json.dumps(rows))

    def test_daisy_audit_summary_does_not_use_prompt_field(self):
        secret = "SECRET daisy audit prompt should not reach status"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            path.write_text(json.dumps({"ts": 1, "type": "decision", "prompt": secret}) + "\n", encoding="utf-8")
            with patch.object(status, "_DAISY_AUDIT_PATH", path), patch.object(
                status, "_run", return_value=(1, "", "tmux unavailable")
            ):
                summary = status._daisy_audit_summary()
        self.assertNotIn(secret, json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
