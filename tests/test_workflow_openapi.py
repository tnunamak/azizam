"""The native OpenAPI is typed from the workflow manifests and stays consistent
with /gateway/v1/workflows, /gateway/v1/capabilities, /llms.txt and the skill."""

from __future__ import annotations

import base64
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import proxy  # noqa: E402
from tests.support.access import authenticated  # noqa: E402
from gateway import workflow_request_maps  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.workflow_openapi import (  # noqa: E402
    ROOT_EXTENSION,
    _reference_input_from_mapping,
    augment_openapi,
    extension_inputs_schema,
    extension_schema_name,
    match_alias_target,
    reference_input,
    request_mapping,
    schema_name,
    short_id,
    workflow_inputs_schema,
)
from openapi_lint import validate_openapi  # noqa: E402
from gateway.workflows_catalog import _workflow_row  # noqa: E402
from gateway.multipart_adapter import scalar_fields  # noqa: E402
from gateway.mesh_api import mesh_workflow_from_record  # noqa: E402
from tests.support.mesh_pool import fake_mesh_pool  # noqa: E402

MANIFESTS = ROOT / "manifests" / "workflows"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MIB = 1 << 20


def _execution_fixture():
    return yaml.safe_load(workflow_request_maps.DEFAULT_PATH.read_text(encoding="utf-8"))["execution"]


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

    def owned_by_routes(self, workflow_id: str) -> set[str]:
        """Inputs a route of the workflow sets itself, from the catalog's own field map."""
        execution = self.catalog[workflow_id]["execution"]
        return {name for route in (execution["sync"], execution["async"]) if route
                for name, where in route["fields"].items() if where["in"] == "none"}

    def test_every_manifest_has_typed_inputs(self) -> None:
        for record in self.registry.records():
            with self.subTest(workflow=record.id):
                self.assertIn(record.id, self.catalog)
                schema = self.schema_of(record.id)
                owned = self.owned_by_routes(record.id)
                settable = {n: s for n, s in record.manifest["inputs"].items() if n not in owned and s.get("settable") is not False}
                # A route-set input and a generated (non-settable) input are never advertised; every other manifest input is.
                self.assertEqual(sorted(schema["properties"]), sorted(settable))
                self.assertIs(schema["additionalProperties"], False)
                wanted = sorted(n for n, s in settable.items() if s.get("required"))
                self.assertEqual(sorted(schema.get("required", [])), wanted)
                for name, spec in settable.items():
                    prop = schema["properties"][name]
                    if spec["type"] in ("asset", "image"):
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
        # The mesh row probes its service live; the harness's fake upstream answers
        # instead, so this test never reaches a real trellis or observer.
        with authenticated(), \
             patch.object(proxy, "_capabilities_availability_evidence", new=AsyncMock(return_value={})), \
             fake_mesh_pool():
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
            path = f"/gateway/v1/workflows/{record.id}/run"
            body = self.spec["paths"][path]["post"]["requestBody"]
            form = self.spec["components"]["schemas"][schema_name(record.id, "WorkflowForm_")]
            self.assertEqual(
                body["content"]["multipart/form-data"]["schema"],
                {"$ref": "#/components/schemas/" + schema_name(record.id, "WorkflowForm_")},
            )
            for name, spec in record.manifest["inputs"].items():
                if spec["type"] == "asset":
                    self.assertEqual(form["properties"][name]["format"], "binary")

    def test_video_edit_sync_declares_a_job_without_rewriting_the_upload(self) -> None:
        edits = [row for row in self.catalog.values() if row["intent"] == "video.edit"
                 and row["execution"]["sync"]["path"] == f"/gateway/v1/workflows/{row['id']}/run"]
        self.assertTrue(edits)
        for row in edits:
            with self.subTest(workflow=row["id"]):
                route = row["execution"]["sync"]
                self.assertEqual(route["content_type"], "multipart/form-data")
                self.assertNotEqual(route.get("kind"), "media_job")
                self.assertEqual(route["response"], {
                    "kind": "media_job",
                    "poll": "/gateway/v1/media/jobs/{job_id}",
                    "content": "/gateway/v1/media/jobs/{job_id}/content",
                })

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
        # A video manifest that declares width, height and length takes size and seconds from them (CODE_TO_DATA S7, V5a);
        # the chain workflow has no length input, so the route still owns its seconds.
        self.assertNotIn("seconds", by_name["video.generate.ltx-2.5-t2v"])
        self.assertNotIn("size", by_name["video.generate.ltx-2.5-t2v"])
        self.assertIn("seconds", by_name["video.generate.ltx-2.5-long"])
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
                (extension_schema_name(r.id) if r.intent.startswith("video.") else schema_name(r.id))
                for r in self.registry.records()
                if r.intent.startswith(intent)
            }
            self.assertEqual({o["$ref"].rsplit("/", 1)[1] for o in options}, wanted)

    def test_video_route_keeps_descriptor_controls_beside_workflow_inputs(self) -> None:
        # The workflow pass must add its namespace, not replace the descriptor-owned video controls.
        for path in ("/v1/videos", "/openai/v1/videos"):
            schema = self.spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
            namespaces = schema["properties"]["extensions"]["properties"]
            self.assertIn("com.azizam.workflow", namespaces, path)
            self.assertEqual(namespaces["com.azizam.video"]["properties"]["audio"], {"type": "boolean", "default": True}, path)

    def test_sample_requests_validate(self) -> None:
        """A request built from the catalog's field map validates against the schema."""
        for record in self.registry.records():
            sample = {}
            owned = self.owned_by_routes(record.id)
            for name, spec in record.manifest["inputs"].items():
                if name in owned:
                    continue
                if spec["type"] in ("asset", "image"):
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
                    for key in ("submit", "poll", "content", "statuses", "terminal", "fields"):
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
        # Both pictures travel as repeated parts of the one field the edit
        # route reads, in submission order; the second is not a field of its own.
        self.assertEqual(edit["source_image"], {"in": "file", "field": "image[]"})
        self.assertEqual(edit["reference_image_2"], {"in": "file", "field": "image[]"})

    def mesh_row(self) -> dict:
        """The people-facing workflow row for mesh, which carries the example request."""
        (record,) = [r for r in self.registry.records() if r.id == "mesh.generate.trellis-2"]
        return _workflow_row(
            record, capability_rows=[], openapi_row=self.catalog[record.id],
            receipts=[], jobs=[], base_url="http://testserver",
        )

    def test_mesh_reserved_extensions_is_not_an_accepted_form_field(self) -> None:
        # The mesh route refuses extensions until a service option maps to it, so the
        # mapping must not accept it either.
        sync = self.catalog["mesh.generate.trellis-2"]["execution"]["sync"]
        self.assertEqual(sync["fields"]["extensions"], {"in": "none"})

    def test_mesh_example_carries_the_picture_and_supported_scalars_only(self) -> None:
        example = self.mesh_row()["example_request"]
        self.assertEqual(example["content_type"], "multipart/form-data")
        self.assertEqual(set(example["files"]), {"image"})
        self.assertEqual(example["files"]["image"]["field"], "image")
        self.assertNotIn("extensions", example["form"])
        self.assertIn("resolution", example["form"])
        self.assertTrue(set(example["form"]) <= {"seed", "resolution", "bg_removal"})

    def test_mesh_example_fields_bind_through_the_mesh_route(self) -> None:
        example = self.mesh_row()["example_request"]
        manifest = mesh_workflow_from_record(self.registry.get("mesh.generate.trellis-2"))
        field_to_input = {field: name for name, field in manifest.mapping.items()}
        self.assertTrue(set(example["form"]) <= set(field_to_input))
        supplied = {field_to_input[field]: value for field, value in example["form"].items()}
        fields = scalar_fields(manifest.inputs, manifest.mapping, supplied)
        self.assertEqual(set(fields), set(example["form"]))

    def test_mesh_is_one_multipart_post_with_its_picture_as_a_file_part(self) -> None:
        mesh = self.catalog["mesh.generate.trellis-2"]["execution"]
        # The long run is recommended as the job the same route answers when asked with Prefer: respond-async.
        self.assertEqual(mesh["recommended"], "async")
        self.assertEqual(mesh["async"]["kind"], "respond_async")
        submit = mesh["async"]["submit"]
        self.assertEqual((submit["method"], submit["path"]), ("POST", "/v1/3d/generations/glb"))
        self.assertEqual(submit["content_type"], "multipart/form-data")
        self.assertEqual(submit["headers"], {"Prefer": "respond-async"})
        self.assertTrue(mesh["async"]["poll"]["path"].startswith("/gateway/v1/media/jobs/"))
        self.assertTrue(mesh["async"]["content"]["path"].endswith("/content"))
        self.assertTrue(mesh["async"]["cancel"]["path"].endswith("/cancel"))
        self.assertEqual(mesh["async"]["content"]["output"], "model/gltf-binary (GLB bytes)")
        self.assertEqual(mesh["async"]["fields"], mesh["sync"]["fields"])
        self.assertEqual(mesh["sync"]["method"], "POST")
        self.assertEqual(mesh["sync"]["path"], "/v1/3d/generations/glb")
        self.assertEqual(mesh["sync"]["content_type"], "multipart/form-data")
        self.assertEqual(mesh["sync"]["fields"]["image"], {"in": "file", "field": "image"})
        self.assertEqual(mesh["sync"]["fields"]["seed"], {"in": "form", "field": "seed"})
        self.assertEqual(mesh["sync"]["output"], "model/gltf-binary (GLB bytes)")
        from gateway.workflow_openapi import workflow_inputs_schema

        (mesh_record,) = [r for r in self.registry.records() if r.id == "mesh.generate.trellis-2"]
        form = workflow_inputs_schema(mesh_record, form=True)
        self.assertEqual(form["properties"]["image"]["format"], "binary")
        self.assertEqual(form["required"], ["image"])

    def test_mesh_example_asks_for_the_job_and_does_not_save_it_as_a_model(self) -> None:
        example = self.mesh_row()["example_request"]
        self.assertEqual(example["path"], "/v1/3d/generations/glb")
        self.assertIn("-H 'Prefer: respond-async'", example["curl"])
        self.assertIn("'Prefer': 'respond-async'", example["python"])
        self.assertIn('"Prefer": "respond-async"', example["javascript"])
        # The 202 is the job's JSON, so no example writes it to model.glb.
        for language in ("curl", "python", "javascript"):
            self.assertNotIn(".glb", example[language])

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



    def test_shipped_video_reference_role_relations_match_previous_selection(self) -> None:
        """Static relation gate; full runtime outcome/golden parity remains a lead run."""
        records = {record.id: record for record in self.registry.records()}
        manifests = {
            key: record.manifest["video"] for key, record in records.items()
            if isinstance(record.manifest.get("video"), dict)
        }
        self.assertTrue(manifests, "No shipped video blocks were checked")
        reference_edges = 0
        for workflow_id, manifest in manifests.items():
            with self.subTest(workflow=workflow_id):
                eligible = []
                for role, candidate_id in (manifest.get("variants") or {}).items():
                    candidate = records.get(candidate_id)
                    candidate_manifest = manifests.get(candidate_id)
                    if candidate is None or candidate_manifest is None:
                        continue
                    target = reference_input(candidate, "async")
                    if target is not None and (candidate_manifest.get("roles") or {}).get(role, role) == target:
                        eligible.append(candidate_id)
                # This is the pre-change selector's edge, captured in the scoped source.
                old_variant = (manifest.get("variants") or {}).get("image")
                previous = [old_variant] if old_variant in manifests else []
                self.assertEqual(eligible, previous, f"{workflow_id}: shipped variant role-to-input relation differs")
                reference_edges += len(previous)
                target = reference_input(records[workflow_id], "async")
                required = set(manifest.get("requires") or ())
                reference_roles = {
                    role for role in required
                    if target is not None and (manifest.get("roles") or {}).get(role, role) == target
                }
                self.assertEqual(bool(reference_roles), "image" in required,
                                 f"{workflow_id}: shipped required-reference relation differs")
                if target is not None:
                    self.assertEqual((manifest.get("roles") or {}).get("image", "input_reference"), target,
                                     f"{workflow_id}: staged reference key differs")
        self.assertGreater(reference_edges, 0, "No shipped reference variant was checked")


