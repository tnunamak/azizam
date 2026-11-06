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


class _WorkerGate:
    """Prove loop progress while a worker is blocked, with independent cleanup."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.expired = threading.Event()
        self.worker_threads = []
        self.watchdog = threading.Timer(5, self._expire)
        self.watchdog.daemon = True

    def _expire(self):
        self.expired.set()
        self.release.set()
        self.loop.call_soon_threadsafe(self.entered.set)

    def __enter__(self):
        self.watchdog.start()
        return self

    def block(self):
        self.worker_threads.append(threading.get_ident())
        self.loop.call_soon_threadsafe(self.entered.set)
        self.release.wait()

    async def assert_loop_progress(self, case, task):
        await asyncio.wait_for(self.entered.wait(), timeout=10)
        case.assertFalse(self.expired.is_set(), "Worker watchdog expired before loop progress")
        case.assertTrue(self.worker_threads, "The blocked worker must actually execute")
        case.assertTrue(all(t != self.loop_thread for t in self.worker_threads))
        case.assertFalse(self.release.is_set())
        case.assertFalse(task.done(), "The loop must run before the worker is released")

    def __exit__(self, *_exc):
        self.release.set()
        self.watchdog.cancel()
        self.watchdog.join()


class StatusBuildTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(status, "_SNAPSHOT", {}))

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
        with _WorkerGate() as gate:
            def blocking_metrics(*_args):
                gate.block()
                return {"available": True}

            self.patches.enter_context(patch.object(status, "_metrics_section", blocking_metrics))
            build = asyncio.create_task(status.build_status_payload({}))
            try:
                await gate.assert_loop_progress(self, build)
            finally:
                gate.release.set()
                payload = await asyncio.wait_for(build, timeout=10)
        self.assertEqual(payload["metrics"], {"available": True})

    async def test_worker_uses_detached_config_and_live_jobs_stay_on_loop(self):
        proxy = self.isolate_sections()
        proxy._video_progress = {"test": {"status": "running", "percent": 50}}
        config = {"images": {"default_model": "original"}}
        loop_thread = threading.get_ident()
        with _WorkerGate() as gate:
            def models(worker_config, *_args, **_kwargs):
                self.assertNotEqual(threading.get_ident(), loop_thread)
                gate.block()
                return {"model": worker_config["images"]["default_model"]}

            live_jobs = status._live_video_jobs

            def jobs(*args):
                self.assertEqual(threading.get_ident(), loop_thread)
                return live_jobs(*args)

            self.patches.enter_context(patch.object(status, "_build_models", models))
            self.patches.enter_context(patch.object(status, "_live_video_jobs", jobs))
            build = asyncio.create_task(status.build_status_payload(config))
            try:
                await gate.assert_loop_progress(self, build)
                config["images"]["default_model"] = "edited"
                proxy._video_progress["test"]["percent"] = 75
            finally:
                gate.release.set()
                payload = await asyncio.wait_for(build, timeout=10)
        self.assertEqual(payload["models"], {"model": "original"})
        self.assertEqual(payload["video_jobs"][0]["percent"], 75)

    async def test_disk_pressure_stat_does_not_delay_loop(self):
        self.isolate_sections()
        with _WorkerGate() as gate:
            def stat():
                gate.block()
                return SimpleNamespace(st_dev=1)

            def path(value):
                if value in ("/test-models", "/test-disk"):
                    return SimpleNamespace(stat=stat)
                return Path(value)

            self.patches.enter_context(patch.object(status, "Path", side_effect=path))
            self.patches.enter_context(patch.object(status, "_disk_partitions", return_value=[
                {"target": "/test-disk", "pct_used": 90}
            ]))
            build = asyncio.create_task(status.build_status_payload({
                "model_routing": {"model_dir": "/test-models"}
            }))
            try:
                await gate.assert_loop_progress(self, build)
            finally:
                gate.release.set()
                payload = await asyncio.wait_for(build, timeout=10)
        self.assertIn("models", " ".join(payload["system_health_reasons"]).lower())


class LifecycleProbeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_preflight_does_not_delay_loop(self):
        started = threading.Event()
        inspect_threads = []
        driver = status.lifecycle_drivers.DockerDriver()

        def inspect(*_args, **_kwargs):
            inspect_threads.append(threading.get_ident())
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
            self.assertNotIn(threading.get_ident(), inspect_threads, "Docker inspect ran on the event loop thread")
            self.assertFalse(probe.done(), "Docker inspect blocked the loop until completion")
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
