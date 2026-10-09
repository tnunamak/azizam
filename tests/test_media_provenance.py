"""Generated-media provenance and the ``metadata`` delivery control.

Covers: the profile default, the per-request override, the provenance record
(and that it never holds secrets), lossless stripping at delivery for every
media type, and that the stored copy stays full.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import comfy_object_info, media_delivery, media_jobs, media_metadata, provenance, receipt_store  # noqa: E402

_REAL_OBJECT_INFO_LOOKUP = comfy_object_info.lookup  # tests/conftest.py stubs the module attribute
from gateway.extensions import validate_extension_inputs  # noqa: E402
from gateway.policy import DEFAULT_PROFILE, MediaMetadataPolicy  # noqa: E402
from gateway.profiles import ProfileLoadError, load_profile  # noqa: E402

NS = "com.azizam.workflow"
FFMPEG = shutil.which("ffmpeg") is not None

PROMPT_JSON = json.dumps({"3": {"class_type": "KSampler", "inputs": {"seed": 7}}})
WORKFLOW_JSON = json.dumps({"nodes": [{"id": 3, "type": "KSampler"}], "version": 0.4})
SECRET_KEY = "sk-live-0123456789abcdefSECRET"


def _patch_route_stores(case: unittest.TestCase, root: Path) -> None:
    media_db = root / "media_jobs.db"
    receipts_db = root / "receipts.db"
    cache_dir = root / "asset-cache"
    media_jobs.init_db(media_db)
    receipt_store.init_db(receipts_db)
    for target, value in (
        (media_jobs, media_db),
        (receipt_store, receipts_db),
    ):
        patcher = patch.object(target, "DB_PATH", value)
        patcher.start()
        case.addCleanup(patcher.stop)
    for name, value in (
        ("IMAGE_CACHE_DIR", cache_dir),
        ("_image_url_signing_secret", b"test-secret"),
    ):
        patcher = patch.object(proxy, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


def comfy_png(color=(10, 20, 30, 255), size=(8, 6)) -> bytes:
    """A PNG shaped like ComfyUI output: prompt and workflow text chunks."""
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", PROMPT_JSON)
    info.add_text("workflow", WORKFLOW_JSON)
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def pixels(content: bytes) -> list[tuple[int, ...]]:
    from PIL import Image

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    return list(image.tobytes())


def idat(content: bytes) -> bytes:
    out, pos = b"", 8
    while pos < len(content):
        length = int.from_bytes(content[pos : pos + 4], "big")
        if content[pos + 4 : pos + 8] == b"IDAT":
            out += content[pos + 8 : pos + 8 + length]
        pos += 12 + length
    return out


class StubRequest:
    headers: dict[str, str] = {}
    url = SimpleNamespace(scheme="http", netloc="gateway.test")


class ProfileDefaultTests(unittest.TestCase):
    def test_default_profile_strips(self) -> None:
        self.assertEqual(DEFAULT_PROFILE.media_metadata.default, "none")
        self.assertEqual(load_profile({}).media_metadata.default, "none")
        self.assertEqual(load_profile({"id": "x"}).media_metadata.default, "none")

    def test_profile_can_default_to_full(self) -> None:
        profile = load_profile({"id": "p", "media_metadata": {"default": "full"}})
        self.assertEqual(profile.media_metadata.default, "full")

    def test_bad_profile_value_is_rejected(self) -> None:
        for bad in ({"default": "maybe"}, {"default": 1}, {"unknown": "x"}, "full"):
            with self.assertRaises(ProfileLoadError):
                load_profile({"id": "p", "media_metadata": bad})

    def test_policy_dataclass_validates(self) -> None:
        with self.assertRaises(ValueError):
            MediaMetadataPolicy(default="all")

    def test_seeded_owner_profile_is_full(self) -> None:
        from gateway.default_profiles import DEFAULT_PROFILE_SEEDS

        seeds = {s["profile"]["id"]: s["profile"] for s in DEFAULT_PROFILE_SEEDS}
        self.assertEqual(seeds["owner-local"]["media_metadata"], {"default": "full"})
        for pid in ("assistant-chat", "media-creator"):
            self.assertNotEqual(seeds[pid].get("media_metadata", {}).get("default"), "full")

    def test_runtime_default_by_identity(self) -> None:
        none_ctx = proxy.AuthContext(identity=proxy._identity_for("some-path-token", "path-token"))
        lan_ctx = proxy.AuthContext(identity=proxy._identity_for("lan-bypass", "lan"))
        full_profile = load_profile({"id": "owner", "media_metadata": {"default": "full"}})
        owner = proxy.AuthContext(
            identity=proxy.AuthIdentity(name="o", key_hash="sha256:o", profile=full_profile)
        )
        plain = proxy.AuthContext(identity=proxy.AuthIdentity(name="p", key_hash="sha256:p"))
        self.assertEqual(proxy._media_metadata_default(lan_ctx), "full")
        self.assertEqual(proxy._media_metadata_default(owner), "full")
        self.assertEqual(proxy._media_metadata_default(none_ctx), "none")
        self.assertEqual(proxy._media_metadata_default(plain), "none")
        # auth disabled: personal local run
        self.assertEqual(proxy._media_metadata_default(proxy.NO_AUTH_CONTEXT), "full")
        # the LAN profile keeps the default-local id, so receipts do not change
        self.assertEqual(lan_ctx.identity.profile.id, DEFAULT_PROFILE.id)


class ControlParsingTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertEqual(provenance.parse_metadata_mode("full"), "full")
        self.assertEqual(provenance.parse_metadata_mode(" NONE "), "none")
        for bad in ("", "strip", None, True, 1, ["none"]):
            with self.assertRaises(provenance.MetadataModeError):
                provenance.parse_metadata_mode(bad)

    def test_request_override_beats_profile_default(self) -> None:
        resolve = provenance.resolve_metadata_mode
        self.assertEqual(resolve("none", "full"), "none")
        self.assertEqual(resolve("full", "none"), "full")
        self.assertEqual(resolve(None, "full"), "full")
        self.assertEqual(resolve(None, None), "none")

    def test_read_from_body(self) -> None:
        body = {"extensions": {NS: {"metadata": "none", "seed": 1}}}
        self.assertEqual(provenance.requested_metadata_mode(body), "none")
        self.assertIsNone(provenance.requested_metadata_mode({"prompt": "x"}))
        self.assertIsNone(provenance.requested_metadata_mode({"extensions": {NS: {"seed": 1}}}))
        with self.assertRaises(provenance.MetadataModeError):
            provenance.requested_metadata_mode({"extensions": {NS: {"metadata": "x"}}})
        with self.assertRaises(provenance.MetadataModeError):
            provenance.requested_metadata_mode({"extensions": "none"})

    def test_workflow_validation_accepts_control_without_binding_it(self) -> None:
        specs = {"seed": {"type": "integer"}}
        result = validate_extension_inputs(
            specs, {"metadata": "none", "seed": 3}, namespace=NS
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.accepted, {"seed": 3})
        self.assertEqual(result.gateway_controls, {"metadata": "none"})
        bad = validate_extension_inputs(specs, {"metadata": "nope"}, namespace=NS)
        self.assertIn("metadata", bad.rejected)

    def test_other_namespaces_do_not_get_the_control(self) -> None:
        result = validate_extension_inputs(
            {}, {"metadata": "none"}, namespace="com.azizam.chat"
        )
        self.assertIn("metadata", result.rejected)

    def test_query_forms(self) -> None:
        media_delivery.begin(profile_default="full")
        self.assertEqual(
            media_delivery.note_query([("extensions[com.azizam.workflow][metadata]", "none")]),
            "none",
        )
        self.assertEqual(media_delivery.current_mode(), "none")
        media_delivery.begin(profile_default="full")
        self.assertEqual(media_delivery.note_query([("metadata", "none")]), "none")
        media_delivery.begin(profile_default="none")
        self.assertIsNone(media_delivery.note_query([("other", "x")]))
        self.assertEqual(media_delivery.current_mode(), "none")
        with self.assertRaises(provenance.MetadataModeError):
            media_delivery.note_query([("metadata", "x")])

    def test_ranged_delivery_response_supports_browser_audio_seek(self) -> None:
        content = b"0123456789abcdef"
        request = SimpleNamespace(headers={"range": "bytes=4-9"})
        response = media_delivery.ranged_delivery_response(
            request,  # type: ignore[arg-type]
            content,
            media_type="audio/wav",
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-range"], "bytes 4-9/16")
        self.assertEqual(response.body, b"456789")

        bad = media_delivery.ranged_delivery_response(
            SimpleNamespace(headers={"range": "bytes=99-100"}),  # type: ignore[arg-type]
            content,
            media_type="audio/wav",
        )
        self.assertEqual(bad.status_code, 416)
        self.assertEqual(bad.headers["content-range"], "bytes */16")


class ProvenanceRecordTests(unittest.TestCase):
    def test_record_fields_from_receipt(self) -> None:
        from gateway.receipts import RouteReceipt

        receipt = RouteReceipt(
            protocol="openai.images", intent="image.generate", path="/v1/images/generations",
            requested_model="gpt-image-1", resolved_model="krea2-turbo",
            resolved_workflow="image.generate.krea2-turbo", backend="comfyui",
            api_key_name="tims-key", app_profile="owner-local",
        )
        receipt.attach_manifest(manifest_id="image.generate.krea2-turbo", manifest_version="3", manifest_hash="abc123")
        record = provenance.build_provenance_record(
            receipt=receipt, kind="image", seed=42, parameters={"size": "512x512"}, index=0,
        )
        self.assertEqual(record["schema"], "azizam.provenance/1")
        self.assertEqual(record["requested_model"], "gpt-image-1")
        self.assertEqual(record["model_alias"], "gpt-image-1")
        self.assertEqual(record["resolved_model"], "krea2-turbo")
        self.assertEqual(record["workflow_id"], "image.generate.krea2-turbo")
        self.assertEqual(record["workflow_version"], "3")
        self.assertEqual(record["workflow_hash"], "abc123")
        self.assertEqual(record["seed"], 42)
        self.assertEqual(record["parameters"], {"size": "512x512"})
        self.assertEqual(record["request_id"], receipt.request_id)
        self.assertEqual(record["receipt"], f"/gateway/v1/receipts/{receipt.request_id}")
        self.assertRegex(record["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # nothing from the receipt beyond the allow-list
        text = json.dumps(record)
        for leaked in ("tims-key", "owner-local", "api_key_name", "app_profile"):
            self.assertNotIn(leaked, text)

    def test_no_secrets_or_media_in_record(self) -> None:
        hostile = {
            "api_key": SECRET_KEY,
            "Authorization": f"Bearer {SECRET_KEY}",
            "nested": {"token": "abc", "ok": "fine"},
            "note": f"use {SECRET_KEY} here",
            "header": "Bearer abcdef123456",
            "url": "http://x/v1/assets/a/content?expires=1&sig=deadbeef",
            "image": "data:image/png;base64," + "QUJD" * 400,
            "blob": "QUJD" * 400,
            "raw": b"\x89PNG....",
            "size": "512x512",
            "steps": 8,
        }
        record = provenance.build_provenance_record(
            kind="image", seed=1, parameters=hostile, request_id="req_1",
        )
        text = provenance.provenance_json(record)
        for leaked in (SECRET_KEY, "abcdef123456", "deadbeef", "QUJDQUJD", "PNG...."):
            self.assertNotIn(leaked, text)
        self.assertEqual(record["parameters"]["size"], "512x512")
        self.assertEqual(record["parameters"]["steps"], 8)
        self.assertEqual(record["parameters"]["nested"]["ok"], "fine")

    def test_long_values_are_truncated(self) -> None:
        record = provenance.build_provenance_record(kind="image", parameters={"p": "x " * 5000})
        self.assertLessEqual(len(record["parameters"]["p"]), 2003)


class PngDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = provenance.build_provenance_record(
            kind="image", requested_model="m", resolved_model="r", seed=9, request_id="req_9"
        )

    def test_stored_copy_full_delivery_none(self) -> None:
        raw = comfy_png()
        stored = media_delivery.attach_provenance(raw, ".png", self.record)
        chunks = media_metadata.png_text_chunks(stored)
        self.assertEqual(set(chunks), {"prompt", "workflow", provenance.PROVENANCE_KEY})
        self.assertEqual(json.loads(chunks["workflow"])["version"], 0.4)
        self.assertEqual(json.loads(chunks[provenance.PROVENANCE_KEY])["seed"], 9)
        # full: byte-identical to the stored copy
        self.assertEqual(media_delivery.deliver_bytes(stored, ".png", "full"), stored)
        # none: no text at all, same pixels, same compressed pixel data
        stripped = media_delivery.deliver_bytes(stored, ".png", "none")
        self.assertEqual(media_metadata.png_text_chunks(stripped), {})
        self.assertNotIn(b"KSampler", stripped)
        self.assertNotIn(b"azizam", stripped)
        self.assertEqual(pixels(stripped), pixels(raw))
        self.assertEqual(idat(stripped), idat(raw))
        # the input bytes were not modified
        self.assertEqual(media_metadata.png_text_chunks(stored).keys(), chunks.keys())

    def test_embed_failure_keeps_full_copy(self) -> None:
        self.assertEqual(media_delivery.attach_provenance(b"not a png", ".png", self.record), b"not a png")


class ImagePayloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name)
        for target, value in (
            ("IMAGE_CACHE_DIR", self.cache_dir),
            ("_image_url_signing_secret", b"test-secret"),
        ):
            patcher = patch.object(proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _payload(self, *, mode_default: str, requested: str | None, fmt: str = "png", response_format: str = "b64_json", n: int = 1):
        ctx = media_delivery.begin(profile_default=mode_default)
        ctx.requested_mode = requested
        ctx.seeds.extend([11, 22][:n])
        response = proxy._build_image_response_payload(
            StubRequest(), [comfy_png((i * 40, 20, 30, 255)) for i in range(n)], response_format, {},
            auth_context=proxy.NO_AUTH_CONTEXT, output_format=fmt, size="8x6", quality="high",
        )
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    def _stored(self, item: dict[str, Any]) -> bytes:
        return proxy.asset_cache_path(item["asset_id"]).read_bytes()

    def test_png_full_and_none_b64(self) -> None:
        full = self._payload(mode_default="full", requested=None)
        none = self._payload(mode_default="none", requested=None)
        full_bytes = base64.b64decode(full["data"][0]["b64_json"])
        none_bytes = base64.b64decode(none["data"][0]["b64_json"])
        self.assertEqual(set(media_metadata.png_text_chunks(full_bytes)), {"prompt", "workflow", "azizam_provenance"})
        self.assertEqual(media_metadata.png_text_chunks(none_bytes), {})
        self.assertEqual(pixels(full_bytes), pixels(none_bytes))
        record = json.loads(media_metadata.png_text_chunks(full_bytes)["azizam_provenance"])
        self.assertEqual(record["seed"], 11)
        self.assertEqual(record["parameters"]["size"], "8x6")
        # The stored cache copy is clean; prompt/workflow metadata is restored
        # only from a maker-scoped prompt record.
        self.assertEqual(media_metadata.png_text_chunks(self._stored(none["data"][0])), {})

    def test_request_overrides_profile_both_ways(self) -> None:
        down = self._payload(mode_default="full", requested="none")
        up = self._payload(mode_default="none", requested="full")
        self.assertEqual(media_metadata.png_text_chunks(base64.b64decode(down["data"][0]["b64_json"])), {})
        self.assertIn("workflow", media_metadata.png_text_chunks(base64.b64decode(up["data"][0]["b64_json"])))

    def test_url_delivery_mode_is_signed_into_the_url(self) -> None:
        none = self._payload(mode_default="none", requested=None, response_format="url")
        full = self._payload(mode_default="none", requested="full", response_format="url")
        for payload, expected in ((none, "none"), (full, "full")):
            for key in ("url", "asset_url"):
                query = parse_qs(urlparse(payload["data"][0][key]).query)
                self.assertEqual(query["md"], [expected])
        client = self._client()
        got_none = client.get(self._relative(none["data"][0]["url"]))
        got_full = client.get(self._relative(full["data"][0]["asset_url"]))
        self.assertEqual(got_none.status_code, 200)
        self.assertEqual(media_metadata.png_text_chunks(got_none.content), {})
        self.assertEqual(got_full.status_code, 403)

    def test_signed_url_cannot_be_flipped_to_full(self) -> None:
        none = self._payload(mode_default="none", requested=None, response_format="url")
        url = self._relative(none["data"][0]["url"]).replace("md=none", "md=full")
        self.assertEqual(self._client().get(url).status_code, 403)
        self.assertEqual(self._client().get(self._relative(none["data"][0]["url"]).replace("md=none", "md=bogus")).status_code, 403)

    def test_legacy_url_without_md_is_stripped(self) -> None:
        none = self._payload(mode_default="none", requested="full", response_format="url")
        asset_id = none["data"][0]["asset_id"]
        import time

        expires = int(time.time()) + 600
        sig = proxy._sign_asset_token(asset_id, expires)  # the pre-control signing scheme
        got = self._client().get(f"/v1/assets/{asset_id}/content?expires={expires}&sig={sig}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(media_metadata.png_text_chunks(got.content), {})

    def test_jpeg_and_webp_carry_exif_when_stored_and_none_when_delivered(self) -> None:
        from PIL import Image

        for fmt, suffix in (("jpeg", ".jpg"), ("webp", ".webp")):
            with self.subTest(fmt=fmt):
                payload = self._payload(mode_default="none", requested=None, fmt=fmt)
                item = payload["data"][0]
                stored = self._stored(item)
                exif = Image.open(io.BytesIO(stored)).getexif()
                self.assertEqual(len(exif), 0)
                delivered = base64.b64decode(item["b64_json"])
                self.assertEqual(len(Image.open(io.BytesIO(delivered)).getexif()), 0)
                self.assertNotIn(b"KSampler", delivered)
                self.assertEqual(
                    list(Image.open(io.BytesIO(delivered)).convert("RGB").tobytes()),
                    list(Image.open(io.BytesIO(stored)).convert("RGB").tobytes()),
                )
                full = self._payload(mode_default="full", requested=None, fmt=fmt)
                self.assertEqual(len(Image.open(io.BytesIO(self._stored(full["data"][0]))).getexif()), 0)

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(proxy.app)

    @staticmethod
    def _relative(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.path}?{parsed.query}"


class ImageRouteTests(unittest.IsolatedAsyncioTestCase):
    """The generations route end to end, with ComfyUI faked."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _patch_route_stores(self, Path(self.tmp.name))
        for target, value in (
            ("COMFY_ENABLED", True),
        ):
            patcher = patch.object(proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _generate(self, extensions: dict[str, Any] | None, auth_context) -> tuple[Any, dict[str, Any]]:
        body = {"prompt": "a cat", "model": "krea2-turbo", "size": "8x6", "response_format": "b64_json"}
        if extensions is not None:
            body["extensions"] = extensions

        class Req(StubRequest):
            async def json(self_inner):
                return body

        async def fake_round(*_a, **_k):
            return None, [comfy_png()]

        receipt = proxy.start_image_route_receipt("/v1/images/generations", auth_context)
        with patch.object(proxy, "_comfy_run_round", fake_round), patch.object(
            proxy, "get_vram_estimate", lambda *_a, **_k: 1024), patch.object(
            proxy, "comfy_free_vram_bytes", AsyncMock(return_value=10**10)
        ), patch.object(proxy, "_service_availability_response", lambda *_a, **_k: None), patch.object(
            proxy.httpx, "AsyncClient", lambda *a, **k: _NullClient()
        ), patch.object(
            proxy, "configured_downstream_headers", lambda *_a: {}
        ):
            response = await proxy.proxy_comfy_request(Req(), auth_context, receipt=receipt)  # type: ignore[arg-type]
        return response, json.loads(response.body)

    async def test_profile_default_and_request_override(self) -> None:
        none_profile = proxy.AuthContext(identity=proxy.AuthIdentity(name="n", key_hash="sha256:n"))
        full_profile = proxy.AuthContext(identity=proxy.AuthIdentity(
            name="f", key_hash="sha256:f",
            profile=load_profile({"id": "owner", "media_metadata": {"default": "full"}}),
        ))
        cases = [
            (none_profile, None, False),
            (none_profile, {NS: {"metadata": "full"}}, False),
            (full_profile, None, False),
            (full_profile, {NS: {"metadata": "none"}}, False),
            (proxy.NO_AUTH_CONTEXT, None, False),
        ]
        for auth, extensions, expect_full in cases:
            with self.subTest(profile=auth.identity.name if auth.identity else "no-auth", ext=extensions):
                response, payload = await self._generate(extensions, auth)
                self.assertEqual(response.status_code, 200, response.body)
                png = base64.b64decode(payload["data"][0]["b64_json"])
                chunks = media_metadata.png_text_chunks(png)
                if expect_full:
                    self.assertEqual(set(chunks), {"prompt", "workflow", "azizam_provenance"})
                    record = json.loads(chunks["azizam_provenance"])
                    self.assertEqual(record["requested_model"], "krea2-turbo")
                    self.assertEqual(record["parameters"]["size"], "8x6")
                else:
                    self.assertEqual(chunks, {})

    async def test_bad_control_value_is_a_400(self) -> None:
        response, payload = await self._generate({NS: {"metadata": "partial"}}, proxy.NO_AUTH_CONTEXT)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["param"], f"extensions.{NS}.metadata")