class RequestMappingTypeTests(unittest.TestCase):
    def test_non_string_input_types_are_not_assets(self) -> None:
        from types import SimpleNamespace
        from gateway.workflow_openapi import request_mapping

        for kind in (["image"], {"type": "image"}):
            with self.subTest(kind=kind):
                record = SimpleNamespace(intent="video.edit", contract={"inputs": {"photo": {"type": kind}}})
                self.assertEqual(request_mapping(record, "sync"), {"photo": {"in": "form", "field": "photo"}})




class ReferenceInputTests(unittest.TestCase):
    @staticmethod
    def record(inputs, *, intent="unfamiliar.render"):
        return SimpleNamespace(
            id=f"{intent}.fixture_7q", intent=intent,
            contract={"inputs": copy.deepcopy(inputs), "bindings": {}},
        )

    @staticmethod
    def asset():
        return {"type": "asset", "accepts": ["image/png"]}

    @staticmethod
    def maps(record, placements, *, mode="async"):
        # Exercise the real grammar and projection; only the loaded data changes.
        document = {"schema_version": 1, "execution": _execution_fixture(), "maps": [
            {"select": {"intent": record.intent, "mode": mode},
             "inputs": placements, "default": {"in": "extension"}},
            {"select": {"any": True}, "default": {"in": "extension"}},
        ]}
        with patch.object(Path, "read_text", return_value=json.dumps(document)):
            maps = workflow_request_maps.load(Path("reference-input-fixture.yml"))
        return patch.object(workflow_request_maps, "_MAPS", maps)

    def test_reference_input_renamed_contract_key_matches_projection(self) -> None:
        record = self.record({"image": self.asset()})
        placements = {"image": {"in": "body", "field": "input_reference"}}
        with self.maps(record, placements):
            self.assertEqual(reference_input(record, "async"), "image")
        record.contract["inputs"]["anchor_7q"] = record.contract["inputs"].pop("image")
        placements["anchor_7q"] = placements.pop("image")
        snapshot = copy.deepcopy(record.contract)
        with self.maps(record, placements):
            fields = request_mapping(record, "async")
            self.assertEqual(reference_input(record, "async"), "anchor_7q")
            self.assertEqual(fields, {"anchor_7q": {"in": "body", "field": "input_reference"}})
            self.assertNotIn("image", fields)
            schema = workflow_inputs_schema(record)
            self.assertIn("anchor_7q", schema["properties"])
            self.assertNotIn("image", schema["properties"])
            self.assertNotIn("anchor_7q", extension_inputs_schema(record)["properties"])
            self.assertEqual(record.contract, snapshot)
            self.assertEqual(request_mapping(record, "async"), fields)
            self.assertEqual(fields, placements)

    def test_reference_input_two_assets_uses_placement_not_mime_or_order(self) -> None:
        for decoy in ("decoy_2z", "image"):
            for reverse in (False, True):
                for extension_field in (decoy, "input_reference"):
                    with self.subTest(decoy=decoy, reverse=reverse, field=extension_field):
                        names = [decoy, "anchor_7q"]
                        if reverse:
                            names.reverse()
                        record = self.record({name: self.asset() for name in names})
                        placements = {
                            decoy: {"in": "extension", "field": extension_field},
                            "anchor_7q": {"in": "body", "field": "input_reference"},
                        }
                        with self.maps(record, placements):
                            self.assertEqual(reference_input(record, "async"), "anchor_7q")
                            fields = request_mapping(record, "async")
                            self.assertEqual(list(fields), names)
                            self.assertEqual(fields, placements)

    def test_reference_input_without_mapping_returns_none_even_with_image_asset(self) -> None:
        record = self.record({"image": self.asset(), "anchor_7q": self.asset()})
        placements = {
            "image": {"in": "extension", "field": "image"},
            "anchor_7q": {"in": "extension", "field": "input_reference"},
        }
        with self.maps(record, placements):
            self.assertIsNone(reference_input(record, "async"))
            self.assertEqual(request_mapping(record, "async"), placements)
        run = self.record(
            {"image": self.asset(), "anchor_7q": self.asset(), "prompt": {"type": "string"}},
            intent="video.edit",
        )
        # The shipped sync row carries files and form fields, not a reference.
        before = request_mapping(run, "sync")
        self.assertIsNone(reference_input(run, "sync"))
        self.assertEqual(before["image"], {"in": "file", "field": "image"})
        self.assertEqual(before["prompt"], {"in": "form", "field": "prompt"})
        self.assertEqual(request_mapping(run, "sync"), before)
        with self.maps(run, placements):
            self.assertIsNone(reference_input(run, "async"))
            self.assertEqual(request_mapping(run, "async"), {
                **placements, "prompt": {"in": "extension", "field": "prompt"},
            })

    def test_reference_input_duplicate_body_mappings_are_rejected(self) -> None:
        messages = []
        for names in (("anchor_7q", "decoy_2z"), ("decoy_2z", "anchor_7q")):
            record = self.record({name: self.asset() for name in names})
            placements = {name: {"in": "body", "field": "input_reference"} for name in names}
            with self.maps(record, placements):
                for lookup in (request_mapping, reference_input):
                    with self.subTest(names=names, lookup=lookup.__name__):
                        with self.assertRaises(ValueError) as caught:
                            lookup(record, "async")
                        messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1)
        self.assertEqual(messages[0],
                         f"{record.id} (async): multiple inputs map to body.input_reference: "
                         "['anchor_7q', 'decoy_2z']")

    def test_reference_input_requires_a_settable_scalar_without_combine(self) -> None:
        placement = {"in": "body", "field": "input_reference"}
        for spec in ({"type": "string"}, {"type": ["asset"]}, {"type": "object"},
                     {"type": "array", "items": {"type": "string"}}):
            record = self.record({"anchor_7q": spec})
            with self.maps(record, {"anchor_7q": placement}):
                for lookup in (request_mapping, reference_input):
                    with self.subTest(spec=spec, lookup=lookup.__name__):
                        with self.assertRaisesRegex(ValueError, "settable scalar asset"):
                            lookup(record, "async")
        record = self.record({"anchor_7q": self.asset(), "decoy_2z": self.asset()})
        combined = {**placement, "combine": ["anchor_7q", "decoy_2z"]}
        with self.maps(record, {"anchor_7q": combined}):
            for lookup in (request_mapping, reference_input):
                with self.subTest(lookup=lookup.__name__):
                    with self.assertRaisesRegex(ValueError, "cannot combine"):
                        lookup(record, "async")
        # Directly test invalid effective maps too: no row or member-set lookup is needed.
        for inputs in ({}, {"anchor_7q": None}, {"anchor_7q": "asset"},
                       {"anchor_7q": {**self.asset(), "settable": False}}):
            with self.subTest(inputs=inputs):
                record = self.record(inputs)
                with self.assertRaisesRegex(ValueError, "settable scalar asset"):
                    _reference_input_from_mapping(record, "async", {"anchor_7q": placement})
        # Duplicate detection precedes validation/resolution of either candidate.
        record = self.record({"anchor_7q": {"type": "string"}, "decoy_2z": self.asset()})
        with self.assertRaisesRegex(ValueError, "multiple inputs"):
            _reference_input_from_mapping(record, "async", {
                "decoy_2z": placement, "anchor_7q": combined,
            })
        # Container suppression happens before the completed map is validated.
        record = self.record({"anchor_7q": {"type": "array", "items": self.asset()}})
        with self.maps(record, {"anchor_7q": placement}):
            self.assertEqual(request_mapping(record, "async"), {"anchor_7q": {"in": "none"}})
            self.assertIsNone(reference_input(record, "async"))

    def test_reference_input_uses_current_contract_mode_and_map_without_mutation(self) -> None:
        record = self.record({"anchor_7q": {"type": "asset", "accepts": []},
                              "decoy_2z": self.asset()})
        # A legacy view must not override the canonical contract.
        record.manifest = {"inputs": {"image": self.asset()}}
        placements = {
            "anchor_7q": {"in": "body", "field": "input_reference"},
            "decoy_2z": {"in": "extension", "field": "decoy_2z"},
        }
        contract_before = copy.deepcopy(record.contract)
        placements_before = copy.deepcopy(placements)
        with self.maps(record, placements):
            fields = request_mapping(record, "async")
            fields_before = copy.deepcopy(fields)
            self.assertEqual(reference_input(record, "async"), "anchor_7q")
            self.assertIsNone(reference_input(record, "sync"))
            # Prove the primitive itself never projects or resolves membership.
            with patch.object(workflow_request_maps, "select", side_effect=AssertionError("selected a row")), \
                    patch.object(workflow_request_maps, "_members", side_effect=AssertionError("resolved a set")):
                self.assertEqual(_reference_input_from_mapping(record, "async", fields), "anchor_7q")
            self.assertEqual(fields, fields_before)
        swapped = {
            "anchor_7q": {"in": "extension", "field": "anchor_7q"},
            "decoy_2z": {"in": "body", "field": "input_reference"},
        }
        with self.maps(record, swapped):
            self.assertEqual(reference_input(record, "async"), "decoy_2z")
        other = self.record({"fresh_9v": self.asset()})
        self.assertEqual(other.id, record.id)
        with self.maps(other, {"fresh_9v": {"in": "body", "field": "input_reference"}}):
            self.assertEqual(reference_input(other, "async"), "fresh_9v")
        self.assertEqual(record.contract, contract_before)
        self.assertEqual(placements, placements_before)


    def test_reference_input_uses_effective_scalar_asset_placement(self) -> None:
        record = self.record({"guide_frame": self.asset()}, intent="fixture.generate")
        placement = {"in": "body", "field": "input_reference"}
        with FrontendVideoReferenceTests.maps():
            self.assertEqual(request_mapping(record, "async")["guide_frame"], placement)
            self.assertEqual(reference_input(record, "async"), "guide_frame")
            self.assertEqual(request_mapping(record, "sync")["guide_frame"],
                             {"in": "file", "field": "guide_frame"})
            self.assertIsNone(reference_input(record, "sync"))
        for where in ("file", "form", "extension", "none"):
            with self.subTest(where=where):
                mapped = {"in": where} if where == "none" else {"in": where, "field": "input_reference"}
                with self.maps(record, {"guide_frame": mapped}):
                    self.assertIsNone(reference_input(record, "async"))
        for inputs in ({}, {"prompt": {"type": "string"}},
                       {"guide_frame": {"type": "array", "items": self.asset()}}):
            with self.subTest(inputs=inputs):
                nonmatch = self.record(inputs, intent="fixture.generate")
                with self.maps(nonmatch, {"guide_frame": placement}):
                    self.assertIsNone(reference_input(nonmatch, "async"))
                    if "guide_frame" in inputs:
                        self.assertEqual(request_mapping(nonmatch, "async")["guide_frame"], {"in": "none"})
        # The accepted helper on this base rejects malformed scalar placement rather
        # than treating it as absent. Preserve its existing strict validation tests.
        malformed = self.record({"guide_frame": {"type": "string"}}, intent="fixture.generate")
        with self.maps(malformed, {"guide_frame": placement}):
            with self.assertRaisesRegex(ValueError, "settable scalar asset"):
                reference_input(malformed, "async")
        duplicate = self.record({"guide_frame": self.asset(), "other_frame": self.asset()}, intent="fixture.generate")
        with self.maps(duplicate, {"guide_frame": placement, "other_frame": placement}):
            with self.assertRaisesRegex(ValueError, "multiple inputs"):
                reference_input(duplicate, "async")

    def test_reference_input_rejects_unsupported_modes(self) -> None:
        record = self.record({"anchor_7q": self.asset()})
        placements = {"anchor_7q": {"in": "body", "field": "input_reference"}}
        with self.maps(record, placements):
            for mode in ("any", "", "ASYNC", "future"):
                for lookup in (request_mapping, reference_input):
                    with self.subTest(mode=mode, lookup=lookup.__name__):
                        with self.assertRaisesRegex(ValueError, "unsupported request mode"):
                            lookup(record, mode)
                with self.assertRaisesRegex(ValueError, "unsupported request mode"):
                    _reference_input_from_mapping(record, mode, {})



