"""A failed media job says who refused it, in plain words and typed fields.

The gateway's own refusal (an access plan, its inputs check, its admission)
must not read as an upstream failure, and an upstream 403 must not read as an
access-plan refusal. No error text may carry the prompt.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Response

import proxy
from gateway import media_jobs
from gateway.mcp_server import McpDeps, Tools
from gateway.policy import DENIED, PolicyDecision
from tests.support.access import access_context, authenticated
from tests.support.audio_workflow import audio_manifest

PROMPT = "a secret red fox at dawn"


def _json(status: int, error: dict[str, Any]) -> Response:
    return Response(content=json.dumps({"error": error}), status_code=status, media_type="application/json")


class FailureClassificationTests(unittest.TestCase):
    def test_gateway_access_refusal_is_authorization(self) -> None:
        refusal = proxy._policy_denied_response(PolicyDecision(
            decision=DENIED, sources=("access_plan:plan_x",), reasons=("Images aren't in your access plan.",),
            code="not_in_access_plan"))
        message, info = proxy._media_job_failure(refusal)
        self.assertEqual(message, "This workflow isn't in your access plan.")
        self.assertEqual(info, {"error_code": "not_in_access_plan", "error_category": "authorization",
                                "retryable": False})

    def test_upstream_403_is_not_an_access_plan_refusal(self) -> None:
        message, info = proxy._media_job_failure(_json(403, {"message": f"forbidden: {PROMPT}", "code": "forbidden",
                                                             "type": "access_error"}))
        self.assertEqual(message, "Media generation failed (upstream HTTP 403).")
        self.assertEqual(info["error_category"], "upstream")
        self.assertEqual(info["upstream_http_status"], 403)
        self.assertFalse(info["retryable"])

    def test_categories(self) -> None:
        cases = [
            (_json(502, {"message": f"ComfyUI unavailable for {PROMPT}"}), "upstream", True, 502),
            (_json(503, {"message": "Insufficient free VRAM", "code": "vram_exhausted"}), "upstream", True, 503),
            (_json(504, {"message": "ComfyUI timeout"}), "timeout", True, None),
            (_json(400, {"message": f"bad {PROMPT}", "code": "invalid_value"}), "invalid_input", False, None),
            (_json(400, {"message": "blocked", "code": "content_policy_violation"}), "safety", False, None),
            (_json(429, {"message": "slow down", "code": "rate_limited"}), "capacity", True, None),
        ]
        for response, category, retryable, upstream in cases:
            with self.subTest(category=category, status=response.status_code):
                message, info = proxy._media_job_failure(response)
                self.assertNotIn(PROMPT, message)
                self.assertNotIn(PROMPT, json.dumps(info))
                self.assertEqual(info["error_category"], category)
                self.assertEqual(info["retryable"], retryable)
                self.assertEqual(info.get("upstream_http_status"), upstream)
                self.assertIn(info["error_category"], media_jobs.ERROR_CATEGORIES)

    def test_error_code_from_a_backend_is_kept_only_as_a_plain_token(self) -> None:
        _message, info = proxy._media_job_failure(_json(502, {"message": "x", "code": f"{PROMPT}!"}))
        self.assertEqual(info["error_code"], "upstream_error")

    def test_only_known_error_codes_are_copied(self) -> None:
        # A plain token can still carry prompt words; only codes the gateway knows pass.
        for status in (400, 502, 504):
            with self.subTest(status=status):
                _message, info = proxy._media_job_failure(_json(status, {"message": "x", "code": "red_fox_at_dawn"}))
                self.assertNotIn("fox", info["error_code"])
        _message, info = proxy._media_job_failure(_json(503, {"message": "x", "code": "vram_exhausted"}))
        self.assertEqual(info["error_code"], "vram_exhausted")

    def test_access_errors_are_the_gateways_refusal(self) -> None:
        for code in proxy.ACCESS_ERROR_MESSAGES:
            with self.subTest(code=code):
                message, info = proxy._media_job_failure(proxy.access_error(code))
                self.assertEqual(info["error_category"], "authorization")
                self.assertEqual(info["error_code"], code)
                self.assertNotIn("upstream_http_status", info)
                self.assertNotIn("upstream", message)


class JobErrorFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "jobs.db"
        media_jobs.init_db(self.db)

    def failed_job(self, **info: Any) -> str:
        job = media_jobs.MediaJob(intent="image.generate", request_id="req_x")
        media_jobs.create(job, path=self.db)
        media_jobs.update_status(job.job_id, media_jobs.STATUS_FAILED, error="This workflow isn't in your access plan.",
                                 error_info=info, path=self.db)
        return job.job_id

    def test_the_rest_job_view_shows_the_fields(self) -> None:
        job_id = self.failed_job(error_code="not_in_access_plan", error_category="authorization", retryable=False)
        client = media_jobs.get(job_id, audience="client", path=self.db)
        self.assertEqual(client["error"], "This workflow isn't in your access plan.")
        self.assertEqual(client["error_code"], "not_in_access_plan")
        self.assertEqual(client["error_category"], "authorization")
        self.assertIs(client["retryable"], False)
        self.assertIsNone(client["upstream_http_status"])
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="gw.test"))
        payload = proxy._image_media_job_client_payload(client, request)  # type: ignore[arg-type]
        self.assertEqual(payload["error_category"], "authorization")

    def test_upstream_status_round_trips(self) -> None:
        job_id = self.failed_job(error_code="upstream_error", error_category="upstream", retryable=True,
                                 upstream_http_status=502)
        self.assertEqual(media_jobs.get(job_id, audience="client", path=self.db)["upstream_http_status"], 502)

    def test_unknown_category_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.failed_job(error_code="x", error_category="mystery", retryable=False)


class McpJobResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_job_result_carries_the_error_fields(self) -> None:
        raw = {"status": "failed", "progress": 5, "error": "This workflow isn't in your access plan.",
               "error_code": "not_in_access_plan", "error_category": "authorization", "retryable": False,
               "upstream_http_status": None}

        async def call(_request, _method, _path, **_kwargs):
            return SimpleNamespace(status_code=200, json=lambda: raw, headers={})

        deps = McpDeps(authenticate=lambda *_: None, call=call, catalog=lambda: [], aliases=lambda: {},
                       store_asset=lambda *_: {}, read_asset=lambda *_: None, public_base_url=lambda _r: None)
        result = await Tools(deps, None, None).job({"job_id": "mj_x"})  # type: ignore[arg-type]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "This workflow isn't in your access plan.")
        self.assertEqual(result["error_code"], "not_in_access_plan")
        self.assertEqual(result["error_category"], "authorization")
        self.assertIs(result["retryable"], False)
        self.assertNotIn("upstream_http_status", result)


class _JsonRequest:
    method = "POST"
    scope: dict[str, Any] = {"auth_context": access_context()}
    query_params: dict[str, str] = {}

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = dict(body)
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


@pytest.mark.usefixtures("isolated_receipts")
class AudioJobRefusalReasonTests(unittest.IsolatedAsyncioTestCase):
    """What an async audio job keeps of a 400, by who wrote the refusal."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db = root / "media_jobs.db"
        media_jobs.init_db(self.db)
        for patcher in (
            patch.object(media_jobs, "DB_PATH", self.db),
            patch.object(proxy, "IMAGE_CACHE_DIR", root / "images"),
            patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            authenticated(),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def submit(self, body: dict[str, Any]) -> str:
        with patch.object(proxy, "_schedule_audio_media_job", lambda _job_id: True):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_JsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "sfx.generate", "request": body}, audience="client")
        assert response is not None and response.status_code == 202, getattr(response, "body", None)
        return json.loads(response.body.decode("utf-8"))["job_id"]

    def failed(self, job_id: str) -> dict[str, Any]:
        job = media_jobs.get(job_id, audience="client", path=self.db)
        assert job is not None
        self.assertEqual(job["status"], media_jobs.STATUS_FAILED)
        return job

    async def test_a_gateway_validation_refusal_keeps_its_own_message(self) -> None:
        """The real route refuses duration 2 against a 5 second floor; the job says so."""
        job_id = await self.submit({"model": "tangoflux", "prompt": PROMPT, "type": "sfx", "duration": 2})
        sync = None
        with patch.multiple(
            proxy,
            COMFY_ENABLED=True,
            _compile_from_pin=lambda _capability: False,
            _dispatch_admission_response=lambda *a, **kw: None,
            _service_availability_response=lambda *a, **kw: None,
            _resolve_audio_workflow=lambda model: ("fake-audio", {"min_seconds": 5, "max_seconds": 30}),
            _load_audio_workflow_manifest=lambda name: audio_manifest(name, {"prompt": [("1", "text")],
                                                                          "duration": [("1", "seconds")]}),
        ):
            sync = await proxy.proxy_audio_generate(
                _JsonRequest({"model": "tangoflux", "prompt": PROMPT, "type": "sfx", "duration": 2}),  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT)
            await proxy._run_audio_media_job(job_id)
        self.assertEqual(sync.status_code, 400)
        reason = json.loads(sync.body)["error"]["message"]
        self.assertIn("duration", reason)
        job = self.failed(job_id)
        self.assertEqual(job["error"], reason)
        self.assertEqual(job["error_category"], "invalid_input")
        self.assertEqual(job["error_code"], "invalid_input")
        self.assertIs(job["retryable"], False)
        self.assertNotIn(PROMPT, job["error"])

    async def test_an_upstream_400_that_echoes_the_prompt_keeps_the_fixed_sentence(self) -> None:
        job_id = await self.submit({"model": "tangoflux", "prompt": PROMPT, "type": "sfx"})

        async def upstream_refuses(_request, _auth_context, **_kwargs):
            return _json(400, {"message": f"cannot render: {PROMPT}", "type": "invalid_request_error"})

        with patch.object(proxy, "proxy_audio_generate", upstream_refuses):
            await proxy._run_audio_media_job(job_id)
        job = self.failed(job_id)
        self.assertEqual(job["error"], "The gateway did not accept this job's inputs.")
        self.assertEqual(job["error_category"], "invalid_input")
        self.assertNotIn(PROMPT, json.dumps(job))


if __name__ == "__main__":
    unittest.main()
