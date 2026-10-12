"""Tests for the gateway-composed Flux2 seamless-tile route."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import access_context  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.extension_catalog import summarize_extensions  # noqa: E402
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE as NS  # noqa: E402
from gateway.manifests import bind_backend_request, load_workflow_manifest  # noqa: E402
from gateway import media_jobs  # noqa: E402


MANIFEST_PATH = ROOT / "manifests/workflows/flux2-klein-4b-seamless-tile.yml"
INPAINT_MANIFEST_PATH = ROOT / "manifests/workflows/flux2-klein-4b-inpaint.yml"


def _png_from_pixels(
    pixels: list[tuple[int, int, int, int]],
    *,
    size: tuple[int, int],
) -> bytes:
    from PIL import Image

    image = Image.new("RGBA", size)
    image.putdata(pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _pixels(content: bytes) -> list[tuple[int, int, int, int]]:
    from PIL import Image

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    return [
        image.getpixel((x, y))
        for y in range(image.height)
        for x in range(image.width)
    ]


class _JsonRequest:
    headers: dict[str, str] = {}
    url = SimpleNamespace(scheme="http", netloc="gateway.test")

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _UploadResponse:
    status_code = 200
    text = "ok"

    def __init__(self, name: str) -> None:
        self._name = name

    def json(self) -> dict[str, str]:
        return {"name": self._name}


class _FakeAsyncClient:
    uploads = 0
    upload_files: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **kwargs: Any) -> _UploadResponse:
        type(self).uploads += 1
        type(self).upload_files.append(kwargs.get("files") or {})
        return _UploadResponse(f"uploaded-{type(self).uploads}.png")


class TestFlux2SeamlessTileHelpers(unittest.TestCase):
    def test_offset_wraps_pixels(self) -> None:
        raw = _png_from_pixels(
            [
                (255, 0, 0, 255),
                (0, 255, 0, 255),
                (0, 0, 255, 255),
                (255, 255, 0, 255),
            ],
            size=(4, 1),
        )

        shifted = proxy._offset_image_bytes(raw, x_fraction=0.5, y_fraction=0)

        self.assertEqual(
            _pixels(shifted),
            [
                (0, 0, 255, 255),
                (255, 255, 0, 255),
                (255, 0, 0, 255),
                (0, 255, 0, 255),
            ],
        )

    def test_seam_cross_mask_uses_white_as_edit_region(self) -> None:
        mask = proxy._make_seam_cross_mask_png(4, 4, seam_width=2)

        values = [pixel[0] for pixel in _pixels(mask)]
        self.assertEqual(
            values,
            [
                0, 255, 255, 0,
                255, 255, 255, 255,
                255, 255, 255, 255,
                0, 255, 255, 0,
            ],
        )


class TestFlux2SeamlessTileManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_gateway_manifest_declares_composed_model(self) -> None:
        self.assertEqual(
            self.manifest["id"],
            "image.generate.flux2-klein-4b-seamless-tile",
        )
        gateway = self.manifest["bindings"]["gateway"]
        self.assertEqual(gateway["model_id"], "flux2-klein-4b-seamless-tile")
        self.assertEqual(gateway["compose"], "offset_inpaint_tile")
        self.assertEqual(gateway["base_model"], "flux2-klein-4b")
        self.assertEqual(gateway["inpaint_model"], "flux2-klein-4b-inpaint")

    def test_gateway_controls_bind_to_composition_metadata(self) -> None:
        result = bind_backend_request(
            self.manifest,
            "gateway",
            {},
            {
                "steps": 6,
                "guidance": 3.5,
                "seed": 42,
                "seam_mask_width": 80,
                "seam_mask_expand": 4,
                "seam_mask_feather": 16,
                "seam_steps": 10,
                "seam_guidance": 4.5,
                "seam_denoise": 0.52,
                "seam_prompt": "repair the seam",
            },
        )

        self.assertEqual(
            result.request,
            {
                "base": {"steps": 6, "guidance": 3.5, "seed": 42},
                "seam": {
                    "mask_width": 80,
                    "mask_expand": 4,
                    "mask_feather": 16,
                    "steps": 10,
                    "guidance": 4.5,
                    "denoise": 0.52,
                    "prompt": "repair the seam",
                },
            },
        )

    def test_discovery_exposes_gateway_tile_controls(self) -> None:
        registry = WorkflowRegistry.from_directory(ROOT / "manifests/workflows")
        catalog = summarize_extensions(registry, audience="client")
        workflow_targets = [
            workflow
            for extension in catalog["extensions"]
            if extension["namespace"] == NS
            for route in extension["routes"]
            if route["route"] == "/v1/images/generations"
            for target in route["targets"]
            for workflow in target["workflows"]
            if workflow["id"] == "image.generate.flux2-klein-4b-seamless-tile"
        ]

        self.assertEqual(len(workflow_targets), 1)
        self.assertEqual(
            set(workflow_targets[0]["controls"]),
            {
                "steps",
                "guidance",
                "seed",
                "seam_mask_width",
                "seam_mask_expand",
                "seam_mask_feather",
                "seam_steps",
                "seam_guidance",
                "seam_denoise",
                "seam_prompt",
            },
        )

    def test_openai_models_entry_uses_gateway_model_id(self) -> None:
        entries = proxy._build_workflow_model_entries(
            WorkflowRegistry.from_directory(ROOT / "manifests/workflows")
        )
        by_id = {entry["id"]: entry for entry in entries}

        self.assertIn("flux2-klein-4b-seamless-tile", by_id)
        tile = by_id["flux2-klein-4b-seamless-tile"]
        self.assertEqual(tile["azizam_workflow_id"], self.manifest["id"])
        self.assertEqual(tile["azizam_backend"], "gateway")
        rendered = json.dumps(tile, sort_keys=True)
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("/media/", rendered)


class TestFlux2InpaintSeedManifest(unittest.TestCase):
    def test_inpaint_manifest_binds_seed_to_noise_seed(self) -> None:
        manifest = load_workflow_manifest(INPAINT_MANIFEST_PATH)
        workflow = proxy.load_comfy_workflow("flux2-klein-4b-inpaint")
        assert workflow is not None
        workflow.pop("_meta", None)

        bound = bind_backend_request(
            manifest,
            "comfyui",
            workflow,
            {"seed": 12345},
        ).request

        self.assertEqual(bound["25"]["inputs"]["noise_seed"], 12345)


class TestFlux2SeamlessTileRoute(unittest.IsolatedAsyncioTestCase):
    async def test_transparent_background_is_rejected_for_tile_composition(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations",
            access_context(),
        )
        body = {
            "model": "flux2-klein-4b-seamless-tile",
            "prompt": "painted underwater rock texture",
            "size": "1024x1024",
            "background": "transparent",
        }

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy,
            "configured_downstream_headers",
            lambda _auth_context, _service: {},
        ):
            response = await proxy.proxy_comfy_request(
                _JsonRequest(body),  # type: ignore[arg-type]
                access_context(),
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["error"]["param"], "background")
        self.assertEqual(
            receipt.rejected_inputs.get("background"),
            "unsupported_for_gateway_composition",
        )

    async def test_route_generates_offsets_masks_and_inpaints(self) -> None:
        body = {
            "model": "flux2-klein-4b-seamless-tile",
            "prompt": "painted underwater rock texture",
            "size": "65x67",
            "n": 1,
            "response_format": "b64_json",
            "extensions": {
                NS: {
                    "steps": 5,
                    "guidance": 3.0,
                    "seed": 10,
                    "seam_mask_width": 2,
                    "seam_mask_feather": 6,
                    "seam_denoise": 0.5,
                }
            },
        }
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations",
            access_context(),
        )
        run_calls: list[dict[str, Any]] = []
        # Odd dimensions with a distinct value in every pixel make both offset
        # directions and both axes observable; expectations below are computed
        # by independent index arithmetic, not by the production helper.
        width, height = 65, 67
        base_pixels = [
            (x, y, (x + 3 * y) % 256, 255)
            for y in range(height) for x in range(width)
        ]
        repaired_pixels = [
            (128 + x, 128 + y, (2 * x + y) % 256, 255)
            for y in range(height) for x in range(width)
        ]
        base_image = _png_from_pixels(base_pixels, size=(width, height))
        repaired_offset = _png_from_pixels(repaired_pixels, size=(width, height))
        expected_upload = [
            base_pixels[((y - 34) % height) * width + ((x - 32) % width)]
            for y in range(height) for x in range(width)
        ]
        expected_result = [
            repaired_pixels[((y + 34) % height) * width + ((x + 32) % width)]
            for y in range(height) for x in range(width)
        ]
        expected_mask = [
            255 if x in (31, 32) or y in (32, 33) else 0
            for y in range(height) for x in range(width)
        ]

        async def fake_run_round(
            _client: Any,
            workflow: dict[str, Any],
            _headers: dict[str, str],
            label: str,
            **_kwargs: Any,
        ) -> tuple[Response | None, list[bytes]]:
            run_calls.append({"label": label, "workflow": copy.deepcopy(workflow)})
            if "base" in label:
                return None, [base_image]
            return None, [repaired_offset]

        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []
        with tempfile.TemporaryDirectory() as cache_dir:
            media_db = Path(cache_dir) / "media_jobs.db"
            media_jobs.init_db(media_db)
            with patch.object(
                proxy,
                "IMAGE_CACHE_DIR",
                Path(cache_dir),
            ), patch.object(
                media_jobs,
                "DB_PATH",
                media_db,
            ), patch.object(
                proxy,
                "_image_url_signing_secret",
                b"test-secret",
            ), patch.object(
                proxy,
                "COMFY_ENABLED",
                True,
            ), patch.object(
                proxy,
                "configured_downstream_headers",
                lambda _auth_context, _service: {},
            ), patch.object(
                proxy.httpx,
                "AsyncClient",
                _FakeAsyncClient,
            ), patch.object(
                proxy,
                "_comfy_run_round",
                fake_run_round,
            ):
                response = await proxy.proxy_comfy_request(
                    _JsonRequest(body),  # type: ignore[arg-type]
                    access_context(),
                    receipt=receipt,
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Image-Seamless-Tile-Strategy"], "offset-inpaint")
        self.assertEqual(response.headers["X-Image-Rounds"], "2")
        self.assertEqual(response.headers["X-Image-Returned"], "1")
        self.assertEqual(response.headers["X-Image-Seam-Mask-Width"], "2")
        self.assertEqual(len(run_calls), 2)
        self.assertEqual(run_calls[0]["workflow"]["31"]["inputs"]["seed"], 10)
        self.assertEqual(run_calls[0]["workflow"]["31"]["inputs"]["steps"], 5)
        self.assertEqual(run_calls[0]["workflow"]["35"]["inputs"]["guidance"], 3.0)
        self.assertEqual(run_calls[1]["workflow"]["25"]["inputs"]["noise_seed"], 11)
        self.assertEqual(run_calls[1]["workflow"]["49"]["inputs"]["denoise"], 0.5)
        self.assertEqual(run_calls[1]["workflow"]["19"]["inputs"]["left"], 6)

        self.assertEqual(_FakeAsyncClient.uploads, 2)
        uploaded_image = _FakeAsyncClient.upload_files[0]["image"][1]
        self.assertEqual(_pixels(uploaded_image), expected_upload)
        uploaded_mask = _FakeAsyncClient.upload_files[1]["image"][1]
        mask_values = [pixel[0] for pixel in _pixels(uploaded_mask)]
        self.assertEqual(mask_values, expected_mask)
        inpaint = run_calls[1]["workflow"]
        self.assertEqual(inpaint["10"]["inputs"]["image"], "uploaded-1.png")
        self.assertEqual(inpaint["12"]["inputs"]["image"], "uploaded-2.png")
        for node_id in ("11", "14", "48"):
            self.assertEqual(inpaint[node_id]["inputs"]["width"], width)
            self.assertEqual(inpaint[node_id]["inputs"]["height"], height)

        payload = json.loads(response.body.decode("utf-8"))
        returned = payload["data"][0]
        self.assertIn("asset_id", returned)
        self.assertIn("asset_url", returned)
        self.assertEqual(_pixels(base64_to_png(returned["b64_json"])), expected_result)
        self.assertNotEqual(expected_result, base_pixels)
        self.assertNotEqual(expected_result, repaired_pixels)
        self.assertEqual(receipt.manifest_id, self.manifest_id())

    def manifest_id(self) -> str:
        return "image.generate.flux2-klein-4b-seamless-tile"


def base64_to_png(value: str) -> bytes:
    import base64

    return base64.b64decode(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
