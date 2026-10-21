"""Identity scenes as async media jobs: submit, progress, content payload, failure, restart cleanup."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import identity_scene as ids  # noqa: E402
from gateway import media_jobs  # noqa: E402


class StubRequest:
    method = "POST"
    scope: dict = {}
    query_params: dict = {}

    def __init__(self) -> None:
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")


def result() -> ids.SceneResult:
    return ids.SceneResult(
        image=b"png-bytes",
        report={"object": "likeness.check", "overall": {"status": "review"}, "people": []},
        attempts=[{"attempt": 1, "seed": 0, "selected": True}],
        people=[{"label": "grandma"}],
        seconds=1.5,
        refinements=[{"person": "baby", "accepted": True}],
        warnings=[{"code": "pose_rewritten", "param": "prompt", "message": "m"}],
        prompt_used="p",
    )


class IdentityJobTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "jobs.db"
        for target in (patch.object(media_jobs, "DB_PATH", self.db),):
            target.start()
            self.addCleanup(target.stop)
        media_jobs.init_db(self.db)
        self.store = patch.object(
            proxy, "_identity_store_image",
            lambda request, auth, content, fmt: {"asset_id": "img_1", "asset_url": "http://x/1", "media_type": "image/png", "b64_json": "AAAA"},
        )
        self.store.start()
        self.addCleanup(self.store.stop)

    def run_job(self, execute):
        proxy.app.state.identity_execute = execute

        async def go():
            response = await proxy._identity_submit_job(
                StubRequest(), proxy.NO_AUTH_CONTEXT, ids.SceneRequest(photos=[b"x"], prompt="p"), "b64_json"
            )
            body = json.loads(response.body)
            await asyncio.gather(*list(proxy._identity_job_tasks))
            return response, body

        return asyncio.run(go())

    def test_submit_returns_202_with_poll_and_content_urls_then_succeeds(self) -> None:
        seen = {}

        async def execute(auth, req):
            req.on_progress("compose", 0.4, {"attempt": 1})
            seen["job"] = media_jobs.recent(limit=1, audience="internal", path=self.db)[0]
            return result()

        response, body = self.run_job(execute)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(body["object"], "media.job")
        self.assertTrue(body["poll_url"].endswith(body["id"]))
        self.assertTrue(body["content_url"].endswith("/content"))
        self.assertEqual(seen["job"]["status"], "running")
        self.assertEqual(seen["job"]["progress"], 40)
        job = media_jobs.get(body["id"], audience="internal", path=self.db)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["progress"], 100)
        payload = proxy._identity_payload_from_media_job(job)
        self.assertEqual(payload["data"][0]["asset_id"], "img_1")
        self.assertNotIn("prompt_used", payload)
        self.assertEqual(payload["likeness"], result().report)
        self.assertEqual(payload["attempts"], result().attempts)
        self.assertEqual(payload["seconds"], 1.5)
        self.assertEqual(payload["warnings"], [{"code": "pose_rewritten", "param": "prompt"}])
        self.assertEqual(payload["refinements"], [{"accepted": True}])
        self.assertNotIn("people", payload)

        events = media_jobs.events(body["id"], audience="internal", path=self.db)
        self.assertIn("progress", [e.get("event_type") or e.get("type") or e.get("event") for e in events])

    def test_failures_become_failed_jobs_with_a_message(self) -> None:
        async def boom(auth, req):
            raise ids.IdentityError("no face was found for grandma", code="no_face")

        _, body = self.run_job(boom)
        job = media_jobs.get(body["id"], audience="internal", path=self.db)
        self.assertEqual(job["status"], "failed")
        self.assertIn("no face", job["error"])

        async def crash(auth, req):
            raise RuntimeError("")

        _, body = self.run_job(crash)
        self.assertIn("RuntimeError", media_jobs.get(body["id"], audience="internal", path=self.db)["error"])

    def test_actionable_validation_errors_survive_async_storage(self) -> None:
        for message in ("people[].image 2 has no matching photo", "at most 4 people are supported"):
            async def fail(auth, req):
                raise ids.IdentityError(message, param="people")
            _, submitted = self.run_job(fail)
            job = media_jobs.get(submitted["id"], audience="internal", path=self.db)
            self.assertEqual(job["error"], message)

    def test_orphaned_jobs_fail_after_restart(self) -> None:
        job = media_jobs.MediaJob(intent="image.identity", request_id="r1")
        media_jobs.create(job, path=self.db)
        other = media_jobs.MediaJob(intent="image.generate", request_id="r2")
        media_jobs.create(other, path=self.db)
        self.assertEqual(proxy._fail_orphaned_identity_jobs(), 1)
        self.assertEqual(media_jobs.get(job.job_id, audience="internal", path=self.db)["status"], "failed")
        self.assertEqual(media_jobs.get(other.job_id, audience="internal", path=self.db)["status"], "queued")

    def test_failed_identity_diagnostics_do_not_store_person_or_prompt_words(self) -> None:
        marker = "PRIVATE_IDENTITY_REFERENCE_LABEL_7f2a"

        async def fail(auth, req):
            raise ids.IdentityError(f"{marker} and another person point at the same face", code="same_face")

        _, submitted = self.run_job(fail)
        job = media_jobs.get(submitted["id"], audience="internal", path=self.db)
        self.assertEqual(job["status"], "failed")
        self.assertNotIn(marker, json.dumps(job))
        self.assertIn("same face", job["error"])


if __name__ == "__main__":
    unittest.main()
