"""Contract tests for the SDXL seamless tile generation workflow."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.extension_catalog import summarize_extensions  # noqa: E402
from gateway.image_extensions import (  # noqa: E402
    STANDARD_IMAGE_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_workflow_extensions,
    resolve_workflow_extensions,
)
from gateway.manifests import (  # noqa: E402
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)


NS = WORKFLOW_EXTENSION_NAMESPACE
MANIFEST_PATH = ROOT / "manifests/workflows/sdxl-seamless-tile.yml"
WORKFLOW_PATH = ROOT / "workflows/images/sdxl-seamless-tile.json"

EXTENSION_INPUTS = (
    "steps",
    "guidance",
    "seed",
    "batch_size",
    "negative_prompt",
    "sampler",
    "scheduler",
    "seam_blending",
)


def _load_workflow() -> dict[str, object]:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


class TestSdxlSeamlessTileManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_identity_and_declared_inputs(self) -> None:
        identity = manifest_identity(self.manifest)

        self.assertEqual(identity.id, "image.generate.sdxl-seamless-tile")
        self.assertEqual(identity.version, "0.1.1")
        self.assertTrue(identity.trusted)
        self.assertEqual(self.manifest["intent"], "image.generate")
        self.assertEqual(
            set(self.manifest["inputs"]),
            {
                "prompt",
                "width",
                "height",
                "steps",
                "guidance",
                "seed",
                "batch_size",
                "negative_prompt",
                "sampler",
                "scheduler",
                "seam_blending",
            },
        )

    def test_manifest_targets_existing_workflow_nodes(self) -> None:
        workflow = _load_workflow()
        mapping = self.manifest["bindings"]["comfyui"]["map"]

        self.assertEqual(set(mapping), set(self.manifest["inputs"]))
        for entries in mapping.values():
            self.assertIsInstance(entries, list)
            self.assertGreater(len(entries), 0)
            for entry in entries:
                node = entry["node"]
                input_name = entry["input"]
                self.assertIn(node, workflow)
                self.assertIn(input_name, workflow[node]["inputs"])

    def test_inputs_bind_to_texture_workflow_controls(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            self.manifest,
            {
                "prompt": "hand-painted underwater basalt floor tile",
                "width": 768,
                "height": 768,
                "steps": 32,
                "guidance": 6.5,
                "seed": 4242,
                "batch_size": 2,
                "negative_prompt": "straight lines, text, border",
                "sampler": "dpmpp_2m_sde",
                "scheduler": "karras",
                "seam_blending": 0.5,
            },
        )
        self.assertTrue(result.ok, result.rejected)

        mutated = apply_backend_binding(self.manifest, "comfyui", workflow, result.accepted)

        self.assertEqual(workflow, original)
        self.assertEqual(mutated["6"]["inputs"]["text"], "hand-painted underwater basalt floor tile")
        self.assertEqual(mutated["27"]["inputs"]["width"], 768)
        self.assertEqual(mutated["27"]["inputs"]["height"], 768)
        self.assertEqual(mutated["27"]["inputs"]["batch_size"], 2)
        self.assertEqual(mutated["8"]["inputs"]["width"], 768)
        self.assertEqual(mutated["8"]["inputs"]["height"], 768)
        self.assertEqual(mutated["31"]["inputs"]["steps"], 32)
        self.assertEqual(mutated["31"]["inputs"]["cfg"], 6.5)
        self.assertEqual(mutated["31"]["inputs"]["seed"], 4242)
        self.assertEqual(mutated["31"]["inputs"]["sampler_name"], "dpmpp_2m_sde")
        self.assertEqual(mutated["31"]["inputs"]["scheduler"], "karras")
        self.assertEqual(mutated["33"]["inputs"]["text"], "straight lines, text, border")
        self.assertEqual(mutated["45"]["inputs"]["blending"], 0.5)

    def test_invalid_controls_are_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {
                "prompt": "x",
                "sampler": "not-a-sampler",
                "scheduler": "not-a-scheduler",
                "seam_blending": 0.7,
            },
        )

        self.assertIn("sampler", result.rejected)
        self.assertIn("scheduler", result.rejected)
        self.assertIn("seam_blending", result.rejected)


class TestSdxlSeamlessTileExtensionContract(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_standard_generate_fields_remain_route_owned(self) -> None:
        body = {
            "prompt": "mossy stone floor",
            "model": "sdxl-seamless-tile",
            "n": 1,
            "size": "1024x1024",
            "extensions": {
                NS: {
                    "steps": 28,
                    "guidance": 6.5,
                    "negative_prompt": "text, logo, seam",
                    "seam_blending": 0.45,
                }
            },
        }

        result = bind_workflow_extensions(
            body,
            self.manifest,
            "comfyui",
            _load_workflow(),
            supported_names=EXTENSION_INPUTS,
            standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
        )

        self.assertTrue(result.ok, (result.unknown_top_level, result.validation.rejected))
        assert result.request is not None
        self.assertEqual(result.request["31"]["inputs"]["steps"], 28)
        self.assertEqual(result.request["31"]["inputs"]["cfg"], 6.5)
        self.assertEqual(result.request["33"]["inputs"]["text"], "text, logo, seam")
        self.assertEqual(result.request["45"]["inputs"]["blending"], 0.45)

    def test_width_height_are_route_owned_not_extension_applied(self) -> None:
        body = {
            "prompt": "x",
            "extensions": {NS: {"width": 512, "height": 512}},
        }

        result = resolve_workflow_extensions(
            body,
            self.manifest,
            supported_names=EXTENSION_INPUTS,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.ignored,
            {
                "width": "input not supported by current binding",
                "height": "input not supported by current binding",
            },
        )

    def test_controlnet_tile_controls_are_not_exposed_until_backed(self) -> None:
        body = {
            "prompt": "x",
            "extensions": {
                NS: {
                    "control_image": {"multipart_field": "control_image"},
                    "controlnet_strength": 0.75,
                }
            },
        }

        result = resolve_workflow_extensions(
            body,
            self.manifest,
            supported_names=EXTENSION_INPUTS,
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.rejected,
            {
                "control_image": "unknown input",
                "controlnet_strength": "unknown input",
            },
        )

    def test_discovery_exposes_only_extension_owned_tile_controls(self) -> None:
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
            if workflow["id"] == "image.generate.sdxl-seamless-tile"
        ]

        self.assertEqual(len(workflow_targets), 1)
        self.assertEqual(set(workflow_targets[0]["controls"]), set(EXTENSION_INPUTS))


class TestSdxlSeamlessTileModelDiscovery(unittest.TestCase):
    def test_openai_models_entry_is_directly_invocable_and_path_safe(self) -> None:
        entries = proxy._build_workflow_model_entries()
        by_id = {entry["id"]: entry for entry in entries}

        self.assertIn("sdxl-seamless-tile", by_id)
        tile = by_id["sdxl-seamless-tile"]
        self.assertEqual(tile["object"], "model")
        self.assertEqual(tile["owned_by"], "vivid-fish")
        self.assertTrue(tile["vivid_fish_workflow"])
        self.assertEqual(tile["vivid_fish_workflow_id"], "image.generate.sdxl-seamless-tile")
        self.assertEqual(tile["vivid_fish_intent"], "image.generate")
        self.assertEqual(tile["vivid_fish_backend"], "comfyui")

        rendered = json.dumps(tile, sort_keys=True)
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("/media/", rendered)
        self.assertNotIn("workflows/images/sdxl-seamless-tile.json", rendered)


class TestComfyImageRunRoundErrors(unittest.IsolatedAsyncioTestCase):
    async def test_comfy_execution_error_fails_fast(self) -> None:
        class _Response:
            def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload)

            def json(self) -> dict[str, object]:
                return self._payload

        class _Client:
            async def post(self, *_args: object, **_kwargs: object) -> _Response:
                return _Response({"prompt_id": "prompt-error"})

            async def get(self, *_args: object, **_kwargs: object) -> _Response:
                return _Response(
                    {
                        "prompt-error": {
                            "status": {
                                "completed": False,
                                "status_str": "error",
                                "messages": [
                                    [
                                        "execution_error",
                                        {
                                            "node_id": "45",
                                            "node_type": "Image Seamless Texture",
                                            "exception_message": "0.7\n",
                                        },
                                    ]
                                ],
                            },
                            "outputs": {},
                        }
                    }
                )

        err, images = await proxy._comfy_run_round(
            _Client(),  # type: ignore[arg-type]
            {},
            {},
            "test error",
        )

        self.assertEqual(images, [])
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err.status_code, 502)
        body = json.loads(err.body.decode("utf-8"))
        self.assertIn("Image Seamless Texture node 45: 0.7", body["error"]["message"])

    async def test_timeout_cancels_prompt_and_reports_queue_position(self) -> None:
        class _Response:
            def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload)

            def json(self) -> dict[str, object]:
                return self._payload

        class _Client:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            async def post(self, url: str, *_args: object, **_kwargs: object) -> _Response:
                if url.endswith("/prompt"):
                    return _Response({"prompt_id": "prompt-slow"})
                if url.endswith("/api/jobs/prompt-slow/cancel"):
                    self.cancelled.append("prompt-slow")
                    return _Response({"cancelled": True})
                raise AssertionError(url)

            async def get(self, url: str, *_args: object, **_kwargs: object) -> _Response:
                if url.endswith("/queue"):
                    return _Response(
                        {
                            "queue_running": [],
                            "queue_pending": [
                                [1, "other"],
                                [2, "prompt-slow"],
                            ],
                        }
                    )
                return _Response({})

        client = _Client()
        with patch.object(proxy, "COMFY_TIMEOUT", 0):
            err, images = await proxy._comfy_run_round(
                client,  # type: ignore[arg-type]
                {},
                {},
                "test timeout",
            )

        self.assertEqual(images, [])
        self.assertEqual(client.cancelled, ["prompt-slow"])
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err.status_code, 504)
        body = json.loads(err.body.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "image_generation_timeout")
        self.assertEqual(body["error"]["queue_position"], 2)

    async def test_client_disconnect_cancels_prompt(self) -> None:
        class _Response:
            status_code = 200
            text = "{}"

            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def json(self) -> dict[str, object]:
                return self._payload

        class _Client:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            async def post(self, url: str, *_args: object, **_kwargs: object) -> _Response:
                if url.endswith("/prompt"):
                    return _Response({"prompt_id": "prompt-disconnect"})
                if url.endswith("/api/jobs/prompt-disconnect/cancel"):
                    self.cancelled.append("prompt-disconnect")
                    return _Response({"cancelled": True})
                raise AssertionError(url)

            async def get(self, *_args: object, **_kwargs: object) -> _Response:
                return _Response({})

        class _Request:
            async def is_disconnected(self) -> bool:
                return True

        client = _Client()
        with patch.object(proxy, "COMFY_TIMEOUT", 1):
            err, images = await proxy._comfy_run_round(
                client,  # type: ignore[arg-type]
                {},
                {},
                "test disconnect",
                request=_Request(),  # type: ignore[arg-type]
            )

        self.assertEqual(images, [])
        self.assertEqual(client.cancelled, ["prompt-disconnect"])
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err.status_code, 499)
        body = json.loads(err.body.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "image_client_disconnected")

    async def test_task_cancellation_cancels_prompt_and_reraises(self) -> None:
        class _Response:
            status_code = 200
            text = "{}"

            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def json(self) -> dict[str, object]:
                return self._payload

        class _Client:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            async def post(self, url: str, *_args: object, **_kwargs: object) -> _Response:
                if url.endswith("/prompt"):
                    return _Response({"prompt_id": "prompt-cancelled"})
                if url.endswith("/api/jobs/prompt-cancelled/cancel"):
                    self.cancelled.append("prompt-cancelled")
                    return _Response({"cancelled": True})
                raise AssertionError(url)

            async def get(self, *_args: object, **_kwargs: object) -> _Response:
                return _Response({})

        async def raise_cancelled(_delay: float) -> None:
            raise proxy.asyncio.CancelledError()

        client = _Client()
        with patch.object(proxy, "COMFY_TIMEOUT", 1), patch.object(proxy.asyncio, "sleep", raise_cancelled):
            with self.assertRaises(proxy.asyncio.CancelledError):
                await proxy._comfy_run_round(
                    client,  # type: ignore[arg-type]
                    {},
                    {},
                    "test task cancellation",
                )

        self.assertEqual(client.cancelled, ["prompt-cancelled"])


class TestComfyExecutionErrorParsing(unittest.TestCase):
    def test_incomplete_execution_error_is_terminal_failure(self) -> None:
        job = {
            "status": {
                "completed": False,
                "messages": [
                    [
                        "execution_error",
                        {
                            "node_id": "45",
                            "node_type": "Image Seamless Texture",
                            "exception_message": "0.7\n",
                        },
                    ]
                ],
            },
            "outputs": {},
        }

        self.assertEqual(
            proxy._comfy_execution_error_message(job),
            "ComfyUI Image Seamless Texture node 45: 0.7",
        )
        self.assertEqual(
            proxy._history_terminal_state(job),
            ("failed", "ComfyUI Image Seamless Texture node 45: 0.7"),
        )
        self.assertEqual(proxy._job_status_from_history(job), ("failed", 100))

    def test_error_status_without_payload_is_terminal_failure(self) -> None:
        job = {
            "status": {
                "completed": False,
                "status_str": "error",
                "messages": [],
            },
            "outputs": {},
        }

        self.assertEqual(proxy._comfy_execution_error_message(job), "ComfyUI execution failed")
        self.assertEqual(proxy._history_terminal_state(job), ("failed", "ComfyUI execution failed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