class FrontendVideoReferenceTests(unittest.IsolatedAsyncioTestCase):
    """Selection and staging use real effective maps, with runtime seams held fixed."""

    def setUp(self) -> None:
        from gateway.frontend_shapes import shapes, video_create

        self.shapes, self.video_create = shapes, video_create
        self.shape = shapes.OpenAIVideoCreate()
        self.caller, self.spool, self.sentinel_ref = object(), object(), object()
        self.events = []
        self.body = {
            "model": "fixture.generate.base", "prompt": "move", "seed": 7,
            "seconds": 5, "size": "640x480",
            "input_reference": "data:image/png;base64," + base64.b64encode(PNG).decode(),
        }
        self.base = self.entry("base")
        self.candidate = self.entry("guided", reference=True)
        self.entries = {entry.id: entry for entry in (self.base, self.candidate)}
        self.manifests = {
            self.base.id: self.manifest(variants={"guide": self.candidate.id}),
            self.candidate.id: self.manifest(roles={"guide": "guide_frame"}, requires={"guide"}),
        }
        self.snapshot = SimpleNamespace(
            get=self.entries.get,
            names=SimpleNamespace(owner=lambda _kind, name: name if name in self.entries else None),
        )
        self.row = SimpleNamespace(id="fixture.create", routes=(), job_failure_text="", job_retries=0)
        self.hooks = shapes.VideoHooks()
        self.check_reference = Mock(side_effect=lambda *args: self.events.append(("check", args)))
        self.hooks.check_reference = self.check_reference
        self.enterContext(patch.object(shapes, "VIDEO_HOOKS", self.hooks))
        self.enterContext(patch.object(video_create._wire, "choose_workflow",
                                       side_effect=lambda _snapshot, _row, *, target, **_kw: self.entries[target]))
        self.enterContext(patch.object(video_create.video_grid, "video_facts",
                                       side_effect=lambda entry: self.manifests.get(entry.id)))
        # The assignment does not read the production snapshot/manifest constructor.
        # Keep unrelated runtime projection and size arithmetic at explicit seams.
        self.enterContext(patch.object(video_create.video_grid, "resolve_size", return_value=(640, 480)))
        self.enterContext(patch.object(video_create.video_grid, "frame_count", return_value=120))
        self.enterContext(patch.object(video_create._wire, "declared",
                                       side_effect=lambda entry, key: key in entry.contract["inputs"]))
        self.enterContext(patch.object(video_create._wire, "asset_input_names", side_effect=lambda entry: [
            key for key, spec in entry.contract["inputs"].items() if spec.get("type") == "asset"
        ]))
        self.enterContext(patch.object(video_create._wire, "entry_required",
                                       side_effect=lambda entry, key: entry.contract["inputs"][key].get("required")))
        self.enterContext(patch.object(video_create.media_delivery, "current", return_value=None))
        self.stage = self.enterContext(patch.object(video_create._wire, "stage_uploads", new=AsyncMock(side_effect=self.stage_uploads)))
        self.lookup = self.enterContext(patch.object(video_create, "reference_input", wraps=reference_input))
        self.enterContext(self.maps())

    @staticmethod
    def entry(name, *, reference=False):
        inputs = {"prompt": {"type": "string", "required": True}, "seed": {"type": "integer"}}
        if reference:
            inputs["guide_frame"] = {"type": "asset", "accepts": ["image/png"], "required": True}
        # Deliberately no entry.intent: the adapter must read the contract.
        return SimpleNamespace(id=f"fixture.generate.{name}", contract={
            "intent": "fixture.generate", "capability": "clip", "inputs": inputs, "bindings": {},
        })

    @staticmethod
    def manifest(*, variants=None, roles=None, requires=()):
        return SimpleNamespace(
            variants=variants or {}, roles=roles or {}, requires=frozenset(requires),
            chain=None, select_subject=None, grid=SimpleNamespace(fps=24),
            length=SimpleNamespace(min_seconds=1, max_seconds=8, default_seconds=5), size=object(),
        )

    @staticmethod
    def maps():
        document = {"schema_version": 1, "execution": _execution_fixture(), "maps": [
            {"select": {"intent": "fixture.generate", "mode": "sync"},
             "inputs": {"guide_frame": {"in": "file", "field": "guide_frame"}},
             "default": {"in": "extension"}},
            {"select": {"intent": "fixture.generate", "mode": "async"},
             "inputs": {"guide_frame": {"in": "body", "field": "input_reference"}},
             "default": {"in": "extension"}},
            {"select": {"any": True}, "default": {"in": "extension"}},
        ]}
        with patch.object(Path, "read_text", return_value=json.dumps(document)):
            maps = workflow_request_maps.load(Path("frontend-reference-fixture.yml"))
        return patch.object(workflow_request_maps, "_MAPS", maps)

    def request(self, body):
        from starlette.requests import Request

        raw = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/v1/videos",
                           "headers": [(b"content-type", b"application/json")]}, receive)
        request.state.caller = self.caller
        return request

    async def stage_uploads(self, spool, uploads, mapping):
        self.assertIs(spool, self.spool)
        self.assertEqual(mapping, {"guide_frame": "guide_frame"})
        self.assertEqual(len(uploads), 1)
        name, upload = uploads[0]
        self.assertEqual(name, "guide_frame")
        self.assertEqual(await upload.read(), PNG)
        self.assertEqual([event[0] for event in self.events], ["check"])
        self.events.append(("stage", name))
        return {"guide_frame": (self.sentinel_ref,)}

    async def run_body(self, body):
        return await self.shape.to_run_request(self.request(body), self.row, self.snapshot, spool=self.spool)

    def assert_handoff(self, result, body):
        from gateway.frontend_shapes._base import LegacyRoute

        self.assertIsInstance(result, LegacyRoute)
        self.assertEqual(result, LegacyRoute(self.video_create.LEGACY_CREATE, body))
        self.check_reference.assert_not_called()
        self.stage.assert_not_awaited()

    async def test_frontend_video_create_renamed_reference_selects_variant_and_stages_contract_key(self):
        from gateway.workflow_runtime_types import RunRequest

        result = await self.run_body(self.body)
        self.assertIsInstance(result, RunRequest)
        self.assertEqual(result.workflow_id, "fixture.generate.guided")
        self.assertEqual(result.media, {"guide_frame": (self.sentinel_ref,)})
        self.assertEqual(result.sources, {"guide_frame": ()})
        self.assertEqual(result.inputs["prompt"], "move")
        self.assertEqual(result.inputs["seed"], 7)
        self.assertNotIn("image", result.media)
        self.assertNotIn("input_reference", result.media)
        self.check_reference.assert_called_once_with(self.caller, PNG, self.candidate.contract, "guide_frame")
        self.assertIs(self.check_reference.call_args.args[2], self.candidate.contract)
        self.stage.assert_awaited_once()
        candidate_calls = [call for call in self.lookup.call_args_list if call.args[0].id == self.candidate.id]
        self.assertTrue(candidate_calls)
        self.assertTrue(all(call.args[1] == "async" and call.args[0].contract is self.candidate.contract
                            for call in candidate_calls))
        self.assertFalse(hasattr(self.candidate, "intent"))

        self.check_reference.reset_mock()
        self.stage.reset_mock()
        body = {key: value for key, value in self.body.items() if key != "input_reference"}
        body["model"] = self.candidate.id
        result = await self.run_body(body)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(json.loads(result.body), {"error": {
            "message": "Model clip-guided is image-to-video and requires input_reference",
            "type": "invalid_request_error", "param": "input_reference",
        }})
        self.check_reference.assert_not_called()
        self.stage.assert_not_awaited()

    async def test_frontend_video_create_reference_without_mapping_hands_off_before_effects(self):
        from gateway.workflow_runtime_types import RunRequest

        self.entries.pop(self.candidate.id)
        self.manifests[self.base.id].variants = {}
        record = SimpleNamespace(id=self.base.id, intent=self.base.contract["intent"], contract=self.base.contract)
        self.assertIsNone(reference_input(record, "async"))
        picked = self.video_create.select(self.shape, self.body, self.row, self.snapshot, self.hooks)
        self.assert_handoff(picked, self.body)
        self.assert_handoff(await self.run_body(self.body), self.body)
        for reference in ("", None):
            with self.subTest(reference=reference):
                body = {**self.body, "input_reference": reference}
                result = await self.run_body(body)
                if reference is not None:
                    self.assert_handoff(result, body)
                else:
                    self.assertIsInstance(result, RunRequest)
        body = {key: value for key, value in self.body.items() if key != "input_reference"}
        result = await self.run_body(body)
        self.assertIsInstance(result, RunRequest)
        self.assertEqual(result.workflow_id, "fixture.generate.base")
        self.assertEqual(result.media, {})
        self.assertEqual(result.sources, {})
        self.check_reference.assert_not_called()
        self.stage.assert_not_awaited()

    async def test_frontend_video_create_invalid_reference_keeps_decode_handoff(self):
        body = {**self.body, "input_reference": "https://example.invalid/reference.png"}
        self.assert_handoff(await self.run_body(body), body)
        # Empty and null still use the missing-reference branch for a required role.
        for reference in ("", None):
            with self.subTest(reference=reference):
                result = await self.run_body({**self.body, "model": self.candidate.id, "input_reference": reference})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(json.loads(result.body)["error"]["param"], "input_reference")
        self.check_reference.assert_not_called()
        self.stage.assert_not_awaited()

    async def test_frontend_video_reference_variant_selection_rejects_ambiguity(self):
        other = self.entry("alternate", reference=True)
        self.entries[other.id] = other
        self.manifests[other.id] = self.manifest(roles={"anchor": "guide_frame"}, requires={"anchor"})
        edges = [("guide", self.candidate.id), ("anchor", other.id)]
        messages = []
        for ordered in (edges, list(reversed(edges))):
            self.manifests[self.base.id].variants = dict(ordered)
            with self.assertRaises(ValueError) as caught:
                await self.run_body(self.body)
            messages.append(str(caught.exception))
            self.check_reference.assert_not_called()
            self.stage.assert_not_awaited()
        self.assertEqual(messages[0], messages[1])

        # A candidate accepting a reference is not enough: this edge names duration.
        self.manifests[self.base.id].variants = {"duration": self.candidate.id}
        self.manifests[self.candidate.id].roles["duration"] = "clip_length"
        self.assert_handoff(await self.run_body(self.body), self.body)
        # A same-name role/input is the structural fallback, without a special spelling.
        self.manifests[self.base.id].variants = {"guide_frame": self.candidate.id}
        self.manifests[self.candidate.id].roles = {}
        self.manifests[self.candidate.id].requires = frozenset({"guide_frame"})
        result = await self.run_body(self.body)
        self.assertEqual(result.workflow_id, self.candidate.id)
        self.assertEqual(result.media, {"guide_frame": (self.sentinel_ref,)})



