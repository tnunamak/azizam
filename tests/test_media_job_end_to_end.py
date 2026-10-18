"""Async media jobs run end to end: MCP request in, worker, assets out.

Only the backend's HTTP is faked (the FK ComfyUI fake). The submission is the
request the MCP ``run`` tool builds for the workflow, the caller is real (an
App key or an App trusted by client address, from a real admin database), and
the worker is the real one, so a failure anywhere on the path shows here.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from gateway import accounts, admin_store, media_jobs
from gateway.workflow_call import build_request
from tests.support.access import store_plan
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401

HANG_BOUND_SECONDS = 20


def _encoded_audio(suffix: str) -> bytes:
    """A short real tone in ``suffix``'s container, as the backend writes it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"tone{suffix}"
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                               "-i", "sine=d=0.3:r=22050", str(path)], capture_output=True)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"ffmpeg cannot write {suffix}: {proc.stderr[-200:]!r}")
        return path.read_bytes()
PROVENANCE = {"source": "hand-written",
              "reference": "ComfyUI server routes POST /prompt, GET /history/{prompt_id}, GET /view (ComfyUI server.py)"}


class _Request:
    """The core route's view of an authenticated request."""

    method = "POST"
    query_params: dict[str, str] = {}

    def __init__(self, auth_context: proxy.AuthContext) -> None:
        self.scope = {"auth_context": auth_context}
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")
        self.client = SimpleNamespace(host="127.0.0.1", port=40000)

    async def json(self) -> dict[str, Any]:
        return {}


def _audio_scenario(filename: str, media_type: str) -> Any:
    return scenario_from_data({
        "protocol": "comfyui", "name": "audio-job-succeeds", "provenance": PROVENANCE,
        "exchanges": [
            {"id": "prompt", "match": {"method": "POST", "path": "/prompt"},
             "response": {"json": {"prompt_id": "p1"}}},
            {"id": "history", "match": {"method": "GET", "path": "/history/p1"},
             "response": {"json": {"p1": {"status": {"status_str": "success"},
                                          "outputs": {"9": {"audio": [{"filename": filename,
                                                                       "type": "output"}]}}}}}},
            {"id": "view", "match": {"method": "GET", "path": "/view"},
             "response": {"headers": {"content-type": media_type},
                          "body": [{"hex": _encoded_audio(Path(filename).suffix).hex()}]}},
        ],
    })


def _encoded_video() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.mp4"
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                               "-i", "testsrc=d=0.4:s=64x64:r=10", "-pix_fmt", "yuv420p", str(path)],
                              capture_output=True)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"ffmpeg cannot write mp4: {proc.stderr[-200:]!r}")
        return path.read_bytes()


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(out, format="PNG")
    return out.getvalue()


def _comfy_scenario(name: str, outputs_key: str, filename: str, media_type: str, content: bytes) -> Any:
    """One job: submitted, done at the first history check, its output downloaded."""
    return scenario_from_data({
        "protocol": "comfyui", "name": name, "provenance": PROVENANCE,
        "exchanges": [
            {"id": "prompt", "match": {"method": "POST", "path": "/prompt"},
             "response": {"json": {"prompt_id": "p1"}}},
            {"id": "history", "match": {"method": "GET", "path": "/history/p1"}, "repeat": True,
             "response": {"json": {"p1": {"status": {"status_str": "success", "completed": True},
                                          "outputs": {"9": {outputs_key: [{"filename": filename, "subfolder": "",
                                                                           "type": "output"}]}}}}}},
            {"id": "view", "match": {"method": "GET", "path": "/view"}, "repeat": True,
             "response": {"headers": {"content-type": media_type}, "body": [{"hex": content.hex()}]}},
            # The image route reads free VRAM before it sizes a batch.
            {"id": "system_stats", "match": {"method": "GET", "path": "/system_stats"}, "repeat": True,
             "required": False,
             "response": {"json": {"devices": [{"name": "fk", "type": "cuda", "vram_total": 24 << 30,
                                                "vram_free": 20 << 30}]}}},
        ],
    })


