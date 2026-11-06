"""Tests for the workflow registry / discovery summary layer."""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.discovery import (  # noqa: E402
    WorkflowRegistry,
    summarize_catalog,
    summarize_workflow,
)
from gateway.manifests import ManifestError  # noqa: E402


VALID_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    min_gateway_version: "0.1"
    version: "0.1.0"
    id: image.edit.controlnet.example
    intent: image.edit
    display_name: Example Controlnet Edit
    description: Test workflow used to exercise the registry.
    source: local
    trusted: true
    inputs:
      prompt:
        type: string
        required: true
        description: Text prompt for the edit.
      denoise:
        type: number
        minimum: 0
        maximum: 1
        default: 0.5
      style:
        type: string
        enum: [watercolor, oil]
    outputs:
      image:
        media_type: image/png
        description: Edited image.
    bindings:
      comfyui:
        workflow: workflows/images/example-controlnet.json
        map:
          prompt:
            - node: "6"
              input: text
          denoise:
            - node: "31"
              input: denoise
    """
).strip()


SECOND_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    version: "0.2.0"
    id: image.generate.simple.example
    intent: image.generate
    display_name: Simple Generate
    source: local
    trusted: false
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/simple.json
        map:
          prompt:
            - node: "6"
              input: text
      a1111:
        endpoint: /sdapi/v1/txt2img
        map:
          prompt:
            - path: prompt
    """
).strip()


