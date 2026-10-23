"""The gateway's session make runtime: it runs make jobs through the image routes."""
from __future__ import annotations

import asyncio
from tests.support.app_keys import accountable_person
import base64
import json
import time
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import accounts, admin_store, me_make, media_delivery, media_jobs, prompt_memory, receipt_store  # noqa: E402
from tests.support.access import store_plan  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n-source-picture-bytes"
MADE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
SOURCE = "img_" + "5e" * 32
OUTPUT = "img_" + "0a" * 32


class SessionMakeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(root / "jobs.db")
        receipt_store.init_db(root / "receipts.db")
        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", root / "jobs.db"),
            (receipt_store, "DB_PATH", root / "receipts.db"),
            (proxy, "IMAGE_CACHE_DIR", root / "cache"),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            (proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {}),
            (proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "cache").mkdir()
        proxy.asset_cache_path(SOURCE).write_bytes(PNG)
        self.root = root
        self.user = admin_store.upsert_oidc_user("issuer", "maker", email="maker@example.test", path=self.admin)
        accounts.initialize(self.admin)
        accounts.change_person(self.user["id"], "test", path=self.admin,
                               plan_id=store_plan("Everything", path=self.admin)["id"])
        self.app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self.runtime = proxy._SessionMakeRuntime()
        # The source picture is in the person's Library: an earlier job of theirs made it.
        source_job = media_jobs.MediaJob(intent="image.generate", request_id="req_source",
                                         app_id=self.app["id"], status=media_jobs.STATUS_SUCCEEDED,
                                         assets=[{"asset_id": SOURCE, "media_type": "image/png"}])
        media_jobs.create(source_job, path=root / "jobs.db")

    def _payload(self, intent: str = "image.edit", **body) -> dict:
        body = {"model": "image", "prompt": "private make prompt", **body}
        if intent == "image.edit":
            body.setdefault("source_asset_id", SOURCE)
        return {"job_id": "mj_runtime", "intent": intent, "body": body,
                "auth": {"app_id": self.app["id"], "person_id": self.user["id"]},
                "created_at": 1_700_000_000}

    def _ok(self) -> Response:
        return Response(content=json.dumps({"data": [{"asset_id": OUTPUT, "url": "http://x/y"}]}),
                        status_code=200, media_type="application/json")

    def test_edit_replays_the_source_picture_as_the_session_person(self) -> None:
        seen = {}

        async def fake_edit(request, auth_context, receipt=None):
            form = await request.form()
            seen["fields"] = {key: value for key, value in form.multi_items() if not hasattr(value, "read")}
            upload = form.get("image")
            seen["image"] = await upload.read()
            seen["identity"] = auth_context.identity
            seen["receipt_app"] = receipt.app_id
            return self._ok()

        payload = self._payload(extensions={prompt_memory.NAMESPACE: {"mode": "forget"}})
        with patch.object(proxy, "proxy_graph_image_edit_request", fake_edit):
            assets = asyncio.run(self.runtime.execute(payload))
        self.assertEqual([asset["asset_id"] for asset in assets], [OUTPUT])
        self.assertEqual(seen["image"], PNG)
        self.assertEqual(seen["fields"]["prompt"], "private make prompt")
        self.assertEqual(seen["fields"]["response_format"], "url")
        self.assertNotIn("source_asset_id", seen["fields"])
        self.assertEqual(json.loads(seen["fields"]["extensions"]),
                         {prompt_memory.NAMESPACE: {"mode": "forget"}})
        self.assertEqual(seen["identity"].app_id, self.app["id"])
        self.assertEqual(seen["identity"].person_id, self.user["id"])
        self.assertEqual(seen["receipt_app"], self.app["id"])
        self.assertEqual(payload["body"]["source_asset_id"], SOURCE, "the stored payload is not mutated")

    def test_edit_refuses_a_source_the_person_deleted_after_asking(self) -> None:
        async def fake_edit(request, auth_context, receipt=None):
            raise AssertionError("a deleted source must never be read")

        admin_store.record_audit("media.asset.delete", actor_kind="user", actor_id=self.user["id"],
                                 target_kind="media_asset", target_id=SOURCE, path=self.admin)
        self.assertTrue(proxy.asset_cache_path(SOURCE).exists(), "a shared copy can outlive the delete")
        with patch.object(proxy, "proxy_graph_image_edit_request", fake_edit):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload()))
        self.assertEqual(raised.exception.http_status, 404)
        self.assertIn("no longer in your Library", raised.exception.public_message)

    def test_edit_refuses_a_cached_source_outside_the_persons_library(self) -> None:
        stranger_picture = "img_" + "7d" * 32
        proxy.asset_cache_path(stranger_picture).write_bytes(PNG)
        async def fake_edit(request, auth_context, receipt=None):
            raise AssertionError("another person's picture must never be read")

        with patch.object(proxy, "proxy_graph_image_edit_request", fake_edit):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload(source_asset_id=stranger_picture)))
        self.assertEqual(raised.exception.http_status, 404)

    def test_generate_runs_the_json_image_route(self) -> None:
        seen = {}

        async def fake_generate(request, auth_context, receipt=None):
            seen["body"] = await request.json()
            return self._ok()

        with patch.object(proxy, "proxy_graph_image_request", fake_generate):
            assets = asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertEqual(assets[0]["asset_id"], OUTPUT)
        self.assertEqual(seen["body"], {"model": "image", "prompt": "private make prompt",
                                        "response_format": "url"})

    def test_generate_uses_the_neutral_protocol_dispatch_entry(self) -> None:
        async def fake_generate(request, auth_context, receipt=None):
            return self._ok()
        with patch.object(proxy, "proxy_image_request", fake_generate), patch.object(
            proxy, "proxy_graph_image_request", side_effect=AssertionError("bypassed neutral dispatch")
        ):
            assets = asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertEqual(assets[0]["asset_id"], OUTPUT)

    def test_only_the_persons_own_enabled_session_app_can_run(self) -> None:
        other = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], access_plan="person", path=self.admin)
        stranger = admin_store.upsert_oidc_user("issuer", "stranger", email="s@example.test", path=self.admin)
        cases = {
            "ordinary App": {"app_id": other["id"], "person_id": self.user["id"]},
            "another person": {"app_id": self.app["id"], "person_id": stranger["id"]},
        }
        for label, auth in cases.items():
            with self.subTest(label):
                with self.assertRaises(me_make.MakeBackendError) as raised:
                    asyncio.run(self.runtime.execute({**self._payload(), "auth": auth}))
                self.assertEqual(raised.exception.http_status, 403)
        admin_store.disable_app(self.app["id"], path=self.admin)
        with self.assertRaises(me_make.MakeBackendError):
            asyncio.run(self.runtime.execute(self._payload()))

    def test_backend_failure_keeps_only_transport_evidence(self) -> None:
        async def failing(request, auth_context, receipt=None):
            return Response(content=json.dumps({"error": {"message": "cannot draw private make prompt"}}),
                            status_code=500, media_type="application/json")

        with patch.object(proxy, "proxy_graph_image_request", failing):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertNotIn("private make prompt", raised.exception.public_message)
        self.assertEqual(raised.exception.http_status, 500)

    def test_a_policy_denial_stops_the_worker_before_any_image_route_runs(self) -> None:
        # The image routes check policy again, so the worker's own refusal is
        # only visible as "the delegate never starts". Kills M-INV-make-worker-ignores-denial.
        denial = proxy.PolicyDecision(decision="denied", sources=("app_profile",),
                                      reasons=("images are off for this app",))
        for intent, delegate in (("image.generate", "proxy_graph_image_request"),
                                 ("image.edit", "proxy_graph_image_edit_request")):
            started = []

            async def spy(request, auth_context, receipt=None, _started=started):
                _started.append(receipt)
                return self._ok()

            with self.subTest(intent=intent), \
                    patch.object(proxy, "_apply_image_route_policy", return_value=denial), \
                    patch.object(proxy, delegate, spy):
                with self.assertRaises(me_make.MakeBackendError) as raised:
                    asyncio.run(self.runtime.execute(self._payload(intent)))
                self.assertEqual(raised.exception.http_status, 403)
                self.assertEqual(started, [], "a denied job must not start the image route")

    def _admission(self, **cfg) -> None:
        patcher = patch.object(proxy, "_effective_media_job_admission_cfg", return_value=cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_make_meets_the_same_operator_admission_as_media_jobs(self) -> None:
        self._admission(disabled_intents=["image.generate"])
        regular = proxy._media_job_admission_response("image.generate")
        refused = self.runtime.admission("image.generate")
        self.assertIsNotNone(regular)
        self.assertIsNotNone(refused, "make must not bypass the operator's admission")
        self.assertEqual((refused.status_code, refused.body, refused.headers.get("retry-after")),
                         (regular.status_code, regular.body, regular.headers.get("retry-after")))
        self.assertEqual(refused.status_code, 503)

    def test_execution_rechecks_operator_admission(self) -> None:
        async def never(request, auth_context, receipt=None):
            raise AssertionError("a refused make job never reaches the backend")

        self._admission(disabled_intents=["image.generate"])
        with patch.object(proxy, "proxy_graph_image_request", never):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual("This kind of work is disabled.", raised.exception.public_message)

    def test_execution_does_not_count_its_own_job_against_the_queue_limit(self) -> None:
        media_jobs.create(media_jobs.MediaJob(job_id="mj_runtime", intent="image.generate", request_id="req_self",
                                              app_id=self.app["id"], status=media_jobs.STATUS_RUNNING),
                          path=media_jobs.DB_PATH)
        self._admission(max_queue_depth=1)
        self.assertIsNotNone(proxy._media_job_admission_response("image.generate"), "the queue is full")

        async def fake_generate(request, auth_context, receipt=None):
            return self._ok()

        with patch.object(proxy, "proxy_graph_image_request", fake_generate):
            assets = asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertEqual(assets[0]["asset_id"], OUTPUT)

    def test_disabling_the_image_class_refuses_edits_at_submit_and_execution(self) -> None:
        # Review repro (P2-6): disabled_resource_classes=["image"] still admitted an edit.
        async def never(request, auth_context, receipt=None):
            raise AssertionError("a refused edit never reaches the backend")

        self._admission(disabled_resource_classes=["image"])
        for intent in ("image.generate", "image.edit"):
            refused = self.runtime.admission(intent)
            self.assertIsNotNone(refused, f"{intent} is image work")
            self.assertEqual(refused.status_code, 503)
        with patch.object(proxy, "proxy_graph_image_edit_request", never):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload("image.edit")))
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual("This kind of resource is disabled.", raised.exception.public_message)

    def test_queued_edit_and_identity_jobs_count_toward_the_queue_limit(self) -> None:
        # Review repro (P2-6): a queued edit counted 0 toward max_queue_depth. Identity
        # scene jobs are the regular (non-make) edit-shaped media jobs, with the same gap.
        self._admission(max_queue_depth=1)
        for index, intent in enumerate(("image.edit", proxy._IDENTITY_JOB_INTENT)):
            job_id = f"mj_queued_{index}"
            media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent=intent, request_id=f"req_{job_id}",
                                                  app_id=self.app["id"]), path=media_jobs.DB_PATH)
            self.assertEqual(proxy._media_job_queue_depth(1), 1, f"a queued {intent} job is queued work")
            refused = proxy._media_job_admission_response("image.generate")
            self.assertIsNotNone(refused, f"a queued {intent} job fills a one-job queue")
            self.assertEqual("Shared capacity is busy.", json.loads(refused.body)["error"]["message"])
            media_jobs.update_status(job_id, media_jobs.STATUS_CANCELED, path=media_jobs.DB_PATH)

    def test_at_most_the_configured_number_of_make_jobs_run_at_once(self) -> None:
        running, peak = 0, 0

        def job(job_id: str):
            async def run() -> None:
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1
            return run

        async def main() -> None:
            with patch.object(proxy, "_image_media_job_semaphore", asyncio.Semaphore(2)):
                for index in range(8):
                    self.runtime.schedule(f"mj_bounded_{index}", job(f"mj_bounded_{index}"))
                await asyncio.gather(*list(proxy._image_media_job_tasks))

        asyncio.run(main())
        self.assertEqual(peak, 2)

    def test_canceled_queued_jobs_leave_the_scheduler_at_once(self) -> None:
        # Review repro: while the image semaphore is busy, repeated submit + cancel
        # left one waiting task and gate per canceled job, none counted outstanding.
        jobs_db = media_jobs.DB_PATH
        executed: list[str] = []

        async def execute(payload):
            executed.append(payload["job_id"])
            return [{"asset_id": OUTPUT, "media_type": "image/png"}]

        def submit(job_id: str) -> None:
            media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent="image.generate", request_id=f"req_{job_id}",
                                                  app_id=self.app["id"], metadata={"mode": me_make.JOB_MODE}),
                              path=jobs_db)
            self.runtime.keep_payload(job_id, {**self._payload("image.generate"), "job_id": job_id})
            self.runtime.schedule(job_id, me_make.run_job(job_id, runtime=self.runtime, jobs_path=jobs_db))

        def cancel(job_id: str) -> None:  # What the cancel route does, on its worker thread.
            media_jobs.update_status(job_id, media_jobs.STATUS_CANCELED, error="Canceled by requester",
                                     expected_status=media_jobs.STATUS_QUEUED, path=jobs_db)
            self.assertTrue(self.runtime.request_cancel(job_id))
            self.runtime.drop_payload(job_id)

        def outstanding() -> int:
            return sum(len(media_jobs.recent(500, status=status, path=jobs_db))
                       for status in (media_jobs.STATUS_QUEUED, media_jobs.STATUS_RUNNING))

        async def main() -> tuple[int, int, int]:
            busy = asyncio.Semaphore(1)
            with patch.object(proxy, "_image_media_job_semaphore", busy), \
                    patch.object(self.runtime, "execute", execute):
                await busy.acquire()  # Another job holds the only slot.
                submit("mj_kept")
                for index in range(60):
                    submit(f"mj_cycle_{index}")
                    await asyncio.to_thread(cancel, f"mj_cycle_{index}")
                for _ in range(5):
                    await asyncio.sleep(0)
                retained = (len(proxy._image_media_job_tasks), len(self.runtime._gates),
                            len(proxy._image_media_job_scheduled))
                busy.release()
                await asyncio.gather(*list(proxy._image_media_job_tasks))
            return retained

        tasks, gates, scheduled = asyncio.run(main())
        self.assertEqual((tasks, gates, scheduled), (1, 1, 1), "only the job still waiting is retained")
        self.assertEqual(executed, ["mj_kept"], "a canceled job never runs")
        self.assertEqual(outstanding(), 0)
        canceled = media_jobs.recent(500, status=media_jobs.STATUS_CANCELED, path=jobs_db)
        self.assertEqual(len(canceled), 60)
        self.assertEqual((proxy._image_media_job_tasks, self.runtime._gates), (set(), {}))

    def test_a_resumed_job_canceled_while_it_waits_settles_as_canceled(self) -> None:
        # After a restart a job resumes as running but still waits for a turn.
        jobs_db = media_jobs.DB_PATH
        job_id = "mj_resumed"
        media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent="image.generate", request_id="req_resumed",
                                              app_id=self.app["id"], metadata={"mode": me_make.JOB_MODE}),
                          path=jobs_db)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=jobs_db)
        self.runtime.keep_payload(job_id, {**self._payload("image.generate"), "job_id": job_id})

        async def never(request, auth_context, receipt=None):
            raise AssertionError("a canceled job never reaches the backend")

        def cancel() -> None:  # The cancel route's path for a job that is not queued.
            self.assertTrue(self.runtime.request_cancel(job_id))
            try:
                media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, expected_status=media_jobs.STATUS_RUNNING,
                                         metadata_patch={"cancel_requested_at": 1}, path=jobs_db)
            except ValueError:  # It already settled.
                pass
            self.runtime.drop_payload(job_id)

        async def main() -> int:
            busy = asyncio.Semaphore(1)
            with patch.object(proxy, "_image_media_job_semaphore", busy), \
                    patch.object(proxy, "proxy_graph_image_request", never):
                await busy.acquire()
                self.runtime.schedule(job_id, me_make.run_job(job_id, runtime=self.runtime, jobs_path=jobs_db))
                await asyncio.sleep(0)
                await asyncio.to_thread(cancel)
                for _ in range(5):
                    await asyncio.sleep(0)
                return len(proxy._image_media_job_tasks)

        self.assertEqual(asyncio.run(main()), 0, "it does not wait for a turn it will not use")
        self.assertEqual(media_jobs.get(job_id, path=jobs_db)["status"], media_jobs.STATUS_CANCELED)
        self.assertIsNone(self.runtime.load_payload(job_id))

    def _resumed_waiting_job(self, job_id: str) -> None:
        # After a restart a job resumes as running but still waits for a turn.
        media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent="image.generate", request_id=f"req_{job_id}",
                                              app_id=self.app["id"], metadata={"mode": me_make.JOB_MODE}),
                          path=media_jobs.DB_PATH)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, path=media_jobs.DB_PATH)
        self.runtime.keep_payload(job_id, {**self._payload("image.generate"), "job_id": job_id})

    def test_repeated_cancels_of_a_waiting_resumed_job_settle_it_once(self) -> None:
        # Review repro (P2-7): two cancels before the loop handled them read as
        # shutdown, so the job vanished but stayed "running" and held capacity.
        jobs_db = media_jobs.DB_PATH
        job_id = "mj_resumed_twice"
        self._resumed_waiting_job(job_id)

        async def never(request, auth_context, receipt=None):
            raise AssertionError("a canceled job never reaches the backend")

        def cancel() -> None:  # The cancel route's path for a job that is not queued.
            self.assertTrue(self.runtime.request_cancel(job_id))
            try:
                media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, expected_status=media_jobs.STATUS_RUNNING,
                                         metadata_patch={"cancel_requested_at": 1}, path=jobs_db)
            except ValueError:  # It already settled.
                pass
            self.runtime.drop_payload(job_id)


        async def main() -> int:
            busy = asyncio.Semaphore(1)
            with patch.object(proxy, "_image_media_job_semaphore", busy), \
                    patch.object(proxy, "proxy_graph_image_request", never):
                await busy.acquire()
                self.runtime.schedule(job_id, me_make.run_job(job_id, runtime=self.runtime, jobs_path=jobs_db))
                await asyncio.sleep(0)
                cancel()  # Two cancel POSTs land before the loop runs the first stop.
                cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                return len(proxy._image_media_job_tasks)

        self.assertEqual(asyncio.run(main()), 0)
        job = media_jobs.get(job_id, path=jobs_db)
        self.assertEqual(job["status"], media_jobs.STATUS_CANCELED, "it settles, so it frees its capacity")
        self.assertEqual(self.runtime._gates, {})
        self.assertNotIn(job_id, proxy._image_media_job_scheduled)
        settled = [e for e in media_jobs.events(job_id, path=jobs_db) if e["event_type"] == "canceled"]
        self.assertEqual(len(settled), 1, "it settles exactly once")

    def test_shutdown_of_a_waiting_resumed_job_leaves_it_to_resume(self) -> None:
        jobs_db = media_jobs.DB_PATH
        job_id = "mj_resumed_shutdown"
        self._resumed_waiting_job(job_id)
        ran: list[str] = []

        async def run() -> None:
            ran.append(job_id)

        async def main() -> bool:
            busy = asyncio.Semaphore(1)
            with patch.object(proxy, "_image_media_job_semaphore", busy):
                await busy.acquire()
                self.runtime.schedule(job_id, run)
                await asyncio.sleep(0)
                task = next(iter(proxy._image_media_job_tasks))
                task.cancel()  # What the lifespan does at shutdown.
                await asyncio.gather(task, return_exceptions=True)
                busy.release()
                return task.cancelled() and busy._value == 1

        self.assertTrue(asyncio.run(main()), "shutdown still cancels the task and frees no turn it never took")
        self.assertEqual(ran, [])
        self.assertEqual(media_jobs.get(job_id, path=jobs_db)["status"], media_jobs.STATUS_RUNNING)
        self.assertIsNotNone(self.runtime.load_payload(job_id), "the payload stays so the job can resume")

    def test_outstanding_limit_holds_under_concurrent_submits(self) -> None:
        import threading

        jobs_db = self.root / "jobs.db"
        limit = media_jobs.OutstandingLimit("person", 1, me_make.JOB_MODE, app_id=self.app["id"])
        outcomes: list[str] = []
        barrier = threading.Barrier(8)

        def submit(index: int) -> None:
            job = media_jobs.MediaJob(intent="image.generate", request_id=f"req_race_{index}",
                                      app_id=self.app["id"], metadata={"mode": me_make.JOB_MODE})
            barrier.wait()
            try:
                media_jobs.create(job, outstanding_limits=[limit], path=jobs_db)
                outcomes.append("created")
            except media_jobs.OutstandingLimitReached:
                outcomes.append("refused")

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(outcomes), ["created"] + ["refused"] * 7)

    def _published(self) -> list[str]:
        return sorted(path.name for path in (self.root / "cache").iterdir() if not path.name.startswith("5e5e"))

    def _publishing_route(self, entered: asyncio.Event, release: asyncio.Event, *, stoppable: bool):
        user_id = self.user["id"]

        async def route(request, auth_context, receipt=None):
            # What the image route does: a remembered prompt, then the picture.
            context = media_delivery.begin(plan_bound=None)
            context.person_id = user_id
            context.prompt_decision = prompt_memory.Decision(user_id, True, "test", None, int(time.time()))
            context.prompt_text = "private make prompt"
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                if stoppable:
                    raise
                # A backend that cannot be stopped finishes its work anyway.
            asset_id = proxy.store_image_bytes(MADE_PNG)
            return Response(content=json.dumps({"data": [{"asset_id": asset_id, "url": "http://x/y"}]}),
                            status_code=200, media_type="application/json")

        return route

    def _run_job(self, *, cancel: bool, stoppable: bool = True) -> tuple[bool | None, dict]:
        job_id = "mj_cancel_me"
        media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent="image.generate", request_id="req_cancel_me",
                                              app_id=self.app["id"], metadata={"mode": me_make.JOB_MODE}),
                          path=media_jobs.DB_PATH)
        self.runtime.keep_payload(job_id, {**self._payload("image.generate"), "job_id": job_id})

        async def main() -> bool | None:
            entered, release = asyncio.Event(), asyncio.Event()
            route = self._publishing_route(entered, release, stoppable=stoppable)
            with patch.object(proxy, "proxy_graph_image_request", route), \
                    patch.object(proxy, "_image_media_job_semaphore", asyncio.Semaphore(1)):
                self.runtime.schedule(job_id, me_make.run_job(job_id, runtime=self.runtime,
                                                              jobs_path=media_jobs.DB_PATH))
                await entered.wait()
                accepted = await asyncio.to_thread(self.runtime.request_cancel, job_id) if cancel else None
                release.set()
                await asyncio.gather(*list(proxy._image_media_job_tasks))
            return accepted

        accepted = asyncio.run(main())
        return accepted, media_jobs.get(job_id, path=media_jobs.DB_PATH)

    def test_uncanceled_job_publishes_its_picture_and_keeps_the_prompt(self) -> None:
        # Control for the cancel tests below: this route does publish and remember.
        _, job = self._run_job(cancel=False)
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job)
        self.assertEqual(len(self._published()), 2, "the picture and its metadata sidecar")
        self.assertEqual(len(prompt_memory.get_person_prompts(self.user["id"])), 1)

    def test_cancel_stops_a_running_job_before_it_publishes(self) -> None:
        accepted, job = self._run_job(cancel=True, stoppable=True)
        self.assertTrue(accepted)
        self.assertEqual(job["status"], media_jobs.STATUS_CANCELED, job)
        self.assertEqual(job["assets"], [])
        self.assertEqual(self._published(), [])
        self.assertEqual(prompt_memory.get_person_prompts(self.user["id"]), {})
        self.assertIsNone(self.runtime.load_payload("mj_cancel_me"))

    def test_cancel_never_publishes_when_the_backend_cannot_be_stopped(self) -> None:
        accepted, job = self._run_job(cancel=True, stoppable=False)
        self.assertTrue(accepted)
        self.assertEqual(job["status"], media_jobs.STATUS_CANCELED, job)
        self.assertEqual(job["assets"], [])
        self.assertEqual(self._published(), [], "the late picture is never stored")
        self.assertEqual(prompt_memory.get_person_prompts(self.user["id"]), {}, "nor its prompt")

    def test_cancel_after_publication_is_refused(self) -> None:
        gate = proxy._MakePublishGate()
        token = proxy._MAKE_PUBLISH_GATE.set(gate)
        try:
            proxy.store_image_bytes(MADE_PNG)
        finally:
            proxy._MAKE_PUBLISH_GATE.reset(token)
        self.assertFalse(gate.cancel(), "a published picture cannot be un-published by cancel")

    def test_forget_payload_never_reaches_disk(self) -> None:
        payload = self._payload(prompt="forget spool sentinel",
                                extensions={prompt_memory.NAMESPACE: {"mode": "forget"}})
        self.runtime.keep_payload("mj_forget", payload)
        self.assertEqual(self.runtime.load_payload("mj_forget")["body"]["prompt"], "forget spool sentinel")
        on_disk = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertTrue(all(b"forget spool sentinel" not in path.read_bytes() for path in on_disk))
        self.runtime.drop_payload("mj_forget")
        self.assertIsNone(self.runtime.load_payload("mj_forget"))

    def test_remembered_payload_survives_a_restart(self) -> None:
        self.runtime.keep_payload("mj_keep", self._payload())
        proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.clear()
        self.assertEqual(self.runtime.load_payload("mj_keep")["auth"]["app_id"], self.app["id"])


class SessionAppStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.admin = Path(tmp.name) / "admin.db"
        admin_store.init_db(self.admin)
        self.user = admin_store.upsert_oidc_user("issuer", "p", email="p@example.test", path=self.admin)

    def test_session_app_is_created_once_and_is_never_the_key_home(self) -> None:
        first = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        again = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self.assertEqual(first["id"], again["id"])
        home = admin_store.ensure_personal_app_for_user(self.user["id"], path=self.admin)
        self.assertNotEqual(home["id"], first["id"])

    def test_self_service_cannot_see_or_key_the_session_app(self) -> None:
        from gateway import user_runtime
        from gateway.admin_oidc import AdminSession

        session = AdminSession(subject=self.user["id"], email="p@example.test", role="none",
                               issued_at=0, expires_at=2**31)
        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        snapshot = user_runtime.build_user_portal_snapshot(session, path=self.admin)
        self.assertNotIn(app["id"], [item["id"] for item in snapshot["apps"]])
        with self.assertRaises(user_runtime.UserPortalError):
            user_runtime.mint_user_app_key(session, app["id"], path=self.admin)
        self.assertEqual(admin_store.list_credentials(path=self.admin), [])

    def _legacy_session_app_key(self, app_id: str, key_hash: str) -> str:
        """Write a key the way a gateway without the session-App guard could."""
        import sqlite3

        from tests.support.app_keys import unguarded

        unguarded(self.admin)
        conn = sqlite3.connect(self.admin)
        try:
            conn.execute("DROP TRIGGER IF EXISTS credentials_never_on_session_apps")
            conn.execute(
                "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at) "
                "VALUES ('cred_legacy', 'app', ?, ?, ?, 'opk_legacy', 1)", (app_id, app_id, key_hash),
            )
            conn.commit()
        finally:
            conn.close()
        return "cred_legacy"

    def test_no_one_can_create_a_key_for_a_session_app(self) -> None:
        from gateway.admin_api import AdminContext, NotYourApp
        from gateway.admin_runtime import AdminSQLiteStore

        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"],
                                               hash="h" * 64, prefix="opk_x", path=self.admin, created_by=accountable_person(self.admin, app["id"]))
        # An administrator is not the person the session App answers for: not_your_app. That person is refused
        # because a session App never holds a key (ValueError).
        with self.subTest(actor="admin"), self.assertRaises(NotYourApp):
            AdminSQLiteStore(self.admin).create_credential(
                app_id=app["id"], data={},
                actor=AdminContext(subject="admin", actor_id="admin", roles=frozenset({"owner"})),
            )
        with self.subTest(actor="the person"), self.assertRaises(ValueError):
            AdminSQLiteStore(self.admin).create_credential(
                app_id=app["id"], data={},
                actor=AdminContext(subject=self.user["id"], actor_id=self.user["id"], roles=frozenset({"owner"})),
            )
        self.assertEqual(admin_store.list_credentials(path=self.admin), [])
        ordinary = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], access_plan="person", path=self.admin)
        admin_store.create_credential_hash(subject_kind="app", subject_id=ordinary["id"],
                                           hash="o" * 64, prefix="opk_o", path=self.admin, created_by=accountable_person(self.admin, ordinary["id"]))

    def test_a_session_app_key_never_authenticates(self) -> None:
        from gateway.admin_runtime import runtime_identity_for_app_credential_hash

        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self._legacy_session_app_key(app["id"], "s" * 64)
        self.assertIsNone(runtime_identity_for_app_credential_hash("s" * 64, path=self.admin))

    def test_startup_revokes_existing_session_app_keys(self) -> None:
        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        ordinary = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], access_plan="person", path=self.admin)
        kept = admin_store.create_credential_hash(subject_kind="app", subject_id=ordinary["id"],
                                                  hash="o" * 64, prefix="opk_o", path=self.admin, created_by=accountable_person(self.admin, ordinary["id"]))
        legacy = self._legacy_session_app_key(app["id"], "s" * 64)
        admin_store.init_db(self.admin)
        credentials = {item["id"]: item for item in admin_store.list_credentials(path=self.admin)}
        self.assertIsNotNone(credentials[legacy]["revoked_at"])
        self.assertIsNone(credentials[kept["id"]]["revoked_at"])
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"],
                                               hash="n" * 64, prefix="opk_n", path=self.admin, created_by=accountable_person(self.admin, app["id"]))


if __name__ == "__main__":
    unittest.main()