class AsyncMediaJobEndToEndTests(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams):
        self.fakes = fake_upstreams

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        self.jobs = root / "media_jobs.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        accounts.initialize(self.admin)
        owner = admin_store.upsert_oidc_user("https://idp.test", "ann", email="ann@example.test", path=self.admin)
        plan = store_plan("Everything", path=self.admin)
        accounts.change_person(owner["id"], "test", path=self.admin, plan_id=plan["id"])
        self.local = admin_store.create_app("This computer", kind="service", owner_user_id=owner["id"],
                                            access_plan=plan["id"], path=self.admin)
        admin_store.add_trusted_address(self.local["id"], "127.0.0.1", max_entries=10, path=self.admin)
        self.keyed = admin_store.create_app("daisy", kind="service", owner_user_id=owner["id"],
                                            access_plan=plan["id"], path=self.admin)
        admin_store.create_credential_hash(subject_kind="app", subject_id=self.keyed["id"], app_id=self.keyed["id"],
                                           hash=proxy.hash_api_key("oak_e2e_daisy"), prefix="oak_e2e",
                                           created_by=owner["id"], path=self.admin)
        self.scheduled: list[str] = []
        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", self.jobs),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            (proxy, "IMAGE_CACHE_DIR", root / "cache"),
            (proxy, "COMFY_ENABLED", True),
            (proxy, "_image_url_signing_secret", b"test-secret"),
            (proxy, "_schedule_audio_media_job", lambda job_id: self.scheduled.append(job_id) or True),
            (proxy, "_schedule_image_media_job", lambda job_id: self.scheduled.append(job_id) or True),
            (proxy, "_schedule_video_media_job", lambda job_id: self.scheduled.append(job_id) or True),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        overrides = patch.object(proxy.ADMIN_STORE, "operator_overrides",
                                 return_value={"service_availability": {}, "media_job_admission": {}})
        overrides.start()
        self.addCleanup(overrides.stop)

    def caller(self, kind: str) -> proxy.AuthContext:
        if kind == "key":
            identity = proxy.runtime_identity_for_app_credential_hash(proxy.hash_api_key("oak_e2e_daisy"),
                                                                      path=self.admin)
        else:
            identity = proxy.runtime_identity_for_trusted_address(ipaddress.ip_address("127.0.0.1"),
                                                                  path=self.admin)
        assert identity is not None and identity.denial is None
        return proxy.AuthContext(identity=identity)

    async def submit_as_mcp(self, target: str, inputs: dict[str, Any], auth: proxy.AuthContext) -> str:
        """Submit exactly the request MCP ``run`` builds for an async run of ``target``."""
        entry = next(e for e in proxy._mcp_catalog() if target in (e["id"], e["short_id"]))
        route = entry["execution"]["async"]
        self.assertEqual(route["kind"], "media_job")
        submit = {**route["submit"], "fields": route["fields"], "content_type": route["submit"].get("content_type", "")}
        built = await build_request(lambda *_: None, None, auth, entry, submit, inputs,
                                    wrap_intent=route["submit"]["body"]["intent"])
        response = await proxy._dispatch_gateway_media_job_submission(
            request=_Request(auth), payload=built.json, audience="client")  # type: ignore[arg-type]
        assert response is not None
        self.assertEqual(response.status_code, 202, response.body)
        return json.loads(response.body)["job_id"]

    async def run_audio(self, caller: str) -> dict[str, Any]:
        # The workflow writes FLAC (its manifest output), as the live backend does.
        fake = self.fakes.asgi(_audio_scenario("sfx_00001_.flac", "audio/flac"))
        auth = self.caller(caller)
        with patch.object(proxy, "COMFY_HOST", fake.base_url), \
                patch.object(proxy.asyncio, "sleep", AsyncMock()):
            job_id = await self.submit_as_mcp("audio.generate.sa3-small-sfx", {"prompt": "a door creaks"}, auth)
            self.assertEqual(self.scheduled, [job_id])
            await asyncio.wait_for(proxy._run_audio_media_job(job_id), HANG_BOUND_SECONDS)
        return media_jobs.get(job_id, audience="internal", path=self.jobs)

    async def run_image(self, caller: str) -> dict[str, Any]:
        fake = self.fakes.asgi(_comfy_scenario("image-job-succeeds", "images", "out.png", "image/png", _png()))
        auth = self.caller(caller)
        # "auto" background asks a chat model whether to cut out the subject; no chat backend here.
        no_cutout = AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))
        with patch.object(proxy, "COMFY_HOST", fake.base_url), \
                patch.object(proxy, "resolve_image_background_decision", no_cutout), \
                patch.object(proxy.asyncio, "sleep", AsyncMock()):
            job_id = await self.submit_as_mcp("image.generate.flux2-klein-4b", {"prompt": "a red fox"}, auth)
            self.assertEqual(self.scheduled, [job_id])
            await asyncio.wait_for(proxy._run_image_media_job(job_id), HANG_BOUND_SECONDS)
        return media_jobs.get(job_id, audience="internal", path=self.jobs)

    async def run_video(self, caller: str) -> dict[str, Any]:
        fake = self.fakes.asgi(_comfy_scenario("video-job-succeeds", "videos", "out.mp4", "video/mp4",
                                               _encoded_video()))
        auth = self.caller(caller)
        entry = next(e for e in proxy._mcp_catalog() if e["id"] == "video.generate.ltx-2.5-t2v")
        route = entry["execution"]["async"]
        submit = {**route["submit"], "fields": route["fields"], "content_type": route["submit"].get("content_type", "")}
        # The body MCP builds for the video route, sent as a durable media job.
        built = await build_request(lambda *_: None, None, auth, entry, submit, {"prompt": "waves at dusk"},
                                    wrap_intent=None)
        with patch.object(proxy, "COMFY_HOST", fake.base_url), \
                patch.object(proxy.asyncio, "sleep", AsyncMock()), \
                patch.object(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_Request(auth), payload={"intent": "video.generate", "request": built.json},  # type: ignore[arg-type]
                audience="client")
            assert response is not None
            self.assertEqual(response.status_code, 202, response.body)
            job_id = json.loads(response.body)["job_id"]
            self.assertEqual(self.scheduled, [job_id])
            await asyncio.wait_for(proxy._run_video_media_job(job_id), HANG_BOUND_SECONDS)
        return media_jobs.get(job_id, audience="internal", path=self.jobs)

    async def test_image_job_for_a_trusted_address_caller(self) -> None:
        job = await self.run_image("trusted address")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.local["id"])
        self.assertTrue(job["assets"][0]["asset_id"].startswith("img_"))

    async def test_image_job_for_an_app_key_caller(self) -> None:
        job = await self.run_image("key")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.keyed["id"])

    async def test_video_job_for_a_trusted_address_caller(self) -> None:
        job = await self.run_video("trusted address")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.local["id"])
        self.assertTrue(job["assets"][0]["asset_id"].startswith("vid_"))

    async def test_video_job_for_an_app_key_caller(self) -> None:
        job = await self.run_video("key")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.keyed["id"])

    async def test_audio_job_for_a_trusted_address_caller(self) -> None:
        job = await self.run_audio("trusted address")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.local["id"])
        self.assertTrue(job["assets"] and job["assets"][0]["asset_id"].startswith("aud_"))
        self.assertEqual(job["assets"][0]["media_type"], "audio/flac")

    async def test_audio_job_for_an_app_key_caller(self) -> None:
        job = await self.run_audio("key")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(job["app_id"], self.keyed["id"])


if __name__ == "__main__":
    unittest.main()
