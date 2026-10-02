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
        ):
            self.assertEqual(proxy._unfinished_openai_video_jobs(), 2)

    def test_counter_is_registered_with_the_drain(self) -> None:
        now = int(time.time())
        with patch.object(
            proxy, "_video_job_records", {"p": {"created_at": now, "completed_at": None}}
        ), patch.object(proxy, "_video_progress", {}):
            snapshot = proxy.DRAIN_STATE.snapshot()
        self.assertEqual(snapshot["active"]["async_media_jobs"].get("openai_video"), 1)
        self.assertFalse(snapshot["idle"])


if __name__ == "__main__":
    unittest.main()