MALFORMED_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    id: image.edit.broken
    intent: image.edit
    inputs: this-is-not-a-mapping
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/broken.json
        map: {}
    """
).strip()


class WorkflowRegistryTests(unittest.TestCase):
    def _build_registry(self, tmp: Path) -> WorkflowRegistry:
        (tmp / "controlnet.yml").write_text(VALID_MANIFEST_YAML, encoding="utf-8")
        (tmp / "simple.yaml").write_text(SECOND_MANIFEST_YAML, encoding="utf-8")
        return WorkflowRegistry.from_directory(tmp)

    def test_loads_and_summarizes_valid_manifests(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)

            self.assertEqual(
                registry.ids(),
                ["image.edit.controlnet.example", "image.generate.simple.example"],
            )
            self.assertEqual(registry.errors, [])

            record = registry.get("image.edit.controlnet.example")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.intent, "image.edit")
            self.assertEqual(record.supported_backends, ["comfyui"])
            self.assertEqual(len(record.identity.manifest_hash), 64)

            by_intent = registry.by_intent("image.edit")
            self.assertEqual([r.id for r in by_intent], [record.id])

    def test_malformed_manifest_does_not_abort_directory_load(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "good.yml").write_text(VALID_MANIFEST_YAML, encoding="utf-8")
            (tmp / "broken.yml").write_text(MALFORMED_MANIFEST_YAML, encoding="utf-8")
            registry = WorkflowRegistry.from_directory(tmp)

            self.assertEqual(registry.ids(), ["image.edit.controlnet.example"])
            self.assertEqual(len(registry.errors), 1)
            err = registry.errors[0]
            self.assertEqual(err.source_path.name, "broken.yml")
            self.assertIn("inputs", err.message)

    def test_missing_directory_fails_fast(self) -> None:
        with self.assertRaises(ManifestError):
            WorkflowRegistry.from_directory("/nonexistent/manifest/dir/xyz")

    def test_duplicate_id_fails_fast(self) -> None:
        registry = WorkflowRegistry()
        import yaml

        manifest = yaml.safe_load(VALID_MANIFEST_YAML)
        registry.add_manifest(manifest, source_path="a.yml")
        with self.assertRaises(ManifestError):
            registry.add_manifest(manifest, source_path="b.yml")

    def test_client_summary_redacts_provenance_and_backend_internals(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)
            client = registry.summarize_workflow(
                "image.edit.controlnet.example", audience="client"
            )
            self.assertIsNotNone(client)
            assert client is not None

            # Portable contract fields present
            self.assertEqual(client["id"], "image.edit.controlnet.example")
            self.assertEqual(client["intent"], "image.edit")
            self.assertEqual(client["version"], "0.1.0")
            self.assertEqual(client["display_name"], "Example Controlnet Edit")
            self.assertEqual(client["supported_backends"], ["comfyui"])

            # Inputs preserve portable validation hints but nothing else
            input_names = {i["name"] for i in client["inputs"]}
            self.assertEqual(input_names, {"prompt", "denoise", "style"})
            denoise = next(i for i in client["inputs"] if i["name"] == "denoise")
            self.assertEqual(denoise["type"], "number")
            self.assertEqual(denoise["minimum"], 0)
            self.assertEqual(denoise["maximum"], 1)
            self.assertEqual(denoise["default"], 0.5)
            style = next(i for i in client["inputs"] if i["name"] == "style")
            self.assertEqual(style["enum"], ["watercolor", "oil"])

            # Outputs expose media_type only
            self.assertEqual(
                client["outputs"],
                [
                    {
                        "name": "image",
                        "media_type": "image/png",
                        "description": "Edited image.",
                    }
                ],
            )

            # No backend file paths, manifest hash, source path, bindings, or
            # ComfyUI node IDs anywhere in the client payload.
            forbidden_keys = {
                "manifest_hash",
                "source",
                "source_path",
                "trusted",
                "bindings",
                "min_gateway_version",
            }
            self.assertEqual(forbidden_keys & set(client), set())

            serialized = repr(client)
            self.assertNotIn("workflows/images/example-controlnet.json", serialized)
            self.assertNotIn("node", serialized)

    def test_admin_summary_includes_identity_and_binding_provenance(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)
            admin = registry.summarize_workflow(
                "image.edit.controlnet.example", audience="admin"
            )
            assert admin is not None

            self.assertEqual(len(admin["manifest_hash"]), 64)
            self.assertEqual(admin["source"], "local")
            self.assertTrue(admin["trusted"])
            self.assertEqual(admin["min_gateway_version"], "0.1")
            self.assertTrue(admin["source_path"].endswith("controlnet.yml"))

            bindings = admin["bindings"]
            self.assertEqual(len(bindings), 1)
            comfy = bindings[0]
            self.assertEqual(comfy["backend"], "comfyui")
            self.assertEqual(
                comfy["workflow_artifact"],
                "workflows/images/example-controlnet.json",
            )
            self.assertEqual(sorted(comfy["mapped_inputs"]), ["denoise", "prompt"])
            # Admin does not get the raw node-level map.
            self.assertNotIn("map", comfy)

    def test_internal_summary_includes_raw_binding_map(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)
            internal = registry.summarize_workflow(
                "image.edit.controlnet.example", audience="internal"
            )
            assert internal is not None

            comfy = internal["bindings"][0]
            self.assertEqual(comfy["map"]["prompt"][0]["node"], "6")
            self.assertEqual(comfy["map"]["denoise"][0]["input"], "denoise")

    def test_catalog_summary_lists_all_workflows(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)
            catalog = registry.summarize_catalog(audience="client")
            self.assertEqual(catalog["schema_version"], 1)
            self.assertEqual(catalog["audience"], "client")
            ids = [w["id"] for w in catalog["workflows"]]
            self.assertEqual(
                ids,
                [
                    "image.edit.controlnet.example",
                    "image.generate.simple.example",
                ],
            )
            # Multi-backend manifest reports both backend names but no paths.
            simple = next(
                w
                for w in catalog["workflows"]
                if w["id"] == "image.generate.simple.example"
            )
            self.assertEqual(
                simple["supported_backends"], ["a1111", "comfyui"]
            )
            self.assertNotIn("bindings", simple)

    def test_committed_sample_manifest_loads_via_registry(self) -> None:
        registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        self.assertIn("image.edit.flux2-klein-4b", registry.ids())
        record = registry.get("image.edit.flux2-klein-4b")
        assert record is not None
        client = summarize_workflow(record, audience="client")
        self.assertEqual(client["intent"], "image.edit")
        self.assertEqual(client["supported_backends"], ["comfyui"])
        # Client summary must not leak the committed workflow JSON path.
        self.assertNotIn(
            "workflows/images/flux2-klein-4b-edit.json", repr(client)
        )

        admin = summarize_workflow(record, audience="admin")
        self.assertEqual(
            admin["bindings"][0]["workflow_artifact"],
            "workflows/images/flux2-klein-4b-edit.json",
        )
        self.assertTrue(admin["trusted"])
        self.assertEqual(len(admin["manifest_hash"]), 64)

    def test_audio_video_configured_workflows_have_manifests(self) -> None:
        config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
        registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        workflow_artifacts = {
            binding["workflow"]
            for record in registry.records()
            for binding in (record.manifest.get("bindings") or {}).values()
            if "workflow" in binding
        }

        for section in ("audio", "videos"):
            with self.subTest(section=section):
                configured = {
                    workflow["file"]
                    for workflow in config[section]["workflows"].values()
                }
                self.assertTrue(configured)
                self.assertLessEqual(configured, workflow_artifacts)

    def test_unknown_audience_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            registry = self._build_registry(tmp)
            with self.assertRaises(ValueError):
                registry.summarize_catalog(audience="public")

    def test_summarize_unknown_workflow_returns_none(self) -> None:
        registry = WorkflowRegistry()
        self.assertIsNone(registry.summarize_workflow("missing"))

    def test_summarize_catalog_direct_helper(self) -> None:
        registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        catalog = summarize_catalog(registry.records(), audience="internal")
        self.assertEqual(catalog["audience"], "internal")
        self.assertTrue(catalog["workflows"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