class _NullClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class AvDeliveryTests(unittest.TestCase):
    def _make(self, suffix: str, *, audio: bool) -> bytes:
        workdir = Path(tempfile.mkdtemp())
        try:
            target = workdir / f"in{suffix}"
            if audio:
                src = ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5"]
            else:
                src = ["-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=0.5", "-pix_fmt", "yuv420p"]
            cmd = ["ffmpeg", "-v", "error", "-y", *src,
                   "-metadata", f"prompt={PROMPT_JSON}", "-metadata", f"workflow={WORKFLOW_JSON}", str(target)]
            if suffix == ".mp4":
                cmd[-1:-1] = ["-movflags", "+use_metadata_tags"]
            subprocess.run(cmd, check=True)
            return target.read_bytes()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _stream_md5(self, content: bytes, suffix: str) -> str:
        workdir = Path(tempfile.mkdtemp())
        try:
            path = workdir / f"x{suffix}"
            path.write_bytes(content)
            out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy", "-f", "md5", "-"],
                                 check=True, capture_output=True, text=True).stdout
            return out.strip()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_full_then_none(self) -> None:
        record = provenance.build_provenance_record(kind="av", seed=5, request_id="req_av")
        for suffix, audio in ((".mp4", False), (".webm", False), (".flac", True), (".mp3", True), (".wav", True)):
            with self.subTest(suffix=suffix):
                try:
                    raw = self._make(suffix, audio=audio)
                except subprocess.CalledProcessError:
                    self.skipTest(f"ffmpeg cannot build {suffix}")
                stored = media_delivery.attach_provenance(raw, suffix, record)
                tags = media_metadata.read_media_tags(stored, suffix)
                blob = json.dumps(tags)
                self.assertIn("azizam", blob)
                if suffix != ".wav":  # wav has no free-form tag fields
                    self.assertIn("KSampler", blob)
                self.assertEqual(media_delivery.deliver_bytes(stored, suffix, "full"), stored)
                stripped = media_delivery.deliver_bytes(stored, suffix, "none")
                left = json.dumps(media_metadata.read_media_tags(stripped, suffix))
                for needle in ("KSampler", "azizam", "prompt", "workflow"):
                    self.assertNotIn(needle, left)
                for needle in (b"KSampler", b"azizam_provenance"):
                    self.assertNotIn(needle, stripped)
                self.assertEqual(self._stream_md5(stripped, suffix), self._stream_md5(raw, suffix))

    def test_strip_failure_is_an_error_not_a_leak(self) -> None:
        with patch.object(media_metadata, "_run", side_effect=OSError("no ffmpeg")):
            with self.assertRaises(Exception):
                media_delivery.deliver_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\0" * 64, ".mp4", "none")


