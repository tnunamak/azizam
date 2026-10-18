"""A media job whose worker crashes is logged and labelled, never swallowed.

Every kind of job (image, audio, video, make) ends its task in a done
callback. A worker that raises must leave a log line with the stack (but not
the exception's text, which may hold a prompt) and a failed job with typed
fields that say the gateway itself broke.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import proxy
from gateway import media_jobs

SECRET = "a secret red fox at dawn"


async def _crash(*_args, **_kwargs) -> None:
    raise RuntimeError(f"worker broke near {SECRET}")


class MediaJobCrashTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs = Path(tmp.name) / "media_jobs.db"
        media_jobs.init_db(self.jobs)
        for target, name, value in (
            (media_jobs, "DB_PATH", self.jobs),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", Path(tmp.name) / "payloads"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def new_job(self, intent: str) -> str:
        job = media_jobs.MediaJob(intent=intent, request_id=f"req_{intent}", metadata={"mode": "test"})
        media_jobs.create(job, path=self.jobs)
        return job.job_id

    async def settle(self, tasks: set[asyncio.Task]) -> None:
        pending = list(tasks)
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)  # done callbacks run on the next loop turn

    def assert_crash_recorded(self, job_id: str, logs) -> None:
        job = media_jobs.get(job_id, audience="client", path=self.jobs)
        self.assertEqual(job["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(job["error_category"], "internal")
        self.assertEqual(job["error_code"], "internal_error")
        self.assertIs(job["retryable"], True)
        self.assertNotIn(SECRET, job["error"])
        text = "\n".join(logs.output)
        self.assertIn(job_id, text)
        self.assertIn("RuntimeError", text)
        self.assertIn("_crash", text)  # the stack names where it broke
        self.assertNotIn(SECRET, text)

    async def test_image_audio_and_video_crashes(self) -> None:
        for intent, runner, schedule, tasks in (
            ("image.generate", "_run_image_media_job", proxy._schedule_image_media_job, proxy._image_media_job_tasks),
            ("audio.generate", "_run_audio_media_job", proxy._schedule_audio_media_job, proxy._audio_media_job_tasks),
            ("video.generate", "_run_video_media_job", proxy._schedule_video_media_job, proxy._video_media_job_tasks),
        ):
            with self.subTest(intent):
                job_id = self.new_job(intent)
                with patch.object(proxy, runner, _crash), self.assertLogs("azizam.media_jobs", "ERROR") as logs:
                    self.assertTrue(schedule(job_id))
                    await self.settle(tasks)
                self.assert_crash_recorded(job_id, logs)

    async def test_make_job_crash(self) -> None:
        job_id = self.new_job("image.generate")
        with self.assertLogs("azizam.media_jobs", "ERROR") as logs:
            proxy._SESSION_MAKE_RUNTIME.schedule(job_id, _crash)
            await self.settle(proxy._image_media_job_tasks)
        self.assert_crash_recorded(job_id, logs)


if __name__ == "__main__":
    unittest.main()
