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

from fastapi import Response

import proxy
from gateway import media_jobs
from gateway.mcp_server import McpDeps, Tools
from gateway.policy import DENIED, PolicyDecision

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

if __name__ == "__main__":
    unittest.main()
