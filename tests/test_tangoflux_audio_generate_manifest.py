"""Tests for the TangoFlux audio.generate manifest binding.

Pure tests — no subprocess, no network, no ComfyUI. They prove the
audio.generate workflow-extension contract mirrors the image.generate
contract:

- The manifest validates schema_version/id/intent/inputs/outputs/bindings.
- Validated inputs land on the documented ComfyUI nodes in a deep copy
  of the committed workflow JSON (source workflow is not mutated).
- Required inputs are enforced; out-of-range values are rejected.
- Unknown ComfyUI inputs would raise ManifestError (binding-shape guard).
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    ManifestError,
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)
from gateway.audio_extensions import (  # noqa: E402
    STANDARD_AUDIO_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_audio_workflow_extensions,
    detect_unknown_audio_fields,
)
import proxy  # noqa: E402


MANIFEST_PATH = ROOT / "manifests/workflows/tangoflux.yml"
WORKFLOW_PATH = ROOT / "workflows/audio/tangoflux.json"


def _load_workflow() -> dict[str, Any]:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _ace_workflow() -> dict[str, Any]:
    return {
        "_meta": {
            "prompt_node": [{"node": "5", "key": "tags"}],
            "tags_node": [{"node": "5", "key": "tags"}],
            "lyrics_node": [{"node": "5", "key": "lyrics"}],
            "duration_node": [
                {"node": "10", "key": "seconds"},
                {"node": "5", "key": "duration"},
            ],
            "seed_node": [{"node": "20", "key": "seed"}],
        },
        "5": {
            "class_type": "TextEncodeAceStepAudio1.5",
            "inputs": {
                "tags": "",
                "lyrics": "[instrumental]",
                "duration": 0,
            },
        },
        "10": {"class_type": "PrimitiveFloat", "inputs": {"seconds": 0}},
        "20": {"class_type": "PrimitiveInt", "inputs": {"seed": 0}},
    }


class _JsonRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _FakeHTTPResponse:
    def __init__(
        self,
        status_code: int,
        *,
        json_data: dict[str, Any] | None = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._json_data


class _FakeAudioClient:
    def __init__(self, captured: dict[str, Any]) -> None:
        self.captured = captured

    async def __aenter__(self) -> "_FakeAudioClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        self.captured["post_url"] = url
        self.captured["payload"] = kwargs.get("json")
        return _FakeHTTPResponse(200, json_data={"prompt_id": "audio-prompt"})

    async def get(self, url: str, **_kwargs: Any) -> _FakeHTTPResponse:
        if "/history/" in url:
            return _FakeHTTPResponse(
                200,
                json_data={
                    "audio-prompt": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "38": {
                                "audios": [
                                    {"filename": "out.wav", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        return _FakeHTTPResponse(
            200,
            content=b"RIFFfake-wave",
            headers={"content-type": "audio/wav"},
        )


class TestManifestShape(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_identity(self) -> None:
        identity = manifest_identity(self.manifest)
        self.assertEqual(identity.id, "audio.generate.tangoflux")
        self.assertEqual(identity.version, "0.1.0")
        self.assertTrue(identity.trusted)
        self.assertEqual(len(identity.manifest_hash), 64)

    def test_intent_is_audio_generate(self) -> None:
        self.assertEqual(self.manifest["intent"], "audio.generate")

    def test_inputs_cover_workflow_controls(self) -> None:
        self.assertEqual(
            set(self.manifest["inputs"]),
            {"prompt", "duration", "steps", "guidance", "seed", "batch_size"},
        )

    def test_output_is_audio_wav(self) -> None:
        self.assertEqual(self.manifest["outputs"]["audio"]["media_type"], "audio/wav")

    def test_binding_targets_are_well_formed(self) -> None:
        mapping = self.manifest["bindings"]["comfyui"]["map"]
        self.assertEqual(set(mapping.keys()), set(self.manifest["inputs"].keys()))
        for entries in mapping.values():
            self.assertIsInstance(entries, list)
            self.assertGreater(len(entries), 0)
            for entry in entries:
                self.assertIn("node", entry)
                self.assertIn("input", entry)


class TestManifestBindsToCommittedWorkflow(unittest.TestCase):
    """Validated inputs land at the documented TangoFluxSampler node."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_all_inputs_apply_to_sampler_node(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            self.manifest,
            {
                "prompt": "a soft rainfall on a tin roof",
                "duration": 8.0,
                "steps": 40,
                "guidance": 3.5,
                "seed": 4242,
                "batch_size": 1,
            },
        )
        self.assertTrue(result.ok, result.rejected)

        mutated = apply_backend_binding(
            self.manifest, "comfyui", workflow, result.accepted
        )

        # Source workflow not mutated.
        self.assertEqual(workflow, original)
        # All inputs land on the TangoFluxSampler (node 2).
        node = mutated["2"]["inputs"]
        self.assertEqual(node["prompt"], "a soft rainfall on a tin roof")
        self.assertEqual(node["duration"], 8.0)
        self.assertEqual(node["steps"], 40)
        self.assertEqual(node["guidance_scale"], 3.5)
        self.assertEqual(node["seed"], 4242)
        self.assertEqual(node["batch_size"], 1)

    def test_prompt_is_required(self) -> None:
        result = validate_inputs(self.manifest, {"duration": 5.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.rejected.get("prompt"), "required input missing")

    def test_out_of_range_inputs_are_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {
                "prompt": "x",
                "duration": 999,
                "steps": 0,
                "guidance": 99.0,
                "batch_size": 0,
            },
        )
        self.assertIn("duration", result.rejected)
        self.assertIn("steps", result.rejected)
        self.assertIn("guidance", result.rejected)
        self.assertIn("batch_size", result.rejected)

    def test_wrong_type_is_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {"prompt": "x", "duration": "ten", "seed": True},
        )
        self.assertIn("duration", result.rejected)
        # Booleans must not satisfy integer typing.
        self.assertIn("seed", result.rejected)

    def test_unknown_input_is_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {"prompt": "x", "denoise": 0.4, "controlnet_strength": 1.0},
        )
        self.assertIn("denoise", result.rejected)
        self.assertIn("controlnet_strength", result.rejected)
        for reason in (
            result.rejected["denoise"],
            result.rejected["controlnet_strength"],
        ):
            self.assertEqual(reason, "unknown input")


