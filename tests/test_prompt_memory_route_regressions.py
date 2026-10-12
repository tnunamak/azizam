"""Regression coverage from the independent prompt-memory privacy probes."""

from __future__ import annotations

import base64
import asyncio
import sqlite3
import threading
import time
import io
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import proxy
import pytest
from fastapi.testclient import TestClient
from gateway import admin_store, media_jobs, media_metadata, me_media, prompt_memory, receipt_store

from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401
from tests.test_media_provenance import StubRequest, _patch_route_stores

SENTINEL = "zz-review-sentinel-91ab"


def _comfy_png_with(sentinel: str) -> bytes:
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": sentinel}}}))
    info.add_text("workflow", json.dumps({"nodes": [{"widgets_values": [sentinel]}]}))
    out = io.BytesIO()
    Image.new("RGBA", (8, 6), (1, 2, 3, 255)).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


class MemberImageRouteProbe(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams, monkeypatch):
        self.fakes = fake_upstreams
        self.monkeypatch = monkeypatch

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _patch_route_stores(self, self.root)
        self.admin_db = self.root / "admin.db"
        admin_store.init_db(self.admin_db)
        for target, name, value in (
            (proxy, "COMFY_ENABLED", True),
            (proxy.ADMIN_STORE, "path", self.admin_db),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.member = admin_store.upsert_oidc_user("iss", "robin", email="robin@example.test", path=self.admin_db)
        self.app = admin_store.create_app("Try", kind="personal", owner_user_id=self.member["id"], path=self.admin_db)
        self.auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="Try", key_hash="sha256:robin", app_id=self.app["id"], person_id=self.member["id"]))

    async def _generate(self, extensions):
        body = {"prompt": SENTINEL, "model": "krea2-turbo", "size": "8x6", "response_format": "b64_json"}
        if extensions is not None:
            body["extensions"] = extensions

        class Req(StubRequest):
            async def json(self_inner):
                return body

        comfy = self.fakes.asgi(load_scenario("comfyui/image-complete-png", png_hex=_comfy_png_with(SENTINEL).hex()))
        self.monkeypatch.setattr(proxy, "COMFY_HOST", comfy.base_url)
        receipt = proxy.start_image_route_receipt("/v1/images/generations", self.auth)
        with patch.object(
            proxy, "get_vram_estimate", lambda *_a, **_k: 1024), patch.object(
            proxy, "comfy_free_vram_bytes", AsyncMock(return_value=10**10)), patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None), patch.object(
            proxy, "configured_downstream_headers", lambda *_a: {}), patch.object(
            # The background decision probes service health over HTTP; no ComfyUI fact is under test here.
            proxy, "resolve_image_background_decision",
            AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))):
            response = await proxy.proxy_comfy_request(Req(), self.auth, receipt=receipt)
        return response

    async def test_member_forget_end_to_end(self):
        response = await self._generate({"com.azizam.prompts": {"mode": "forget"}})
        self.assertEqual(response.status_code, 200, response.body)
        delivered = base64.b64decode(json.loads(response.body)["data"][0]["b64_json"])
        self.assertNotIn(SENTINEL.encode(), delivered)
        self.assertNotIn(SENTINEL.encode(), _all_bytes(self.root))

    async def test_member_default_remembers_and_cache_clean(self):
        response = await self._generate(None)
        self.assertEqual(response.status_code, 200, response.body)
        asset_id = json.loads(response.body)["data"][0]["asset_id"]
        row = prompt_memory.get(asset_id, self.member["id"])
        self.assertEqual(row["prompt_text"], SENTINEL)
        self.assertNotIn(SENTINEL.encode(), _all_bytes(self.root / "asset-cache"))

    async def test_library_delete_removes_prompt_row(self):
        response = await self._generate(None)
        asset_id = json.loads(response.body)["data"][0]["asset_id"]
        deleter = me_media.conservative_asset_deleter(
            metadata_reader=proxy._read_asset_metadata,
            asset_path_resolver=proxy.asset_cache_path,
            metadata_path_resolver=proxy._asset_metadata_path,
        )
        self.assertTrue(deleter(asset_id))
        self.assertIsNone(proxy.asset_cache_path(asset_id) and proxy.asset_cache_path(asset_id).exists() or None)
        # The retained text has the same lifecycle as the deleted item.
        self.assertIsNone(prompt_memory.get(asset_id, self.member["id"]))


    async def test_prompt_write_failure_returns_finished_media_without_storage(self):
        with patch.object(prompt_memory, "put", side_effect=sqlite3.OperationalError("locked")):
            response = await self._generate(None)
        self.assertEqual(response.status_code, 200, response.body)
        asset_id = json.loads(response.body)["data"][0]["asset_id"]
        self.assertIsNone(prompt_memory.get(asset_id, self.member["id"]))
        self.assertNotIn(SENTINEL.encode(), _all_bytes(self.root))

    async def test_prompt_write_does_not_block_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        original = prompt_memory.put
        def slow_put(*args, **kwargs):
            entered.set()
            # Storage stays blocked until the test has seen the loop run.
            assert release.wait(timeout=30), "the loop never ran while prompt storage waited"
            return original(*args, **kwargs)
        with patch.object(prompt_memory, "put", slow_put):
            task = asyncio.create_task(self._generate(None))
            while not entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
            self.assertTrue(entered.is_set())
            # The loop still runs these turns while the write is blocked.
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "the loop must progress while prompt storage waits")
            release.set()
            response = await task
        self.assertEqual(response.status_code, 200)