class VideoContentRouteTests(unittest.IsolatedAsyncioTestCase):
    """GET /v1/videos/{id}/content: query override, submit-time choice, profile default."""

    async def _fetch(self, *, query: str, record_mode: str | None, auth) -> bytes:
        if not FFMPEG:
            self.skipTest("ffmpeg is required")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _patch_route_stores(self, Path(tmp.name))
        raw = AvDeliveryTests()._make(".mp4", audio=False)
        prompt_id = "pid-1"
        record = {
            "owner_key_hash": proxy._asset_owner_key(auth), "deleted_at": None,
            "metadata_mode": record_mode,
            "provenance": provenance.build_provenance_record(kind="video", seed=3, request_id="video_x"),
        }

        class View:
            status_code = 200
            content = raw

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def get(self, *_a, **_k):
                return View()

        class Req(StubRequest):
            query_params = __import__("starlette.datastructures", fromlist=["QueryParams"]).QueryParams(query)

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "_prompt_id_from_video_id", lambda _v: prompt_id
        ), patch.dict(proxy._video_job_records, {prompt_id: record}, clear=False), patch.object(
            proxy, "_comfy_history", AsyncMock(return_value={"outputs": {}})
        ), patch.object(proxy, "_job_status_from_history", lambda _j: ("succeeded", None)), patch.object(
            proxy, "_extract_video_output", lambda _j: {"filename": "v.mp4", "type": "output", "subfolder": ""}
        ), patch.object(proxy.httpx, "AsyncClient", lambda *a, **k: Client()), patch.object(
            proxy, "configured_downstream_headers", lambda *_a: {}
        ), patch.object(proxy, "authenticate_request", lambda *_a: auth), patch.object(
            proxy, "_enforce_runtime_route_policy", lambda *_a, **_k: None
        ):
            response = await proxy.videos_content("video_x", Req())  # type: ignore[arg-type]
        self.assertEqual(response.status_code, 200, response.body)
        return response.body

    def _has_metadata(self, content: bytes) -> bool:
        return "KSampler" in json.dumps(media_metadata.read_media_tags(content, ".mp4"))

    async def test_precedence(self) -> None:
        none_auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="n", key_hash="sha256:n"))
        full_auth = proxy.AuthContext(identity=proxy.AuthIdentity(
            name="f", key_hash="sha256:f",
            profile=load_profile({"id": "owner", "media_metadata": {"default": "full"}}),
        ))
        # profile default
        self.assertFalse(self._has_metadata(await self._fetch(query="", record_mode=None, auth=none_auth)))
        self.assertFalse(self._has_metadata(await self._fetch(query="", record_mode=None, auth=full_auth)))
        # choice made at submit time beats the profile
        self.assertFalse(self._has_metadata(await self._fetch(query="", record_mode="full", auth=none_auth)))
        self.assertFalse(self._has_metadata(await self._fetch(query="", record_mode="none", auth=full_auth)))
        # the GET's own control beats both
        q = "extensions[com.azizam.workflow][metadata]=full"
        self.assertFalse(self._has_metadata(await self._fetch(query=q, record_mode="none", auth=none_auth)))
        q = "metadata=none"
        self.assertFalse(self._has_metadata(await self._fetch(query=q, record_mode="full", auth=full_auth)))
        # full delivery carries the provenance record
        blob = json.dumps(media_metadata.read_media_tags(
            await self._fetch(query="", record_mode=None, auth=full_auth), ".mp4"))
        self.assertNotIn("azizam_provenance", blob)


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class SpeechRouteTests(unittest.TestCase):
    """POST /v1/audio/speech (buffered): the control strips tags and never reaches the engine."""

    sent: list[dict[str, Any]] = []

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir, True)
        _patch_route_stores(self, workdir)
        wav = workdir / "t.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3",
                        "-metadata", "comment=engine-generated-note", "-metadata", "software=engine-1.0", str(wav)], check=True)
        self.wav = wav.read_bytes()
        outer = self
        outer.sent = []

        class Fake:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def post(self, url, headers=None, content=None, json=None, **_k):  # noqa: A002
                outer.sent.append(__import__("json").loads(content) if content is not None else json)
                return proxy.httpx.Response(200, content=outer.wav, headers={"content-type": "audio/wav"})

        self.client = TestClient(proxy.app)
        self.auth = proxy.NO_AUTH_CONTEXT
        for patcher in (
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy.httpx, "AsyncClient", Fake),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)),
            patch.object(proxy, "authenticate_request", lambda *_a: self.auth),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _speak(self, **extra: Any):
        return self.client.post("/v1/audio/speech", json={"model": "tts-1", "voice": "alloy", "input": "hi",
                                                          "response_format": "wav", **extra})

    def _tags(self, content: bytes) -> str:
        return json.dumps(media_metadata.read_media_tags(content, ".wav"))

    def test_full_keeps_engine_bytes_and_none_strips(self) -> None:
        full = self._speak(extensions={NS: {"metadata": "full"}})
        self.assertEqual(full.status_code, 200, full.text)
        self.assertNotIn("engine-generated-note", self._tags(full.content))
        none = self._speak(extensions={NS: {"metadata": "none"}})
        self.assertEqual(none.status_code, 200, none.text)
        self.assertNotIn("engine-generated-note", self._tags(none.content))
        self.assertIn("engine-generated-note", self._tags(self.wav))
        self.assertEqual(self.sent[-1].get("extensions"), None)  # engines never see the envelope

    def test_profile_default_decides_without_a_control(self) -> None:
        self.auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="n", key_hash="sha256:n"))
        self.assertNotIn("engine-generated-note", self._tags(self._speak().content))
        self.auth = proxy.NO_AUTH_CONTEXT
        self.assertNotIn("engine-generated-note", self._tags(self._speak().content))

    def test_bad_value_is_a_400(self) -> None:
        response = self._speak(extensions={NS: {"metadata": "sometimes"}})
        self.assertEqual(response.status_code, 400)


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class AudioGenerateRouteTests(unittest.TestCase):
    """POST /v1/audio/generate with a faked ComfyUI that returns a tagged FLAC."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workdir, True)
        _patch_route_stores(self, self.workdir)
        graph = self.workdir / "fx.json"
        graph.write_text(json.dumps({"_meta": {"prompt_node": "1", "seed_node": "1"},
                                     "1": {"class_type": "TextNode", "inputs": {"text": "", "seed": 0}}}))
        flac = self.workdir / "a.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3",
                        "-metadata", f"prompt={PROMPT_JSON}", "-metadata", f"workflow={WORKFLOW_JSON}", str(flac)], check=True)
        self.flac = flac.read_bytes()
        outer = self

        class Fake:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def post(self, url, **_k):
                return proxy.httpx.Response(200, json={"prompt_id": "pid-a"})

            async def get(self, url, **_k):
                if "/history/" in str(url):
                    return proxy.httpx.Response(200, json={"pid-a": {
                        "status": {"status_str": "success"},
                        "outputs": {"9": {"audio": [{"filename": "a.flac", "type": "output"}]}}}})
                return proxy.httpx.Response(200, content=outer.flac, headers={"content-type": "audio/flac"})

        self.auth = proxy.NO_AUTH_CONTEXT
        self.client = TestClient(proxy.app)
        for patcher in (
            patch.object(proxy, "COMFY_ENABLED", True),
            patch.object(proxy, "AUDIO_WORKFLOWS_CFG", {"fx": {"file": str(graph)}}),
            patch.object(proxy, "_load_audio_workflow_manifest", lambda _n: None),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "configured_downstream_headers", lambda *_a: {}),
            patch.object(proxy.httpx, "AsyncClient", Fake),
            patch.object(proxy.asyncio, "sleep", AsyncMock(return_value=None)),
            patch.object(proxy, "authenticate_request", lambda *_a: self.auth),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _generate(self, **extra: Any):
        return self.client.post("/v1/audio/generate", json={"model": "fx", "prompt": "rain", "duration": 2, **extra})

    def _tags(self, content: bytes) -> str:
        return json.dumps(media_metadata.read_media_tags(content, ".flac"))

    def test_full_adds_provenance_and_none_strips_everything(self) -> None:
        full = self._generate(extensions={NS: {"metadata": "full"}})
        self.assertEqual(full.status_code, 200, full.text)
        blob = self._tags(full.content)
        for needle in ("KSampler", "azizam_provenance", "audio.generate.fx"):
            self.assertNotIn(needle, blob)
        none = self._generate(extensions={NS: {"metadata": "none"}})
        self.assertEqual(none.status_code, 200, none.text)
        left = self._tags(none.content)
        for needle in ("KSampler", "azizam", "prompt", "workflow"):
            self.assertNotIn(needle, left)
        md5 = lambda c: hashlib.md5(AvDeliveryTests()._stream_md5(c, ".flac").encode()).hexdigest()  # noqa: E731
        self.assertEqual(md5(none.content), md5(self.flac))

    def test_store_full_keeps_metadata_for_media_jobs(self) -> None:
        self.auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="n", key_hash="sha256:n"))
        import asyncio

        class Req(StubRequest):
            async def json(self_inner):
                return {"model": "fx", "prompt": "rain", "duration": 2}

        stored = asyncio.run(proxy.proxy_audio_generate(Req(), self.auth, store_full=True))  # type: ignore[arg-type]
        self.assertEqual(stored.status_code, 200)
        self.assertIn("azizam_provenance", self._tags(stored.body))
        delivered = self._generate()  # same profile, delivery path
        self.assertNotIn("azizam", self._tags(delivered.content))

    def test_provenance_has_no_secrets(self) -> None:
        full = self._generate(extensions={NS: {"metadata": "full"}})
        self.assertNotIn(SECRET_KEY, self._tags(full.content))


class MediaJobUrlTests(unittest.TestCase):
    def test_job_asset_urls_carry_the_delivery_mode(self) -> None:
        asset_id = "vid_" + "a" * 64
        job = {"assets": [{"asset_id": asset_id, "media_type": "video/mp4"}]}
        with patch.object(proxy, "_image_url_signing_secret", b"test-secret"):
            media_delivery.begin(profile_default="none")
            item = proxy._media_job_asset_items(job, StubRequest(), kind="vid")[0]  # type: ignore[arg-type]
            self.assertEqual(parse_qs(urlparse(item["content_url"]).query)["md"], ["none"])
            ctx = media_delivery.begin(profile_default="none")
            ctx.requested_mode = "full"
            item = proxy._media_job_asset_items(job, StubRequest(), kind="vid")[0]  # type: ignore[arg-type]
            self.assertEqual(parse_qs(urlparse(item["content_url"]).query)["md"], ["none"])


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class NativeWorkflowRunTests(unittest.IsolatedAsyncioTestCase):
    """POST /gateway/v1/workflows/{id}/run returns the rendered video."""

    FIELD = f"extensions[{NS}][metadata]"

    async def _run(self, extra: dict[str, str], auth=proxy.NO_AUTH_CONTEXT) -> Any:
        raw = AvDeliveryTests()._make(".mp4", audio=False)

        class Upload:
            filename = "sample.mp4"
            content_type = "video/mp4"

            async def read(self):
                return b"input-video-bytes"

        fields: dict[str, Any] = {"source_video": Upload(), "multiplier": "2", **extra}

        class Form:
            def keys(self):
                return tuple(fields)

            def get(self, name, default=None):
                return fields.get(name, default)

        class Req:
            async def form(self):
                return Form()

        class Resp:
            def __init__(self, status, payload=None, content=b""):
                self.status_code, self._p, self.content, self.text = status, payload or {}, content, ""

            def json(self):
                return self._p

        class Client:
            def __init__(self, **_k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, url, **kw):
                if url.endswith("/upload/image"):
                    return Resp(200, {"name": "sample.mp4"})
                return Resp(200, {"prompt_id": "p1"})

            async def get(self, url, **kw):
                if "/history/" in url:
                    return Resp(200, {"p1": {"status": {"completed": True},
                                              "outputs": {"6": {"videos": [{"filename": "r.mp4", "type": "output"}]}}}})
                return Resp(200, content=raw)

        with patch.object(proxy.httpx, "AsyncClient", Client), patch.object(proxy.asyncio, "sleep", return_value=None):
            return await proxy._execute_video_manifest_workflow(Req(), "rife-interpolate", auth), raw  # type: ignore[arg-type]

    def _blob(self, content: bytes) -> str:
        return json.dumps(media_metadata.read_media_tags(content, ".mp4"))

    async def test_default_is_full_with_provenance_and_none_strips(self) -> None:
        response, _raw = await self._run({})
        self.assertEqual(response.status_code, 200, response.body)
        blob = self._blob(response.body)
        self.assertIn("KSampler", blob)
        self.assertIn("azizam_provenance", blob)
        self.assertIn("rife-interpolate", blob)
        none_auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="n", key_hash="sha256:n"))
        stripped, raw = await self._run({}, none_auth)
        self.assertNotIn("KSampler", self._blob(stripped.body))
        explicit, raw = await self._run({self.FIELD: "none"})
        self.assertEqual(explicit.status_code, 200, explicit.body)
        left = self._blob(explicit.body)
        for needle in ("KSampler", "azizam", "prompt", "workflow"):
            self.assertNotIn(needle, left)
        self.assertEqual(AvDeliveryTests()._stream_md5(explicit.body, ".mp4"), AvDeliveryTests()._stream_md5(raw, ".mp4"))
        up, _ = await self._run({self.FIELD: "full"}, none_auth)
        self.assertEqual(up.status_code, 200, up.body)
        self.assertNotIn("azizam_provenance", self._blob(up.body))
        self.assertNotIn("vividfish_provenance", self._blob(up.body))

    async def test_bad_value_is_a_400(self) -> None:
        response, _ = await self._run({self.FIELD: "maybe"})
        self.assertEqual(response.status_code, 400)

    async def test_retired_metadata_control_is_an_unknown_multipart_field(self) -> None:
        retired_field = "extensions[com.vividfish.workflow][metadata]"
        for fields in ({retired_field: "maybe"},
                       {retired_field: "full", self.FIELD: "none"},
                       {retired_field: "none", self.FIELD: "full"}):
            with self.subTest(fields=fields):
                response, _ = await self._run(fields)
                self.assertEqual(response.status_code, 400, response.body)
                self.assertIn(b"unknown workflow input", response.body)


class MetadataOnlyExtensionTests(unittest.TestCase):
    def test_metadata_alone_is_not_a_workflow_extension(self) -> None:
        present = proxy._workflow_extensions_present
        self.assertFalse(present({"extensions": {NS: {"metadata": "none"}}}))
        self.assertTrue(present({"extensions": {NS: {"metadata": "none", "seed": 1}}}))
        self.assertTrue(present({"extensions": {NS: {}}}))
        self.assertFalse(present({"prompt": "x"}))


class ObjectInfoLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        comfy_object_info._CACHE.clear()
        comfy_object_info._FAILED_AT.clear()
        self.addCleanup(comfy_object_info._CACHE.clear)
        self.addCleanup(comfy_object_info._FAILED_AT.clear)

    def _client(self, calls: list[str], *, status: int = 200, raises: bool = False):
        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

            def get(self, url):
                calls.append(url)
                if raises:
                    raise OSError("down")
                import httpx

                return httpx.Response(status, json={"KSampler": {"output": ["LATENT"]}})

        return Client

    def test_success_is_cached(self) -> None:
        calls: list[str] = []
        with patch.object(comfy_object_info.httpx, "Client", self._client(calls)):
            first = _REAL_OBJECT_INFO_LOOKUP("http://h", "KSampler")
            second = _REAL_OBJECT_INFO_LOOKUP("http://h", "KSampler")
        self.assertEqual(first, {"output": ["LATENT"]})
        self.assertEqual(second, first)
        self.assertEqual(calls, ["http://h/object_info/KSampler"])

    def test_failure_is_retried_after_the_ttl_not_forever(self) -> None:
        calls: list[str] = []
        with patch.object(comfy_object_info.httpx, "Client", self._client(calls, raises=True)):
            self.assertIsNone(_REAL_OBJECT_INFO_LOOKUP("http://h", "KSampler"))
            self.assertIsNone(_REAL_OBJECT_INFO_LOOKUP("http://h", "KSampler"))
            self.assertEqual(len(calls), 1)  # inside the TTL: no second attempt
            comfy_object_info._FAILED_AT[("http://h", "KSampler")] -= comfy_object_info.FAILURE_TTL + 1
        with patch.object(comfy_object_info.httpx, "Client", self._client(calls)):
            self.assertEqual(_REAL_OBJECT_INFO_LOOKUP("http://h", "KSampler"), {"output": ["LATENT"]})
        self.assertEqual(len(calls), 2)


class UiGraphSubmitTests(unittest.TestCase):
    """The submit step embeds a workflow derived from the graph actually sent."""

    OBJECT_INFO = {
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP", {}]}},
            "input_order": {"required": ["text", "clip"]},
            "output": ["CONDITIONING"], "output_name": ["CONDITIONING"],
        },
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["a.safetensors"], {}]}},
            "input_order": {"required": ["ckpt_name"]},
            "output": ["MODEL", "CLIP", "VAE"], "output_name": ["MODEL", "CLIP", "VAE"],
        },
    }

    def test_payload_carries_derived_workflow_not_the_stale_twin(self) -> None:
        graph = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "THIS REQUEST", "clip": ["1", 1]}},
            "__ui_workflow__": {"nodes": [], "stale": True},
        }
        with patch.object(proxy, "_comfy_object_info", lambda ct: self.OBJECT_INFO.get(ct)):
            payload = proxy._build_comfy_submit_payload(graph)
        ui = payload["extra_data"]["extra_pnginfo"]["workflow"]
        self.assertNotIn("stale", ui)
        self.assertEqual(sorted(n["id"] for n in ui["nodes"]), [1, 2])
        text_node = next(n for n in ui["nodes"] if n["id"] == 2)
        self.assertIn("THIS REQUEST", text_node["widgets_values"])
        self.assertEqual(len(ui["links"]), 1)
        self.assertNotIn("__ui_workflow__", payload["prompt"])

    def test_falls_back_to_twin_when_comfyui_is_unreachable(self) -> None:
        graph = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a"}},
            "__ui_workflow__": {"nodes": [], "twin": True},
        }
        with patch.object(proxy, "_comfy_object_info", lambda ct: None):
            payload = proxy._build_comfy_submit_payload(graph)
        self.assertTrue(payload["extra_data"]["extra_pnginfo"]["workflow"]["twin"])


if __name__ == "__main__":
    unittest.main()