class TestBindingShapeGuards(unittest.TestCase):
    """Manifest binding shape must fail fast for missing nodes/inputs."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_missing_target_node_fails_fast(self) -> None:
        workflow = _load_workflow()
        del workflow["2"]
        result = validate_inputs(self.manifest, {"prompt": "x"})
        with self.assertRaises(ManifestError):
            apply_backend_binding(self.manifest, "comfyui", workflow, result.accepted)


class TestAudioGenerateWorkflowExtensions(unittest.TestCase):
    """Strict namespaced extension binding for POST /v1/audio/generate."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_standard_audio_fields_allow_extensions_envelope(self) -> None:
        self.assertIn("extensions", STANDARD_AUDIO_GENERATE_FIELDS)
        self.assertIn("tags", STANDARD_AUDIO_GENERATE_FIELDS)
        self.assertIn("lyrics", STANDARD_AUDIO_GENERATE_FIELDS)
        for top_level_control in ("steps", "guidance", "batch_size"):
            self.assertNotIn(top_level_control, STANDARD_AUDIO_GENERATE_FIELDS)

    def test_top_level_tags_and_lyrics_are_standard_route_fields(self) -> None:
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "short analog synth riser",
                "tags": "ignored when prompt is present on TangoFlux",
                "lyrics": "ignored by TangoFlux",
                "extensions": {WORKFLOW_EXTENSION_NAMESPACE: {"steps": 30}},
            },
            self.manifest,
            "comfyui",
            _load_workflow(),
        )

        self.assertEqual(result.unknown_top_level, {})
        self.assertTrue(result.ok, result.validation.rejected)
        node = result.request["2"]["inputs"]
        self.assertNotIn("tags", node)
        self.assertNotIn("lyrics", node)

    def test_namespaced_tags_and_lyrics_are_route_managed(self) -> None:
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "x",
                "extensions": {
                    WORKFLOW_EXTENSION_NAMESPACE: {
                        "tags": "do not bind as extension",
                        "lyrics": "do not bind as extension",
                    }
                },
            },
            self.manifest,
            "comfyui",
            _load_workflow(),
        )

        self.assertEqual(set(result.validation.rejected), {"tags", "lyrics"})
        self.assertFalse(result.ok)

    def test_namespaced_controls_validate_and_bind_to_tangoflux(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)
        body = {
            "model": "tangoflux",
            "prompt": "standard prompt",
            "duration": 3.0,
            "extensions": {
                WORKFLOW_EXTENSION_NAMESPACE: {
                    "steps": 45,
                    "guidance": 4.25,
                    "batch_size": 2,
                }
            },
        }

        result = bind_audio_workflow_extensions(
            body,
            self.manifest,
            "comfyui",
            workflow,
        )

        self.assertTrue(result.ok, result.validation.rejected)
        self.assertEqual(workflow, original)
        node = result.request["2"]["inputs"]
        self.assertEqual(node["steps"], 45)
        self.assertEqual(node["guidance_scale"], 4.25)
        self.assertEqual(node["batch_size"], 2)

    def test_route_managed_namespaced_audio_fields_are_rejected(self) -> None:
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "x",
                "extensions": {
                    WORKFLOW_EXTENSION_NAMESPACE: {
                        "duration": 12,
                        "seed": 987,
                    }
                },
            },
            self.manifest,
            "comfyui",
            _load_workflow(),
        )

        self.assertEqual(
            set(result.validation.rejected),
            {"duration", "seed"},
        )
        self.assertFalse(result.ok)

    def test_unknown_top_level_workflow_control_is_rejected(self) -> None:
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "x",
                "steps": 45,
                "extensions": {WORKFLOW_EXTENSION_NAMESPACE: {}},
            },
            self.manifest,
            "comfyui",
            _load_workflow(),
        )

        self.assertIn("steps", result.unknown_top_level)
        self.assertIn("extensions", result.unknown_top_level["steps"])
        self.assertFalse(result.ok)

    def test_unknown_namespaced_control_is_rejected(self) -> None:
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "x",
                "extensions": {
                    WORKFLOW_EXTENSION_NAMESPACE: {
                        "sampler": "euler",
                    }
                },
            },
            self.manifest,
            "comfyui",
            _load_workflow(),
        )

        self.assertEqual(result.validation.rejected, {"sampler": "unknown input"})
        self.assertFalse(result.ok)

    def test_out_of_range_namespaced_control_is_rejected_not_applied(self) -> None:
        workflow = _load_workflow()
        original_steps = workflow["2"]["inputs"]["steps"]
        result = bind_audio_workflow_extensions(
            {
                "model": "tangoflux",
                "prompt": "x",
                "extensions": {WORKFLOW_EXTENSION_NAMESPACE: {"steps": 0}},
            },
            self.manifest,
            "comfyui",
            workflow,
        )

        self.assertIn("steps", result.validation.rejected)
        self.assertEqual(result.request["2"]["inputs"]["steps"], original_steps)