class FrontendReferenceWireTests(unittest.TestCase):
    def test_frontend_images_multipart_spellings_remain_wire_fields(self):
        from gateway.frontend_shapes import shapes, video_create

        self.assertEqual(shapes.IMAGE_PARTS, ("image[]", "image", "images"))
        shape = shapes.OpenAIImagesMultipart()
        for name in shapes.IMAGE_PARTS:
            with self.subTest(name=name):
                self.assertIsNone(shape.precheck({"prompt": "edit"}, [(name, object())]))
        for uploads in ([], [("guide_frame", object())]):
            with self.subTest(uploads=uploads):
                result = shape.precheck({"prompt": "edit"}, uploads)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(json.loads(result.body)["error"]["message"], "image is required")
        self.assertFalse(hasattr(shapes, "REFERENCE_KIND"))
        self.assertNotIn("REFERENCE_KIND", Path(video_create.__file__).read_text(encoding="utf-8"))


class WorkflowResponseKindDeclarationTests(unittest.TestCase):
    def test_every_sync_route_declares_its_response_beside_unchanged_output_text(self) -> None:
        from types import SimpleNamespace
        from gateway.workflow_openapi import _execution

        cases = (
            ("unfamiliar.render", "/native-fixture",
             {"kind": "json"}, "JSON (see the route's response schema)"),
            ("image.generate", None, {"kind": "json"}, "data[].b64_json or data[].url"),
            ("image.edit", None, {"kind": "json"}, "data[].b64_json or data[].url"),
            ("audio.generate", None, {"kind": "binary"}, "audio bytes (see Content-Type)"),
            ("mesh.generate", None, {"kind": "binary", "media_type": "model/gltf-binary"},
             "model/gltf-binary (GLB bytes)"),
            ("video.edit", None,
             {"kind": "media_job", "poll": "/gateway/v1/media/jobs/{job_id}",
              "content": "/gateway/v1/media/jobs/{job_id}/content"},
             "a media job; its content is the video"),
        )
        for intent, native, response, output in cases:
            with self.subTest(intent=intent, native=native):
                record = SimpleNamespace(
                    id=f"{intent}.fixture", intent=intent,
                    contract={"inputs": {}, "bindings": {"gateway": {"route": native}} if native else {}},
                )
                route = _execution(record, 1)["sync"]
                self.assertEqual(route["output"], output)
                self.assertEqual(route["response"], response)

    def test_all_shipped_sync_routes_have_a_supported_response_kind(self) -> None:
        from gateway.workflow_openapi import _execution

        registry = WorkflowRegistry.from_directory(MANIFESTS)
        self.assertEqual(registry.errors, [])
        self.assertTrue(registry.records())
        for record in registry.records():
            with self.subTest(workflow=record.id):
                route = _execution(record, 1)["sync"]
                if route:
                    self.assertIn(route["response"]["kind"], {"json", "binary", "media_job"})


