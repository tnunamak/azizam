"""The native OpenAPI is typed from the workflow manifests and stays consistent
with /gateway/v1/workflows, /gateway/v1/capabilities, /llms.txt and the skill."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import proxy  # noqa: E402
from tests.support.access import authenticated  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.workflow_openapi import (  # noqa: E402
    ROOT_EXTENSION,
    augment_openapi,
    match_alias_target,
    schema_name,
    short_id,
)
from openapi_lint import validate_openapi  # noqa: E402

MANIFESTS = ROOT / "manifests" / "workflows"


class WorkflowOpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = WorkflowRegistry.from_directory(MANIFESTS)
        cls.client = TestClient(proxy.app)
        response = cls.client.get("/openapi.json")
        assert response.status_code == 200, response.text
        cls.spec = response.json()
        cls.catalog = {w["id"]: w for w in cls.spec[ROOT_EXTENSION]}

    def schema_of(self, workflow_id: str) -> dict:
        return self.spec["components"]["schemas"][schema_name(workflow_id)]

    def test_spec_is_structurally_valid(self) -> None:
        self.assertEqual(validate_openapi(self.spec), [])

    def test_manifest_directory_loads_clean(self) -> None:
        self.assertEqual(self.registry.errors, [])
        self.assertGreaterEqual(len(self.registry.records()), 24)

    def test_every_manifest_has_typed_inputs(self) -> None:
        for record in self.registry.records():
            with self.subTest(workflow=record.id):
                self.assertIn(record.id, self.catalog)
                schema = self.schema_of(record.id)
                self.assertEqual(
                    sorted(schema["properties"]), sorted(record.manifest["inputs"])
                )
                self.assertIs(schema["additionalProperties"], False)
                wanted = sorted(
                    n for n, s in record.manifest["inputs"].items() if s.get("required")
                )
                self.assertEqual(sorted(schema.get("required", [])), wanted)
                for name, spec in record.manifest["inputs"].items():
                    prop = schema["properties"][name]
                    if spec["type"] == "asset":
                        self.assertTrue(prop["x-azizam-asset"])
                        continue
                    self.assertEqual(prop["type"], spec["type"])
                    for bound in ("minimum", "maximum", "default"):
                        if bound in spec:
                            self.assertEqual(prop[bound], spec[bound])

    def test_component_names_are_codegen_safe_and_unique(self) -> None:
        names = [
            n for n in self.spec["components"]["schemas"]
            if n.startswith(("WorkflowInputs_", "WorkflowForm_"))
        ]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[A-Za-z0-9_]+$")
        inputs = [n for n in names if n.startswith("WorkflowInputs_")]
        self.assertEqual(len(inputs), len(self.registry.records()))

    def test_catalog_matches_gateway_workflows_endpoint(self) -> None:
        with authenticated():
            listing = self.client.get("/gateway/v1/workflows").json()["workflows"]
        self.assertEqual(sorted(w["id"] for w in listing), sorted(self.catalog))
        for item in listing:
            self.assertEqual(self.catalog[item["id"]]["intent"], item["intent"])

    def test_catalog_workflows_match_capability_rows(self) -> None:
        with authenticated(), \
             patch.object(proxy, "_capabilities_availability_evidence", new=AsyncMock(return_value={})):
            rows = self.client.get("/gateway/v1/capabilities").json()["capabilities"]
        names = {
            value
            for row in rows
            for value in (row.get("workflow"), row.get("model"))
            if value
        }
        for entry in self.catalog.values():
            with self.subTest(workflow=entry["id"]):
                forms = {entry["id"], entry["short_id"], "video-" + entry["short_id"]}
                self.assertTrue(forms & names, f"no capability row names {entry['id']}")
                async_mode = entry["execution"]["async"]
                if entry["intent"] in {"audio.generate", "image.generate", "video.generate"}:
                    self.assertIsNotNone(async_mode, "job capability needs an async route")

    def test_run_route_is_typed(self) -> None:
        generic = self.spec["paths"]["/gateway/v1/workflows/{workflow_id}/run"]["post"]
        self.assertIsNotNone(generic.get("requestBody"))
        self.assertIn("multipart/form-data", generic["requestBody"]["content"])
        edits = [r for r in self.registry.records() if r.intent == "video.edit"]
        self.assertTrue(edits)
        for record in edits:
            path = f"/gateway/v1/workflows/{short_id(record)}/run"
            body = self.spec["paths"][path]["post"]["requestBody"]
            form = self.spec["components"]["schemas"][schema_name(record.id, "WorkflowForm_")]
            self.assertEqual(
                body["content"]["multipart/form-data"]["schema"],
                {"$ref": "#/components/schemas/" + schema_name(record.id, "WorkflowForm_")},
            )
            for name, spec in record.manifest["inputs"].items():
                if spec["type"] == "asset":
                    self.assertEqual(form["properties"][name]["format"], "binary")

    def test_other_native_routes_are_typed(self) -> None:
        paths = self.spec["paths"]
        submit = paths["/gateway/v1/media/jobs"]["post"]
        self.assertIn("intent", submit["requestBody"]["content"]["application/json"]["schema"]["properties"])
        self.assertIn("202", submit["responses"])
        image_job = paths["/gateway/v1/media/jobs/image-generations"]["post"]
        self.assertIsNotNone(image_job["requestBody"])
        for path in ("/v1/videos/{video_id}/remix", "/openai/v1/videos/{video_id}/remix"):
            self.assertIsNotNone(paths[path]["post"]["requestBody"])
        poll = paths["/gateway/v1/media/jobs/{job_id}"]["get"]["responses"]["200"]
        self.assertIn("MediaJob", str(poll))

    def test_media_job_request_is_typed_like_the_sync_routes(self) -> None:
        schemas = self.spec["components"]["schemas"]
        submit = self.spec["paths"]["/gateway/v1/media/jobs"]["post"]
        request = submit["requestBody"]["content"]["application/json"]["schema"]["properties"]["request"]
        refs = {o["$ref"].rsplit("/", 1)[1] for o in request["anyOf"]}
        self.assertEqual(
            refs, {"ImageGenerationRequest", "AudioGenerationRequest", "VideoGenerationRequest"}
        )
        audio = schemas["AudioGenerationRequest"]["properties"]["extensions"]
        self.assertIn(
            schema_name("audio.generate.sa3-small-sfx"),
            str(audio["properties"]["com.azizam.workflow"]["anyOf"]),
        )
        image_job = self.spec["paths"]["/gateway/v1/media/jobs/image-generations"]["post"]
        self.assertEqual(
            image_job["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ImageGenerationRequest"},
        )

    def test_route_inputs_cover_what_manifests_omit(self) -> None:
        by_name = {n: {r["name"] for r in e["route_inputs"]} for n, e in self.catalog.items()}
        self.assertIn("seconds", by_name["video.generate.ltx-2.5-t2v"])
        self.assertIn("size", by_name["video.generate.ltx-2.5-t2v"])
        self.assertNotIn("size", by_name["image.generate.krea2-turbo"])  # has width/height
        self.assertIn("n", by_name["image.generate.krea2-turbo"])
        self.assertEqual(by_name["audio.generate.sa3-small-sfx"], set())

    def test_extension_blocks_are_typed_per_route(self) -> None:
        paths = self.spec["paths"]
        for path, intent in (
            ("/v1/images/generations", "image.generate"),
            ("/v1/audio/generate", "audio.generate"),
            ("/v1/videos", "video."),
        ):
            schema = paths[path]["post"]["requestBody"]["content"]["application/json"]["schema"]
            if "$ref" in schema:
                self.fail(f"{path} body should be inline")
            options = schema["properties"]["extensions"]["properties"][
                "com.azizam.workflow"
            ]["anyOf"]
            wanted = {
                schema_name(r.id)
                for r in self.registry.records()
                if r.intent.startswith(intent)
            }
            self.assertEqual({o["$ref"].rsplit("/", 1)[1] for o in options}, wanted)

    def test_sample_requests_validate(self) -> None:
        """A request built from the catalog's field map validates against the schema."""
        for record in self.registry.records():
            sample = {}
            for name, spec in record.manifest["inputs"].items():
                if spec["type"] == "asset":
                    if spec.get("required"):
                        sample[name] = "data:image/png;base64,AAAA"
                    continue
                if spec.get("required") or "default" in spec:
                    sample[name] = spec.get("default") or {
                        "string": "x", "integer": 1, "number": 1.0, "boolean": True,
                    }[spec["type"]]
            with self.subTest(workflow=record.id):
                Draft202012Validator(self.schema_of(record.id)).validate(sample)

    def test_bad_requests_are_rejected_by_schema(self) -> None:
        sfx = Draft202012Validator(self.schema_of("audio.generate.sa3-small-sfx"))
        self.assertTrue(list(sfx.iter_errors({"prompt": "x", "duration": 500})))
        self.assertTrue(list(sfx.iter_errors({"prompt": "x", "nonsense": 1})))
        self.assertTrue(list(sfx.iter_errors({"duration": 5})))  # prompt required
        self.assertFalse(list(sfx.iter_errors({"prompt": "x", "duration": 5, "seed": 3})))

    def test_execution_routes_and_async_semantics(self) -> None:
        for entry in self.catalog.values():
            with self.subTest(workflow=entry["id"]):
                execution = entry["execution"]
                self.assertIn(execution["recommended"], ("sync", "async"))
                self.assertIsNotNone(execution[execution["recommended"]])
                if execution["async"]:
                    for key in ("submit", "poll", "content", "statuses", "terminal"):
                        self.assertIn(key, execution["async"])
                if entry["intent"].startswith("video."):
                    self.assertEqual(execution["async"]["submit"]["path"], "/v1/videos")
        self.assertEqual(
            self.catalog["video.generate.ltx-2.5-t2v"]["execution"]["async"]["submit"]["model"],
            "video-ltx-2.5-t2v",
        )
        self.assertEqual(self.catalog["image.edit.qwen-image-2.1-edit"]["execution"]["async"], None)

    def test_field_mapping_follows_route_ownership(self) -> None:
        image = self.catalog["image.generate.krea2-turbo"]["execution"]["sync"]["fields"]
        self.assertEqual(image["prompt"], {"in": "body", "field": "prompt"})
        self.assertEqual(image["width"]["field"], "size")
        self.assertEqual(image["seed"], {"in": "extension", "field": "seed"})
        audio = self.catalog["audio.generate.sa3-small-sfx"]["execution"]["sync"]["fields"]
        self.assertEqual(audio["duration"], {"in": "body", "field": "duration"})
        edit = self.catalog["image.edit.qwen-image-2.1-edit-multiref"]["execution"]["sync"]["fields"]
        self.assertEqual(edit["source_image"], {"in": "file", "field": "image"})
        self.assertEqual(edit["reference_image_2"]["in"], "file+extension")

    def test_every_stable_alias_resolves_to_a_workflow(self) -> None:
        aliases = proxy._gateway_aliases_provider()
        listed = {a for e in self.catalog.values() for a in e["aliases"]}
        for operation in ("images", "image_edits", "videos", "audio_generate"):
            for entry in aliases[operation]["stable"]:
                with self.subTest(alias=entry["alias"], operation=operation):
                    self.assertIn(entry["alias"], listed)
        sfx = self.catalog["audio.generate.sa3-small-sfx"]
        self.assertIn("sfx", sfx["aliases"])

    def test_typical_seconds_flow_from_manifest_to_both_surfaces(self) -> None:
        with authenticated():
            listing = {
                w["id"]: w
                for w in self.client.get("/gateway/v1/workflows").json()["workflows"]
            }
        timed = 0
        for record in self.registry.records():
            declared = record.manifest.get("typical_seconds")
            self.assertEqual(listing[record.id]["typical_seconds"], declared)
            self.assertEqual(self.catalog[record.id]["typical_seconds"], declared)
            timed += declared is not None
        self.assertGreaterEqual(timed, 10)
        # Fast workflows prefer sync, slow ones async.
        self.assertEqual(self.catalog["audio.generate.sa3-small-sfx"]["execution"]["recommended"], "sync")
        self.assertEqual(self.catalog["video.generate.ltx-2.5-t2v"]["execution"]["recommended"], "async")

    def test_alias_target_matching(self) -> None:
        self.assertEqual(match_alias_target("video-ltx-2.5-t2v", "video.generate", {"ltx-2.5-t2v"}), "ltx-2.5-t2v")
        self.assertEqual(match_alias_target("flux2-klein-4b-edit", "image.edit", {"flux2-klein-4b"}), "flux2-klein-4b")
        self.assertIsNone(match_alias_target("nothing", "image.edit", {"a"}))

    def test_discovery_surfaces_point_at_the_catalog(self) -> None:
        llms = self.client.get("/llms.txt").text
        self.assertIn("x-azizam-workflows", llms)
        self.assertIn("execution", llms)
        skill = self.client.get("/.well-known/skills/azizam-gateway/SKILL.md").text
        self.assertIn("x-azizam-workflows", skill)
        self.assertIn("execution.fields", skill)
        with authenticated():
            meta = self.client.get("/.well-known/azizam").json()
        self.assertEqual(meta["links"]["workflow_catalog"], "/openapi.json#x-azizam-workflows")

    def test_augment_is_idempotent_on_a_bare_spec(self) -> None:
        import copy

        bare = {"openapi": "3.1.0", "info": {"title": "FastAPI", "version": "1"}, "paths": {}}
        records = tuple(self.registry.records())
        first = augment_openapi(bare, records, None)
        self.assertIs(first, bare)
        self.assertEqual(first["info"]["title"], "Azizam")
        self.assertEqual(validate_openapi(first), [])
        snapshot = copy.deepcopy(first)
        second = augment_openapi(first, records, None)
        self.assertIs(second, first)
        self.assertEqual(second, snapshot)
        self.assertEqual(validate_openapi(second), [])
        self.assertEqual(
            [row["id"] for row in second[ROOT_EXTENSION]],
            sorted(record.id for record in records),
        )


if __name__ == "__main__":
    unittest.main()