class TestAceStepAudioGenerateRoute(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict[str, Any], workflow: dict[str, Any]):
        captured: dict[str, Any] = {}

        async def no_sleep(_seconds: float) -> None:
            return None

        def fake_client_factory(*_args: Any, **_kwargs: Any) -> _FakeAudioClient:
            return _FakeAudioClient(captured)

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy,
            "_resolve_audio_workflow",
            return_value=("ace-step-1.5-xl-turbo", {"file": "unused"}),
        ), patch.object(
            proxy,
            "_load_audio_workflow_file",
            return_value=workflow,
        ), patch.object(
            proxy,
            "_load_audio_workflow_manifest",
            return_value=None,
        ), patch.object(
            proxy,
            "configured_downstream_headers",
            return_value={},
        ), patch.object(
            proxy.httpx,
            "AsyncClient",
            fake_client_factory,
        ), patch.object(proxy.asyncio, "sleep", no_sleep):
            response = await proxy.proxy_audio_generate(
                _JsonRequest(body),  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
            )
        return response, captured

    async def test_tags_and_lyrics_bind_to_ace_step_inputs(self) -> None:
        response, captured = await self._call(
            {
                "model": "ace-step-1.5-xl-turbo",
                "prompt": "generic music prompt",
                "tags": "dream pop, piano, soft drums",
                "lyrics": "first line\nsecond line",
                "duration": 12,
                "seed": 1234,
            },
            _ace_workflow(),
        )

        self.assertEqual(response.status_code, 200)
        workflow = captured["payload"]["prompt"]
        self.assertEqual(workflow["5"]["inputs"]["tags"], "dream pop, piano, soft drums")
        self.assertEqual(workflow["5"]["inputs"]["lyrics"], "first line\nsecond line")
        self.assertEqual(workflow["5"]["inputs"]["duration"], 12.0)
        self.assertEqual(workflow["10"]["inputs"]["seconds"], 12.0)
        self.assertEqual(workflow["20"]["inputs"]["seed"], 1234)

    async def test_prompt_falls_back_to_ace_step_tags(self) -> None:
        response, captured = await self._call(
            {
                "model": "ace-step-1.5-xl-turbo",
                "prompt": "solo piano, intimate room",
            },
            _ace_workflow(),
        )

        self.assertEqual(response.status_code, 200)
        workflow = captured["payload"]["prompt"]
        self.assertEqual(workflow["5"]["inputs"]["tags"], "solo piano, intimate room")
        self.assertEqual(workflow["5"]["inputs"]["lyrics"], "[instrumental]")

    async def test_tags_can_supply_audio_prompt_when_prompt_is_absent(self) -> None:
        response, captured = await self._call(
            {
                "model": "ace-step-1.5-xl-turbo",
                "tags": "ambient synth pad",
            },
            _ace_workflow(),
        )

        self.assertEqual(response.status_code, 200)
        workflow = captured["payload"]["prompt"]
        self.assertEqual(workflow["5"]["inputs"]["tags"], "ambient synth pad")

    async def test_non_string_tags_and_lyrics_are_rejected(self) -> None:
        for field_name in ("tags", "lyrics"):
            body = {
                "model": "ace-step-1.5-xl-turbo",
                "prompt": "x",
                field_name: 123,
            }
            response, captured = await self._call(body, _ace_workflow())
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("payload", captured)
            error = json.loads(response.body.decode("utf-8"))["error"]
            self.assertEqual(error["param"], field_name)

    def test_unknown_audio_field_detector_accepts_tags_and_lyrics(self) -> None:
        self.assertEqual(
            detect_unknown_audio_fields(
                {
                    "model": "ace-step-1.5-xl-turbo",
                    "tags": "dream pop",
                    "lyrics": "hello",
                }
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
