from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class UnfinishedOpenAIVideoJobsTests(unittest.TestCase):
    def test_counts_only_recent_unfinished_jobs(self) -> None:
        now = int(time.time())
        records = {
            "rendering": {"created_at": now - 60, "completed_at": None},
            "queued": {"created_at": now - 5, "completed_at": None},
            "done-polled": {"created_at": now - 60, "completed_at": now - 1},
            "done-unpolled": {"created_at": now - 60, "completed_at": None},
            "failed-unpolled": {"created_at": now - 60, "completed_at": None},
            "deleted": {"created_at": now - 60, "completed_at": None, "deleted_at": now},
            "stale": {"created_at": now - 3 * 3600, "completed_at": None},
        }
        progress = {
            "rendering": {"status": "running"},
            "done-unpolled": {"status": "succeeded"},
            "failed-unpolled": {"status": "failed"},
        }
        with patch.object(proxy, "_video_job_records", records), patch.object(
            proxy, "_video_progress", progress
        ), patch.object(proxy.media_jobs, "get", return_value=None):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 2)

    def test_counter_is_registered_with_the_drain(self) -> None:
        now = int(time.time())
        with patch.object(
            proxy, "_video_job_records", {"p": {"created_at": now, "completed_at": None}}
        ), patch.object(proxy, "_video_progress", {}), patch.object(proxy.media_jobs, "get", return_value=None):
            snapshot = proxy.DRAIN_STATE.snapshot()
        self.assertEqual(snapshot["active"]["async_media_jobs"].get("openai_video"), 1)
        self.assertFalse(snapshot["idle"])

    def test_lost_completion_uses_cached_renderer_terminal_status(self) -> None:
        now = int(time.time())
        records = {"finished": {"created_at": now - 60, "completed_at": None, "seconds": "5"}}
        progress = {"finished": {"status": "succeeded", "updated_at": now - 1}}
        with patch.object(proxy, "_video_job_records", records), patch.object(proxy, "_video_progress", progress), patch.object(
            proxy.media_jobs, "get", return_value=None
        ):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 0)

    def test_stalled_job_past_expected_duration_does_not_block_drain(self) -> None:
        now = int(time.time())
        records = {"stalled": {"created_at": now - 3600, "completed_at": None, "seconds": "5"}}
        progress = {"stalled": {"status": "running", "updated_at": now - 3600}}
        with patch.object(proxy, "_video_job_records", records), patch.object(proxy, "_video_progress", progress), patch.object(
            proxy.media_jobs, "get", return_value=None
        ):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 0)

    def test_terminal_gateway_media_job_does_not_block_drain(self) -> None:
        now = int(time.time())
        records = {"done": {"created_at": now - 60, "completed_at": None, "seconds": "5", "media_job_id": "mj_done"}}
        progress = {"done": {"status": "running", "updated_at": now - 1}}
        terminal = {"job_id": "mj_done", "intent": "video.generate", "status": proxy.media_jobs.STATUS_SUCCEEDED}
        with patch.object(proxy, "_video_job_records", records), patch.object(proxy, "_video_progress", progress), patch.object(
            proxy.media_jobs, "get", return_value=terminal
        ):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 0)

    def test_genuinely_running_job_still_counts(self) -> None:
        now = int(time.time())
        records = {"running": {"created_at": now - 60, "completed_at": None, "seconds": "5"}}
        progress = {"running": {"status": "running", "updated_at": now - 60}}
        with patch.object(proxy, "_video_job_records", records), patch.object(proxy, "_video_progress", progress), patch.object(
            proxy.media_jobs, "get", return_value=None
        ):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 1)


if __name__ == "__main__":
    unittest.main()
