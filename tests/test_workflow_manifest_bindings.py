"""Standalone tests for workflow manifest validation and backend bindings."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    BUNDLED_PROVENANCE,
    BackendBindingResult,
    ManifestError,
    apply_backend_binding,
    bind_backend_request,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)
from gateway.receipts import RouteReceipt, record_validation_result  # noqa: E402


CONTROLNET_MANIFEST = {
    "schema_version": 2,
    "id": "image.edit.controlnet.example",
    "intent": "image.edit",
    "capability": "image",
    "inputs": {
        "prompt": {"type": "string", "required": True},
        "denoise": {"type": "number", "minimum": 0, "maximum": 1},
        "controlnet_strength": {"type": "number", "minimum": 0, "maximum": 2},
    },
    "outputs": {"image": {"media_type": "image/png"}},
    "bindings": {
        "comfyui": {
            "workflow": "workflows/images/example-controlnet.json",
            "map": {
                "prompt": [{"node": "6", "input": "text"}],
                "denoise": [{"node": "31", "input": "denoise"}],
                "controlnet_strength": [{"node": "44", "input": "strength"}],
            },
        }
    },
}


class TestWorkflowManifestBindings(unittest.TestCase):
    def test_validated_inputs_apply_to_comfyui_binding(self) -> None:
        workflow = {
            "6": {"inputs": {"text": "old"}},
            "31": {"inputs": {"denoise": 0.75}},
            "44": {"inputs": {"strength": 0.5}},
        }
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {
                "prompt": "underwater rock arch",
                "denoise": 0.35,
                "controlnet_strength": 1.25,
            },
        )
        self.assertTrue(result.ok)

        mutated = apply_backend_binding(
            CONTROLNET_MANIFEST,
            "comfyui",
            workflow,
            result.accepted,
        )

        self.assertEqual(workflow, original)
        self.assertEqual(mutated["6"]["inputs"]["text"], "underwater rock arch")
        self.assertEqual(mutated["31"]["inputs"]["denoise"], 0.35)
        self.assertEqual(mutated["44"]["inputs"]["strength"], 1.25)

    def test_unknown_and_out_of_range_inputs_are_rejected(self) -> None:
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {
                "prompt": "underwater rock arch",
                "denoise": 1.7,
                "passthrough_denoise": 0.2,
            },
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_validation_result(receipt, result)

        self.assertEqual(receipt.accepted_inputs, ["prompt"])
        self.assertEqual(
            receipt.rejected_inputs,
            {
                "denoise": "denoise must be <= 1",
                "passthrough_denoise": "unknown input",
            },
        )

    def test_sample_flux_manifest_binds_to_committed_workflow_shape(self) -> None:
        manifest = load_workflow_manifest(
            ROOT / "manifests/workflows/flux2-klein-4b-edit.yml"
        )
        identity = manifest_identity(manifest, BUNDLED_PROVENANCE)
        self.assertEqual(identity.id, "image.edit.flux2-klein-4b")
        self.assertEqual(identity.version, "0.3.0")
        self.assertTrue(identity.trusted)
        self.assertEqual(len(identity.manifest_hash), 64)
        workflow = json.loads(
            (ROOT / "workflows/images/flux2-klein-4b-edit.json").read_text(encoding="utf-8")
        )

        result = validate_inputs(
            manifest,
            {
                "prompt": "matte watercolor rock arch",
                "source_image": "api-input.png",
                "width": 896,
                "height": 768,
                "steps": 8,
                "guidance": 3.5,
                "denoise": 0.42,
            },
        )
        self.assertTrue(result.ok)

        mutated = apply_backend_binding(manifest, "comfyui", workflow, result.accepted)
        self.assertEqual(mutated["6"]["inputs"]["text"], "matte watercolor rock arch")
        self.assertEqual(mutated["10"]["inputs"]["image"], "api-input.png")
        self.assertEqual(mutated["48"]["inputs"]["width"], 896)
        self.assertEqual(mutated["47"]["inputs"]["width"], 896)
        self.assertEqual(mutated["48"]["inputs"]["height"], 768)
        self.assertEqual(mutated["47"]["inputs"]["height"], 768)
        self.assertEqual(mutated["48"]["inputs"]["steps"], 8)
        self.assertEqual(mutated["26"]["inputs"]["guidance"], 3.5)
        self.assertEqual(mutated["49"]["inputs"]["denoise"], 0.42)

    def test_missing_binding_target_fails_fast(self) -> None:
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "x", "denoise": 0.5},
        )
        with self.assertRaises(ManifestError):
            apply_backend_binding(
                CONTROLNET_MANIFEST,
                "comfyui",
                {"6": {"inputs": {}}},
                result.accepted,
            )


class TestBackendBindingResult(unittest.TestCase):
    def _manifest_with_unmapped_input(self) -> dict[str, object]:
        manifest = copy.deepcopy(CONTROLNET_MANIFEST)
        manifest["inputs"]["seed"] = {"type": "integer", "minimum": 0}
        return manifest

    def test_accepted_inputs_without_binding_become_ignored(self) -> None:
        manifest = self._manifest_with_unmapped_input()
        workflow = {
            "6": {"inputs": {"text": ""}},
            "31": {"inputs": {"denoise": 0.0}},
            "44": {"inputs": {"strength": 0.0}},
        }
        result = validate_inputs(
            manifest,
            {
                "prompt": "x",
                "denoise": 0.4,
                "controlnet_strength": 1.0,
                "seed": 42,
            },
        )
        self.assertTrue(result.ok)

        bound = bind_backend_request(manifest, "comfyui", workflow, result.accepted)

        self.assertIsInstance(bound, BackendBindingResult)
        self.assertEqual(
            sorted(bound.applied_inputs),
            ["controlnet_strength", "denoise", "prompt"],
        )
        self.assertIn("seed", bound.ignored_inputs)
        self.assertIn("no mapping", bound.ignored_inputs["seed"])
        # Mapped inputs still land on the request.
        self.assertEqual(bound.request["6"]["inputs"]["text"], "x")
        self.assertEqual(bound.request["31"]["inputs"]["denoise"], 0.4)

    def test_empty_mapping_list_is_ignored_not_silent(self) -> None:
        manifest = copy.deepcopy(CONTROLNET_MANIFEST)
        manifest["bindings"]["comfyui"]["map"]["denoise"] = []
        workflow = {"6": {"inputs": {"text": ""}}, "44": {"inputs": {"strength": 0.0}}}

        result = validate_inputs(
            manifest,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        bound = bind_backend_request(manifest, "comfyui", workflow, result.accepted)

        self.assertIn("denoise", bound.ignored_inputs)
        self.assertEqual(
            bound.ignored_inputs["denoise"],
            "empty mapping for input under backend comfyui",
        )
        self.assertNotIn("denoise", bound.applied_inputs)

    def test_malformed_map_entry_fails_fast(self) -> None:
        manifest = copy.deepcopy(CONTROLNET_MANIFEST)
        manifest["bindings"]["comfyui"]["map"]["denoise"] = "not-a-list"
        result = validate_inputs(
            manifest,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        with self.assertRaises(ManifestError):
            bind_backend_request(manifest, "comfyui", {"6": {"inputs": {}}}, result.accepted)

    def test_node_only_mapping_fails_fast(self) -> None:
        manifest = copy.deepcopy(CONTROLNET_MANIFEST)
        manifest["bindings"]["comfyui"]["map"]["denoise"] = [{"node": "31"}]
        result = validate_inputs(
            manifest,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        with self.assertRaises(ManifestError):
            bind_backend_request(
                manifest,
                "comfyui",
                {"6": {"inputs": {"text": ""}}, "31": {"inputs": {}}, "44": {"inputs": {}}},
                result.accepted,
            )

    def test_non_string_path_fails_fast(self) -> None:
        manifest = copy.deepcopy(CONTROLNET_MANIFEST)
        manifest["bindings"]["comfyui"]["map"]["denoise"] = [{"path": ["inputs", "denoise"]}]
        result = validate_inputs(
            manifest,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        with self.assertRaises(ManifestError):
            bind_backend_request(
                manifest,
                "comfyui",
                {"6": {"inputs": {"text": ""}}, "44": {"inputs": {}}},
                result.accepted,
            )

    def test_generic_dotted_path_mapping(self) -> None:
        manifest = {
            "schema_version": 2,
            "id": "chat.generate.example",
            "intent": "chat.generate",
            "capability": "chat",
            "inputs": {
                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                "model": {"type": "string"},
            },
            "outputs": {"text": {"media_type": "text/plain"}},
            "bindings": {
                "llamacpp": {
                    "endpoint": "/v1/chat/completions",
                    "map": {
                        "temperature": [{"path": "sampling.temperature"}],
                        "model": [{"path": "model"}],
                    },
                }
            },
        }
        request = {"sampling": {"top_p": 0.9}, "model": "default"}
        result = validate_inputs(manifest, {"temperature": 0.7, "model": "qwen3"})
        bound = bind_backend_request(manifest, "llamacpp", request, result.accepted)

        self.assertEqual(bound.request["sampling"]["temperature"], 0.7)
        self.assertEqual(bound.request["sampling"]["top_p"], 0.9)
        self.assertEqual(bound.request["model"], "qwen3")
        self.assertEqual(sorted(bound.applied_inputs), ["model", "temperature"])
        self.assertEqual(bound.backend_metadata, {"endpoint": "/v1/chat/completions"})

    def test_identity_attached_to_binding_result(self) -> None:
        workflow = {
            "6": {"inputs": {"text": ""}},
            "31": {"inputs": {"denoise": 0.0}},
            "44": {"inputs": {"strength": 0.0}},
        }
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        bound = bind_backend_request(CONTROLNET_MANIFEST, "comfyui", workflow, result.accepted)

        self.assertEqual(bound.identity.id, "image.edit.controlnet.example")
        self.assertEqual(len(bound.identity.manifest_hash), 64)
        # Identity hash matches the standalone helper.
        self.assertEqual(
            bound.identity.manifest_hash,
            manifest_identity(CONTROLNET_MANIFEST).manifest_hash,
        )
        # Backend metadata excludes the raw map payload.
        self.assertNotIn("map", bound.backend_metadata)
        self.assertEqual(
            bound.backend_metadata["workflow"], "workflows/images/example-controlnet.json"
        )

    def test_receipt_provenance_attaches_to_route_receipt(self) -> None:
        workflow = {
            "6": {"inputs": {"text": ""}},
            "31": {"inputs": {"denoise": 0.0}},
            "44": {"inputs": {"strength": 0.0}},
        }
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        bound = bind_backend_request(CONTROLNET_MANIFEST, "comfyui", workflow, result.accepted)

        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_validation_result(receipt, result)
        provenance = bound.receipt_provenance()
        receipt.backend = provenance["backend"]
        receipt.attach_manifest(
            manifest_id=provenance["manifest_id"],
            manifest_version=provenance["manifest_version"],
            manifest_hash=provenance["manifest_hash"],
        )
        for name, reason in provenance["ignored_inputs"].items():
            receipt.ignore_input(name, reason)

        self.assertEqual(receipt.backend, "comfyui")
        self.assertEqual(receipt.manifest_id, "image.edit.controlnet.example")
        self.assertEqual(receipt.manifest_hash, bound.identity.manifest_hash)
        # Provenance must not leak the request body or the binding map.
        self.assertNotIn("request", provenance)
        self.assertNotIn("map", provenance.get("backend_metadata", {}))

    def test_source_workflow_is_not_mutated(self) -> None:
        workflow = {
            "6": {"inputs": {"text": "before"}},
            "31": {"inputs": {"denoise": 0.0}},
            "44": {"inputs": {"strength": 0.0}},
        }
        original = copy.deepcopy(workflow)
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "after", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        bound = bind_backend_request(CONTROLNET_MANIFEST, "comfyui", workflow, result.accepted)

        self.assertEqual(workflow, original)
        self.assertEqual(bound.request["6"]["inputs"]["text"], "after")

    def test_apply_backend_binding_remains_backward_compatible(self) -> None:
        workflow = {
            "6": {"inputs": {"text": ""}},
            "31": {"inputs": {"denoise": 0.0}},
            "44": {"inputs": {"strength": 0.0}},
        }
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "x", "denoise": 0.5, "controlnet_strength": 1.0},
        )
        mutated = apply_backend_binding(
            CONTROLNET_MANIFEST, "comfyui", workflow, result.accepted
        )
        # Returns the plain request dict, same as before.
        self.assertIsInstance(mutated, dict)
        self.assertEqual(mutated["31"]["inputs"]["denoise"], 0.5)


class TestShippedWorkflowOutputMediaTypes(unittest.TestCase):
    # ComfyUI writer defaults; SaveVideo fixes its container only when its format is mp4.
    WRITER_MEDIA_TYPES = {
        "SaveImage": "image/png",
        "SaveAudio": "audio/flac",
        "SaveAudioMP3": "audio/mpeg",
        "SaveAudioOpus": "audio/ogg",
        "SaveVideo": "video/mp4",
    }

    @classmethod
    def _output_rows(cls):
        """One row per (manifest, binding, output): every declared output of every binding, never skipped silently.

        A row is (manifest file, binding, output, writer or why there is none, declared type, written type or None)."""
        manifest_root = ROOT / "manifests/workflows"
        paths = sorted([*manifest_root.rglob("*.yml"), *manifest_root.rglob("*.yaml")])
        if not paths:
            raise AssertionError("no shipped workflow manifests found")
        for path in paths:
            manifest = load_workflow_manifest(path)
            filename = path.relative_to(manifest_root).as_posix()
            for binding_name, binding in manifest.get("bindings", {}).items():
                graph = json.loads((ROOT / binding["workflow"]).read_text(encoding="utf-8")) if binding.get("workflow") else None
                selectors = binding.get("selectors", {})
                for output_key, output in manifest["outputs"].items():
                    selector = selectors.get(output_key)
                    if graph is None:
                        writer = "<no graph workflow>"
                    elif selector is None or "node" not in selector:
                        writer = "<no node selector>"
                    else:
                        writer = graph[str(selector["node"])]["class_type"]
                    written = cls.WRITER_MEDIA_TYPES.get(writer)
                    if writer == "SaveVideo" and graph[str(selector["node"])].get("inputs", {}).get("format") != "mp4":
                        written = None
                    yield filename, binding_name, output_key, writer, output["media_type"], written

    # Every output the check cannot compare, named one by one: a writer that fixes no media type (SaveVideo with
    # format "auto" keeps its input container; TangoFlux writes through its own node), or a binding with no graph
    # writer at all (the gateway or an HTTP engine makes the output). A new unchecked output fails until it is added here.
    UNCHECKED_OUTPUTS = {
        ("face-likeness.yml", "gateway", "report", "<no graph workflow>"),
        ("flux2-klein-4b-seamless-tile.yml", "gateway", "image", "<no graph workflow>"),
        ("mesh.generate.trellis-2.yml", "http-multipart", "mesh", "<no graph workflow>"),
        ("rife-interpolate.yml", "comfyui", "video", "SaveVideo"),
        ("tangoflux.yml", "comfyui", "audio", "TangoFluxVAEDecodeAndPlay"),
        ("video-mux.yml", "comfyui", "video", "SaveVideo"),
    }

    def test_declared_output_media_types_match_known_writers(self) -> None:
        checked = 0
        unchecked = set()
        for filename, binding, key, writer, declared, written in self._output_rows():
            if written is None:
                unchecked.add((filename, binding, key, writer))
                continue
            checked += 1
            with self.subTest(manifest=filename, binding=binding, output=key, writer=writer):
                self.assertEqual(declared, written)
        self.assertGreaterEqual(checked, 30, "fewer known output writers checked than shipped")
        self.assertEqual(unchecked, self.UNCHECKED_OUTPUTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