class ExecutionDataTests(unittest.TestCase):
    @staticmethod
    def document():
        return {
            "schema_version": 1,
            "maps": [{"select": {"any": True}, "default": {"in": "extension"}}],
            "execution": _execution_fixture(),
        }

    @staticmethod
    def load_document(document, *, records=None):
        with patch.object(Path, "read_text", return_value=json.dumps(document)):
            return workflow_request_maps.load(Path("execution-fixture.yml"), records=records)

    @staticmethod
    def record(intent, *, native=None):
        return SimpleNamespace(
            id=f"{intent}.fixture", intent=intent,
            contract={"inputs": {}, "bindings": {"gateway": {"route": native}} if native else {}},
        )

    def test_execution_reader_rejects_malformed_rows_with_filename(self):
        # Each mutation must fail in the reader, before any projection happens.
        cases = [
            (("unexpected",), True),
            (("schema_version",), True),
            (("schema_version",), 1.0),
            (("execution",), None),
            (("execution", "unknown"), True),
            (("execution", "intents"), {}),
            (("execution", "intents", "image.edit", "extra"), True),
            (("execution", "intents", "image.edit", "model_prefix"), 1),
            (("execution", "intents", "image.edit", "route_inputs"), ["count", "count"]),
            (("execution", "intents", "image.edit", "route_inputs"), ["absent"]),
            (("execution", "intents", "image.edit", "sync"), "absent"),
            (("execution", "intents", "image.edit", "async"), "generated_image"),
            (("execution", "intents", "image.edit", "native_alternate", "model"), {"from": "other"}),
            (("execution", "intents", "image.edit", "native_alternate", "model"),
             {"from": "model", "text": "suffix"}),
            (("execution", "field_rows", "count", "unless_all_inputs"), ["x", "x"]),
            (("execution", "field_rows", "count", "value", "minimum"), True),
            (("execution", "field_rows", "count", "value", "minimum"), 11),
            (("execution", "field_rows", "count", "value", "in"), "file"),
            (("execution", "field_rows", "count", "value", "unknown"), 1),
            (("execution", "native_sync", "model"), {"from": "model"}),
            (("execution", "native_sync", "path"), {"from": "workflow_run_path"}),
            (("execution", "sync_templates", "generated_image", "unknown"), 1),
            (("execution", "sync_templates", "generated_image", "model"), {"from": "fields", "mode": "sync"}),
            (("execution", "sync_templates", "generated_image", "fields"), {"from": "fields", "mode": "other"}),
            (("execution", "sync_templates", "generated_image", "fields"), {"from": "fields", "mode": "async"}),
            (("execution", "sync_templates", "generated_image", "response", "unknown"), 1),
            (("execution", "sync_templates", "generated_image", "response"), {"kind": "json", "poll": "/x"}),
            (("execution", "sync_templates", "generated_image", "output"), {"from": "model"}),
            (("execution", "async_templates", "generated_image_job", "submit", "body", "unknown"), 1),
            (("execution", "async_templates", "generated_image_job", "poll", "unknown"), 1),
            (("execution", "async_templates", "generated_image_job", "poll", "path"), {"from": "model"}),
            (("execution", "async_templates", "generated_image_job", "poll", "path"), "/x/{expression}"),
            (("execution", "async_templates", "generated_image_job", "poll", "path"), "//host/x"),
            (("execution", "async_templates", "generated_image_job", "statuses"), ["queued", "queued"]),
            (("execution", "async_templates", "generated_image_job", "terminal"), ["not-a-status"]),
            (("execution", "async_templates", "generated_image_job", "kind"), "unknown"),
            (("execution", "async_templates", "prefer_binary_model_job", "fields"), {"from": "fields", "mode": "async"}),
            (("execution", "async_templates", "prefer_binary_model_job", "submit", "body"),
             {"intent": "unfamiliar.render", "request": "body"}),
            (("execution", "async_templates", "prefer_binary_model_job", "submit", "headers"), {"Other": "value"}),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                document = self.document()
                target = document
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError) as caught:
                    self.load_document(document)
                self.assertIn("execution-fixture.yml", str(caught.exception))
        for path in (("execution",),
                     ("execution", "intents", "image.edit", "async"),
                     ("execution", "sync_templates", "generated_image", "fields")):
            with self.subTest(missing=path):
                document = self.document()
                target = document
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                with self.assertRaisesRegex(ValueError, "execution-fixture.yml"):
                    self.load_document(document)
        document = self.document()
        document["execution"]["field_rows"]["same_name"] = copy.deepcopy(
            document["execution"]["field_rows"]["count"])
        document["execution"]["intents"]["image.edit"]["route_inputs"] = ["count", "same_name"]
        with self.assertRaisesRegex(ValueError, "duplicate output field"):
            self.load_document(document)

    def test_execution_reader_rejects_duplicate_yaml_and_unreadable_documents(self):
        raw = yaml.safe_dump(self.document(), sort_keys=False)
        raw = raw.replace("    image.generate:\n", "    image.generate: {}\n    image.generate:\n", 1)
        with patch.object(Path, "read_text", return_value=raw):
            with self.assertRaisesRegex(ValueError, "execution-fixture.yml"):
                workflow_request_maps.load(Path("execution-fixture.yml"))
        for raw in ("[]", "null", "1", "{broken"):
            with self.subTest(raw=raw), patch.object(Path, "read_text", return_value=raw):
                with self.assertRaisesRegex(ValueError, "execution-fixture.yml"):
                    workflow_request_maps.load(Path("execution-fixture.yml"))
        with patch.object(Path, "read_text", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(ValueError, "execution-fixture.yml.*cannot read"):
                workflow_request_maps.load(Path("execution-fixture.yml"))

    def test_intent_without_execution_row_gets_no_templated_routes(self):
        from gateway.workflow_openapi import _execution, workflow_catalog

        native = self.record("unfamiliar.render", native="/native-fixture")
        bare_record = self.record("unfamiliar.render")
        maps = self.load_document(self.document(), records=[native, bare_record])
        with patch.object(workflow_request_maps, "_MAPS", maps):
            self.assertIsNone(workflow_request_maps.execution_row("unfamiliar.render"))
            self.assertEqual(_execution(native, 1)["sync"]["path"], "/native-fixture")
            self.assertNotIn("alternate", _execution(native, 1)["sync"])
            self.assertIsNone(_execution(native, 1)["async"])
            self.assertEqual(_execution(bare_record, 1), {"recommended": "sync", "sync": None, "async": None})
            self.assertIsNone(maps.execution.render_sync(bare_record))
            self.assertIsNone(maps.execution.render_async(bare_record))
            workflow_catalog([self.record("image.generate"), native])

    def test_native_only_execution_row_requires_native_binding(self):
        document = self.document()
        row = copy.deepcopy(document["execution"]["intents"]["image.edit"])
        row["sync"] = None
        row["async"] = None
        document["execution"]["intents"]["unfamiliar.render"] = row
        with self.assertRaisesRegex(ValueError, "execution-fixture.yml.*needs a native binding"):
            self.load_document(document, records=[self.record("unfamiliar.render")])
        self.load_document(document, records=[self.record("unfamiliar.render", native="/native-fixture")])


    def test_route_inputs_follow_ordered_guards_and_settable_dimensions(self):
        from gateway.workflow_openapi import route_inputs

        fields = _execution_fixture()["field_rows"]
        cases = [
            ("image.generate", {}, ["count", "size_body"]),
            ("image.generate", {"n": {}}, ["count", "size_body"]),
            ("image.generate", {"width": {}}, ["count", "size_body"]),
            ("image.generate", {"height": {}}, ["count", "size_body"]),
            ("image.generate", {"width": {}, "height": {}}, ["count"]),
            ("image.generate", {"width": {}, "height": {"settable": False}},
             ["count", "size_body"]),
            ("image.edit", {}, ["count", "size_form"]),
            ("audio.generate", {}, []),
            ("mesh.generate", {}, ["size_body"]),
            ("video.generate", {}, ["duration", "size_body"]),
            ("video.generate", {"length": {}}, ["size_body"]),
            ("video.generate", {"length": {"settable": False}}, ["duration", "size_body"]),
            ("video.generate", {"width": {}, "height": {}, "length": {}}, []),
        ]
        for intent, inputs, refs in cases:
            with self.subTest(intent=intent, inputs=inputs):
                record = self.record(intent)
                record.contract["inputs"] = {
                    name: {"type": "number", **spec} for name, spec in inputs.items()
                }
                self.assertEqual(route_inputs(record), [fields[ref]["value"] for ref in refs])

    def test_route_inputs_use_unfamiliar_data_rows_and_return_fresh_fields(self):
        from gateway.workflow_openapi import route_inputs

        for intent in ("unfamiliar.render", "another.compose"):
            document = self.document()
            execution = document["execution"]
            execution["field_rows"]["custom"] = {
                "unless_all_inputs": ["alpha", "beta"],
                "value": {"name": "choice", "type": "string", "description": "Configured.",
                          "in": "form", "field": "choice"},
            }
            execution["intents"][intent] = {
                "model_prefix": "", "route_inputs": ["custom", "count"],
                "sync": "generated_image", "async": None, "native_alternate": None,
            }
            record = self.record(intent)
            record.contract["inputs"] = {"alpha": {"type": "number"}}
            maps = self.load_document(document)
            expected = [execution["field_rows"][ref]["value"] for ref in ("custom", "count")]
            with patch.object(workflow_request_maps, "_MAPS", maps):
                first = route_inputs(record)
                self.assertEqual(first, expected)
                first[0]["description"] = "Changed by caller."
                first.reverse()
                self.assertEqual(route_inputs(record), expected)
                self.assertEqual(maps.execution.field_rows["custom"]["value"]["description"],
                                 "Configured.")
                record.contract["inputs"]["beta"] = {"type": "number"}
                self.assertEqual(route_inputs(record), expected[1:])
            # Change only row data: order, placement and guard determine the new projection.
            execution["intents"][intent]["route_inputs"] = ["count", "custom"]
            execution["field_rows"]["custom"]["unless_all_inputs"] = []
            execution["field_rows"]["custom"]["value"]["in"] = "body"
            with patch.object(workflow_request_maps, "_MAPS", self.load_document(document)):
                self.assertEqual(route_inputs(record), [
                    execution["field_rows"][ref]["value"] for ref in ("count", "custom")
                ])

    def test_route_inputs_missing_row_has_no_templated_fields(self):
        from gateway.workflow_openapi import route_inputs

        self.assertEqual(route_inputs(self.record("unregistered.render")), [])
        document = self.document()
        del document["execution"]["intents"]["image.generate"]
        with patch.object(workflow_request_maps, "_MAPS", self.load_document(document)):
            self.assertEqual(route_inputs(self.record("image.generate")), [])

    def test_route_inputs_preserve_native_and_recommended_run_suppression(self):
        from gateway.workflow_openapi import route_inputs

        for intent in (*workflow_request_maps._MAPS.execution.intents, "unregistered.render"):
            with self.subTest(intent=intent):
                self.assertEqual(route_inputs(self.record(intent, native="/native-fixture")), [])
        fields = _execution_fixture()["field_rows"]
        for typical in (None, 0, 29.999, 30, 30.001, 60):
            with self.subTest(typical=typical):
                record = self.record("video.edit")
                record.contract["typical_seconds"] = typical
                expected = [] if typical is not None and typical <= 30 else [
                    fields[ref]["value"] for ref in ("duration", "size_body")
                ]
                self.assertEqual(route_inputs(record), expected)

    def test_unfamiliar_intent_prefix_and_alternate_come_only_from_data(self):
        from gateway.workflow_openapi import _execution

        for intent in ("unfamiliar.render", "another.compose"):
            document = self.document()
            document["execution"]["intents"][intent] = {
                "model_prefix": "custom-", "route_inputs": [], "sync": None, "async": None,
                "native_alternate": {
                    "method": "POST", "path": "/alternate-fixture",
                    "model": {"from": "model"}, "note": "data-defined alternate",
                },
            }
            record = self.record(intent, native="/native-fixture")
            before = copy.deepcopy(record.contract)
            for prefix, path in (("custom-", "/alternate-fixture"), ("changed-", "/another-alternate")):
                document["execution"]["intents"][intent]["model_prefix"] = prefix
                document["execution"]["intents"][intent]["native_alternate"]["path"] = path
                maps = self.load_document(document, records=[record])
                with patch.object(workflow_request_maps, "_MAPS", maps):
                    execution = _execution(record, 1)
                    self.assertEqual(execution["sync"]["alternate"], {
                        "method": "POST", "path": path, "model": prefix + "fixture",
                        "note": "data-defined alternate",
                    })
                    self.assertNotIn("model", execution["sync"])
                    self.assertIsNone(execution["async"])
                    self.assertEqual(execution["recommended"], "sync")
            self.assertEqual(record.contract, before)


    def test_execution_projection_follows_added_and_changed_data_rows(self):
        from gateway.workflow_openapi import _execution, workflow_catalog

        for intent in ("unfamiliar.render", "another.compose"):
            with self.subTest(intent=intent):
                document = self.document()
                record = self.record(intent)
                record.contract["inputs"] = {"prompt": {"type": "string"}}
                before = self.load_document(document)
                with patch.object(workflow_request_maps, "_MAPS", before):
                    self.assertEqual(_execution(record, 1),
                                     {"recommended": "sync", "sync": None, "async": None})
                execution = document["execution"]
                execution["sync_templates"]["fixture_sync"] = {
                    "method": "POST", "path": "/fixture/first",
                    "content_type": "application/json", "model": {"from": "model"},
                    "fields": {"from": "fields", "mode": "sync"},
                    "output": "fixture response", "response": {"kind": "json"},
                }
                execution["async_templates"]["fixture_async"] = copy.deepcopy(
                    execution["async_templates"]["generated_image_job"])
                execution["async_templates"]["fixture_async"]["submit"]["body"]["intent"] = intent
                execution["intents"][intent] = {
                    "model_prefix": "fixture-", "route_inputs": [], "sync": "fixture_sync",
                    "async": "fixture_async", "native_alternate": None,
                }
                document["maps"].insert(0, {
                    "select": {"intent": intent, "mode": "any"},
                    "inputs": {"prompt": {"in": "body", "field": "message"}},
                    "default": {"in": "extension"},
                })
                for path, kind, prefix in (
                    ("/fixture/first", "json", "fixture-"),
                    ("/fixture/second", "binary", "changed-"),
                ):
                    execution["sync_templates"]["fixture_sync"]["path"] = path
                    execution["sync_templates"]["fixture_sync"]["response"]["kind"] = kind
                    execution["intents"][intent]["model_prefix"] = prefix
                    maps = self.load_document(document, records=[record])
                    with patch.object(workflow_request_maps, "_MAPS", maps):
                        projected = _execution(record, None)
                        self.assertEqual(projected["sync"], {
                            "method": "POST", "path": path, "content_type": "application/json",
                            "model": prefix + "fixture",
                            "fields": {"prompt": {"in": "body", "field": "message"}},
                            "output": "fixture response", "response": {"kind": kind},
                        })
                        self.assertEqual(projected["async"]["submit"]["body"]["intent"], intent)
                        self.assertEqual(projected["async"]["submit"]["model"], prefix + "fixture")
                        self.assertEqual(projected["async"]["fields"], projected["sync"]["fields"])
                        self.assertEqual(projected["recommended"], "async")
                        self.assertEqual(workflow_catalog([record])[0]["execution"], projected)

    def test_execution_projection_uses_declared_modes_and_preserves_recommendation(self):
        from gateway import workflow_openapi

        for intent, row in workflow_request_maps._MAPS.execution.intents.items():
            record = self.record(intent)
            for typical in (None, 0, 29.999, 30, 30.001, 120):
                with self.subTest(intent=intent, typical=typical), patch.object(
                    workflow_openapi, "request_mapping",
                    side_effect=lambda _record, mode: {"mode": mode},
                ) as mapping:
                    projected = workflow_openapi._execution(record, typical)
                    modes = ["sync"] if row.sync is not None else []
                    if row.async_ is not None:
                        modes.append(row.async_["fields"]["mode"])
                    self.assertEqual([call.args[1] for call in mapping.call_args_list], modes)
                    preferred = "sync" if (
                        typical is not None and typical <= 30 and row.sync is not None
                    ) else ("async" if row.async_ is not None else "sync")
                    self.assertEqual(projected["recommended"], preferred)
                    if projected["async"] is not None:
                        self.assertEqual(projected["async"]["poll"]["path"], row.async_["poll"]["path"])
                    native = self.record(intent, native="/native-fixture")
                    mapping.reset_mock()
                    native_projection = workflow_openapi._execution(native, typical)
                    self.assertEqual([call.args[1] for call in mapping.call_args_list], ["sync"])
                    self.assertEqual(native_projection["sync"]["path"], "/native-fixture")
                    self.assertIsNone(native_projection["async"])
                    self.assertNotIn("model", native_projection["sync"])

    def test_execution_projection_does_not_share_mutable_route_data(self):
        from gateway.workflow_openapi import _execution

        for intent in workflow_request_maps._MAPS.execution.intents:
            record = self.record(intent)
            with self.subTest(intent=intent):
                expected = _execution(record, 1)
                first = _execution(record, 1)
                if first["sync"] is not None:
                    first["sync"]["response"]["kind"] = "changed"
                    first["sync"]["fields"]["changed"] = {}
                if first["async"] is not None:
                    first["async"]["statuses"].append("changed")
                    first["async"]["terminal"].clear()
                    first["async"]["fields"]["changed"] = {}
                    first["async"]["submit"].setdefault("headers", {})["Prefer"] = "changed"
                self.assertEqual(_execution(record, 1), expected)

    def test_execution_templates_shadow_existing_routes_without_changing_goldens(self):
        from gateway.workflow_openapi import _execution

        maps = workflow_request_maps._MAPS.execution
        for intent in maps.intents:
            for native in (None, "/native-fixture"):
                record = self.record(intent, native=native)
                with self.subTest(intent=intent, native=native):
                    execution = _execution(record, 1)
                    self.assertEqual(maps.render_sync(record, native=bool(native)), execution["sync"])
                    if not native:
                        self.assertEqual(maps.render_async(record), execution["async"])
        registry = WorkflowRegistry.from_directory(MANIFESTS)
        self.assertEqual(registry.errors, [])
        maps.validate_records(registry.records())

    def test_execution_rendering_preserves_mapping_modes_and_literal_placeholders(self):
        from gateway import workflow_openapi

        maps = workflow_request_maps._MAPS.execution
        for intent, row in maps.intents.items():
            record = self.record(intent)
            with self.subTest(intent=intent), patch.object(
                workflow_openapi, "request_mapping", side_effect=lambda _record, mode: {"mode": mode}
            ) as mapping:
                sync = maps.render_sync(record)
                async_ = maps.render_async(record)
                expected = (["sync"] if row.sync is not None else [])
                if row.async_ is not None:
                    expected.append(row.async_["fields"]["mode"])
                    self.assertEqual(async_["poll"]["path"], row.async_["poll"]["path"])
                self.assertEqual([call.args[1] for call in mapping.call_args_list], expected)
                if sync:
                    self.assertEqual(sync["fields"], {"mode": "sync"})

    def test_execution_rows_are_deeply_frozen_and_renderings_are_independent(self):
        maps = workflow_request_maps._MAPS.execution
        with self.assertRaises(TypeError):
            maps.field_rows["count"]["value"]["name"] = "changed"
        with self.assertRaises(TypeError):
            maps.intents["image.edit"].native_alternate["model"]["from"] = "changed"
        record = self.record("mesh.generate")
        first = maps.render_async(record)
        first["statuses"].append("changed")
        first["terminal"].clear()
        first["submit"]["headers"]["Prefer"] = "changed"
        first["fields"]["changed"] = {}
        second = maps.render_async(record)
        self.assertNotIn("changed", second["statuses"])
        self.assertTrue(second["terminal"])
        self.assertEqual(second["submit"]["headers"], {"Prefer": "respond-async"})
        self.assertNotIn("changed", second["fields"])
        record = self.record("image.edit", native="/native-fixture")
        first = maps.render_sync(record, native=True)
        first["response"]["kind"] = "changed"
        first["alternate"]["model"] = "changed"
        second = maps.render_sync(record, native=True)
        self.assertEqual(second["response"], {"kind": "json"})
        self.assertEqual(second["alternate"]["model"], "fixture")
        left = maps.render_async(self.record("video.generate"))
        right_record = self.record("video.edit")
        right_record.id = "video.edit.other"
        right = maps.render_async(right_record)
        self.assertEqual(left["submit"]["model"], "video-fixture")
        self.assertEqual(right["submit"]["model"], "video-other")


if __name__ == "__main__":
    unittest.main()


def test_workflow_input_schema_copies_mutable_constraints_independently():
    from types import SimpleNamespace
    from gateway.workflow_openapi import workflow_inputs_schema

    record = SimpleNamespace(id="image.generate.copy-test", contract={"inputs": {
        "mode": {"type": "string", "enum": ["fast", "quality"], "default": "fast"},
    }})
    first = workflow_inputs_schema(record)
    first["properties"]["mode"]["enum"].append("invalid")
    second = workflow_inputs_schema(record)
    assert record.contract["inputs"]["mode"]["enum"] == ["fast", "quality"]
    assert second["properties"]["mode"]["enum"] == ["fast", "quality"]
