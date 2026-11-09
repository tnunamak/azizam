from __future__ import annotations
from contextlib import nullcontext

import json
import textwrap
import unittest
import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import media_jobs
from gateway.capabilities import Capability, unavailable_capability
from gateway.discovery import WorkflowRegistry
from gateway.user_runtime import UserPortalError
from gateway.workflow_openapi import augment_openapi
from tests.support.registry_provider import provider_of
from tests.support.workflow_docs import STANDARD_FACTS
from gateway.workflows_catalog import (
    _workflow_description,
    build_workflows_catalog,
    create_workflows_catalog_router,
)


IMAGE_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.generate.simple
    intent: image.generate
    capability: image
    display_name: Simple Image
    description: Test image workflow.
    typical_seconds: 9
    inputs:
      prompt:
        type: string
        required: true
      width:
        type: integer
        minimum: 64
        maximum: 2048
        default: 1024
      seed:
        type: integer
        minimum: 0
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/simple.json
        map:
          prompt:
            - node: "1"
              input: text
    """
).strip()


IMAGE_EXTENSION_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.generate.extension
    intent: image.generate
    capability: image
    display_name: Extension Image
    typical_seconds: 9
    inputs:
      prompt:
        type: string
        required: true
      width:
        type: integer
        minimum: 64
        maximum: 2048
        default: 1024
      height:
        type: integer
        minimum: 64
        maximum: 2048
        default: 768
      steps:
        type: integer
        minimum: 1
        maximum: 20
        default: 8
      seed:
        type: integer
        minimum: 0
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/extension.json
        map:
          prompt:
            - node: "1"
              input: text
    """
).strip()


SLOW_IMAGE_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.generate.slow
    intent: image.generate
    capability: image
    display_name: Slow Image
    typical_seconds: 60
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/slow.json
        map: {}
    """
).strip()


IMAGE_EDIT_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: image.edit.photo
    intent: image.edit
    capability: image
    display_name: Photo Edit
    typical_seconds: 8
    inputs:
      prompt:
        type: string
        required: true
      source_image:
        type: asset
        accepts: ["image/*"]
        required: true
      strength:
        type: number
        minimum: 0
        maximum: 1
        default: 0.5
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/edit.json
        map: {}
    """
).strip()


AUDIO_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: audio.generate.sfx
    intent: audio.generate
    capability: sfx
    display_name: SFX
    description: Test audio workflow.
    typical_seconds: 2
    inputs:
      prompt:
        type: string
        required: true
      duration:
        type: number
        minimum: 1
        maximum: 120
        default: 10
    outputs:
      audio:
        media_type: audio/flac
    bindings:
      comfyui:
        workflow: workflows/audio/sfx.json
        map: {}
    """
).strip()


VIDEO_MANIFEST = textwrap.dedent(
    """
    schema_version: 2
    version: "0.1.0"
    id: video.generate.simple
    intent: video.generate
    capability: video
    display_name: Simple Video
    description: Test video workflow.
    typical_seconds: 45
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      video:
        media_type: video/mp4
    bindings:
      comfyui:
        workflow: workflows/videos/simple.json
        map:
          prompt:
            - node: "1"
              input: text
    """
).strip()


def _registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.add_manifest(yaml.safe_load(IMAGE_MANIFEST), source_path="image.yml", facts=STANDARD_FACTS)
    registry.add_manifest(yaml.safe_load(IMAGE_EXTENSION_MANIFEST), source_path="extension.yml", facts=STANDARD_FACTS)
    registry.add_manifest(yaml.safe_load(SLOW_IMAGE_MANIFEST), source_path="slow.yml", facts=STANDARD_FACTS)
    registry.add_manifest(yaml.safe_load(IMAGE_EDIT_MANIFEST), source_path="edit.yml", facts=STANDARD_FACTS)
    registry.add_manifest(yaml.safe_load(AUDIO_MANIFEST), source_path="audio.yml", facts=STANDARD_FACTS)
    registry.add_manifest(yaml.safe_load(VIDEO_MANIFEST), source_path="video.yml", facts=STANDARD_FACTS)
    return registry


def _aliases() -> dict:
    return {
        "images": {"stable": [{"alias": "image", "resolves_to": "image.generate.simple"}]},
        "videos": {"stable": [{"alias": "video", "resolves_to": "video-simple"}]},
    }


class WorkflowsCatalogTests(unittest.TestCase):
    def test_catalog_includes_manifest_license_verbatim(self) -> None:
        manifest = yaml.safe_load(IMAGE_MANIFEST)
        manifest["license"] = "Community terms: Not For Use in restricted regions"
        registry = WorkflowRegistry()
        registry.add_manifest(manifest, source_path="licensed.yml", facts=STANDARD_FACTS)
        payload = build_workflows_catalog(
            records=registry.records(),
            capabilities=[Capability(intent="image.generate", backend="comfyui", workflow=manifest["id"])],
        )
        self.assertEqual(payload["workflows"][0]["license"], manifest["license"])

    def test_catalog_omits_license_when_manifest_has_none(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple")],
        )
        self.assertNotIn("license", payload["workflows"][0])

    def test_summary_is_preferred_without_leaking_research_notes(self) -> None:
        manifest = yaml.safe_load(IMAGE_MANIFEST)
        manifest.update(
            summary="Make a detailed picture from a written description of your scene.",
            purpose="Make a picture",
            description="Reddit community research and a long bake-off.",
        )
        registry = WorkflowRegistry()
        registry.add_manifest(manifest, source_path="summary.yml", facts=STANDARD_FACTS)
        payload = build_workflows_catalog(
            records=registry.records(),
            capabilities=[Capability(intent="image.generate", backend="comfyui", workflow=manifest["id"])],
        )
        self.assertEqual(payload["workflows"][0]["description"], manifest["summary"])
        self.assertEqual(payload["workflows"][0]["purpose"], manifest["purpose"])
        self.assertNotIn("technical_notes", payload["workflows"][0])
        self.assertNotIn(manifest["description"], json.dumps(payload))

    def test_openapi_summary_replaces_research_in_all_descriptions(self) -> None:
        manifest = yaml.safe_load(VIDEO_MANIFEST)
        manifest.update(id="video.edit.summary", intent="video.edit",
                        summary="Edit a video.", description="Reddit research must remain private.")
        registry = WorkflowRegistry()
        registry.add_manifest(manifest, source_path="summary.yml", facts=STANDARD_FACTS)
        spec = augment_openapi({}, registry.records())
        self.assertEqual(spec["x-azizam-workflows"][0]["description"], manifest["summary"])
        descriptions = [
            schema["description"] for schema in spec["components"]["schemas"].values()
            if schema.get("x-azizam-workflow-id") == manifest["id"]
        ]
        # The full input schema, the run form and the video route's
        # extension-only schema all carry the summary.
        self.assertEqual(descriptions, [manifest["summary"]] * 3)
        operation = next(iter(spec["paths"].values()))["post"]
        self.assertEqual(operation["description"], manifest["summary"])
        self.assertNotIn(manifest["description"], json.dumps(spec))

    def test_openapi_keeps_description_when_summary_is_absent(self) -> None:
        registry = WorkflowRegistry()
        manifest = yaml.safe_load(IMAGE_MANIFEST)
        registry.add_manifest(manifest, source_path="legacy.yml", facts=STANDARD_FACTS)
        spec = augment_openapi({}, registry.records())
        self.assertEqual(spec["x-azizam-workflows"][0]["description"], manifest["description"])

    def test_manifest_capability_controls_audio_workflow_group(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(
                    intent="audio.generate",
                    backend="comfyui",
                    workflow="audio.generate.sfx",
                    output_modalities=("audio",),
                    health="healthy",
                    lifecycle="warm",
                )
            ],
        )
        workflow = next(row for row in payload["workflows"] if row["id"] == "audio.generate.sfx")
        self.assertEqual(workflow["capability"], "sfx")
        self.assertEqual(workflow["description"], "Sound effects from a description.")
        self.assertEqual(workflow["technical_notes"], "Test audio workflow.")

    def test_catalog_descriptions_stay_browseable_and_preserve_manifest_notes(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple"),
                Capability(intent="audio.generate", backend="comfyui", workflow="audio.generate.sfx"),
                Capability(intent="video.generate", backend="comfyui", workflow="video.generate.simple"),
            ],
        )

        for workflow in payload["workflows"]:
            self.assertLessEqual(len(workflow["description"].split()), 8, workflow["id"])

        image = next(row for row in payload["workflows"] if row["id"] == "image.generate.simple")
        self.assertEqual(image["description"], "Generate or edit images from your prompts.")
        self.assertEqual(image["technical_notes"], "Test image workflow.")
        self.assertIsNone(image["purpose"])

    def test_ace_step_offers_no_denoise_because_its_graphs_start_from_an_empty_latent(self) -> None:
        # Text to music starts from an empty latent with no source audio, so a denoise below 1 would change nothing.
        # The manifests do not offer the setting, and each graph keeps its sampler at denoise 1.
        manifest_root = Path(__file__).resolve().parents[1] / "manifests" / "workflows"
        registry = WorkflowRegistry.from_directory(manifest_root)
        self.assertEqual(registry.errors, [])
        records = [record for record in registry.records() if "ace-step" in record.id]
        self.assertEqual(len(records), 1)
        payload = build_workflows_catalog(
            records=records,
            capabilities=[
                Capability(intent=record.intent, backend=record.supported_backends[0], workflow=record.id)
                for record in records
            ],
        )
        self.assertEqual(len(payload["workflows"]), 1)
        for row in payload["workflows"]:
            self.assertNotIn("denoise", [item["name"] for item in row["inputs"]], row["id"])
        for record in records:
            graph_path = Path(__file__).resolve().parents[1] / record.manifest["bindings"]["comfyui"]["workflow"]
            graph = json.loads(graph_path.read_text())
            sampler = next(
                node for node in graph.values()
                if isinstance(node, dict) and node.get("class_type") == "KSampler"
            )
            self.assertEqual(sampler["inputs"]["denoise"], 1, record.id)

    def test_every_exposed_builtin_workflow_has_a_distinct_short_purpose(self) -> None:
        manifest_root = Path(__file__).resolve().parents[1] / "manifests" / "workflows"
        registry = WorkflowRegistry.from_directory(manifest_root)
        self.assertEqual(registry.errors, [])
        records = registry.records()
        payload = build_workflows_catalog(
            records=records,
            capabilities=[
                Capability(
                    intent=record.intent,
                    backend=record.supported_backends[0],
                    workflow=record.id,
                )
                for record in records
            ],
        )

        purposes = [row.get("purpose", "").strip() for row in payload["workflows"]]
        self.assertEqual(len(payload["workflows"]), len(records))
        self.assertTrue(all(purposes), "every exposed workflow needs a purpose")
        self.assertTrue(
            all(len(purpose.split()) <= 8 for purpose in purposes),
            "workflow purposes must use at most 8 words",
        )
        self.assertEqual(
            len({purpose.casefold() for purpose in purposes}),
            len(purposes),
            "workflow purposes must be distinct",
        )
        self.assertTrue(
            all(
                row["description"] == next(
                    (" ".join(record.manifest["summary"].split()) if record.manifest.get("summary") else row["purpose"])
                    for record in records if record.id == row["id"]
                )
                for row in payload["workflows"]
            ),
            "summary or purpose should replace capability fallback descriptions",
        )
        face = next(
            row for row in payload["workflows"] if row["id"] == "image.analyze.face-likeness"
        )
        face_record = next(record for record in records if record.id == face["id"])
        if face_record.manifest.get("summary"):
            self.assertNotIn("technical_notes", face)
        else:
            self.assertEqual(face["technical_notes"], face_record.manifest["description"])
            self.assertNotIn("ArcFace", face["technical_notes"])

    def test_asset_inputs_and_outputs_carry_the_meaning_the_manifest_declares(self) -> None:
        manifest_root = Path(__file__).resolve().parents[1] / "manifests" / "workflows"
        registry = WorkflowRegistry.from_directory(manifest_root)
        records = registry.records()
        payload = build_workflows_catalog(
            records=records,
            capabilities=[
                Capability(intent=r.intent, backend=r.supported_backends[0], workflow=r.id)
                for r in records
            ],
        )
        face = next(row for row in payload["workflows"] if row["id"] == "image.analyze.face-likeness")
        inputs = {row["name"]: row for row in face["inputs"]}
        # An asset is a media file, not "text": the flag and the intent's media kind survive.
        self.assertTrue(inputs["reference"]["asset"])
        self.assertEqual(inputs["reference"]["media_type"], "image/*")
        self.assertTrue(inputs["candidate"]["asset"])
        self.assertNotIn("asset", inputs["people"])
        self.assertNotIn("media_type", inputs["people"])
        # The manifest's own short names reach the page.
        self.assertEqual(inputs["reference"]["label"], "a face photo")
        self.assertEqual(inputs["candidate"]["label"], "a photo to check")
        # ...and so does its one-line help, the plain words Try shows under the field.
        self.assertEqual(inputs["candidate"]["help"], "The picture to compare with the reference.")
        edit = next(row for row in payload["workflows"] if row["id"] == "image.edit.qwen-image-2.1-edit")
        edit_inputs = {row["name"]: row for row in edit["inputs"]}
        self.assertEqual(edit_inputs["cfg"]["label"], "prompt strength")
        # The route's own options carry plain names too ("n" is never a label).
        self.assertEqual(edit_inputs["n"]["label"], "number of results")
        self.assertNotIn("Reddit", json.dumps(edit_inputs))
        self.assertEqual(
            edit["example_request"]["files"],
            {"source_image": {"input": "source_image", "field": "image[]",
                              "required": True, "media_type": "image/*"}},
        )
        self.assertEqual(face["outputs"], [
            {"name": "report", "media_type": "application/json", "label": "a likeness score"},
        ])
        # A mixed manifest keeps each asset's declared kind.
        scail = next(row for row in payload["workflows"] if row["id"].endswith("scail-2-replace"))
        kinds = {row["name"]: row["media_type"] for row in scail["inputs"] if row.get("asset")}
        self.assertEqual(kinds, {"reference_image": "image/*", "source_video": "video/*"})

    def test_catalog_description_rejects_internal_tokens_files_and_license_notes(self) -> None:
        for description in (
            "Edit int8_convrot images for this request.",
            "Render with `TextEncode` and an image input.",
            "Use the weights stored in model.safetensors.",
            "Render this with Apache-2.0 and version v2.1.",
            "Create a simple image from an instruction with a clear and colorful scene for the user.",
        ):
            with self.subTest(description=description):
                self.assertEqual(
                    _workflow_description(description, "image"),
                    "Generate or edit images from your prompts.",
                )

    def test_catalog_includes_ui_contract_fields(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            aliases=_aliases(),
            capabilities=[
                Capability(
                    intent="image.generate",
                    backend="comfyui",
                    workflow="image.generate.simple",
                    output_modalities=("image",),
                    execution="job",
                    health="healthy",
                    lifecycle="warm",
                )
            ],
            base_url="http://testserver",
        )

        self.assertEqual(payload["object"], "workflow.catalog")
        self.assertEqual(payload["groups"][0]["capability"], "image")
        image = payload["workflows"][0]
        self.assertEqual(image["id"], "image.generate.simple")
        self.assertEqual(image["capability"], "image")
        self.assertEqual(image["title"], "Simple Image")
        self.assertEqual(image["aliases"], ["image"])
        self.assertEqual(image["engine_binding"], "comfyui")
        self.assertTrue(image["availability"]["available"])
        self.assertEqual(image["availability"]["status"], "ready")
        self.assertIsNone(image["native_route"])
        self.assertIsNone(image["native_passthrough_route"])
        self.assertEqual(image["example_request"]["path"], "/v1/images/generations")
        self.assertEqual(image["discovery_url"], "http://testserver/gateway/v1/workflows/image.generate.simple")
        self.assertEqual(image["recent_jobs_url"], "/me/api/media?workflow=image.generate.simple")
        self.assertEqual(set(image["examples"]), {"curl", "python", "javascript"})
        self.assertIn("http://testserver/v1/images/generations", image["examples"]["curl"])
        self.assertIn(" -d '{", image["examples"]["curl"])
        width = next(item for item in image["inputs"] if item["name"] == "width")
        self.assertEqual(width["type"], "integer")
        self.assertEqual(width["default"], 1024)
        self.assertIs(width["accepted"], True)
        self.assertNotIn("enum", width)

    def test_json_example_body_uses_route_mapping_and_extensions(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(
                    intent="image.generate",
                    backend="comfyui",
                    workflow="image.generate.extension",
                    health="unknown",
                    lifecycle="unknown",
                )
            ],
            base_url="http://testserver",
        )

        workflow = payload["workflows"][0]
        body = workflow["example_request"]["body"]
        self.assertEqual(body["model"], "extension")
        self.assertEqual(body["prompt"], "A small teal fish mark on a clean white card.")
        self.assertEqual(body["size"], "1024x768")
        self.assertEqual(body["n"], 1)
        self.assertNotIn("width", body)
        self.assertNotIn("height", body)
        self.assertEqual(
            body["extensions"]["com.azizam.workflow"],
            {"steps": 8, "seed": 0},
        )
        self.assertEqual(workflow["availability"]["status"], "configured")
        self.assertFalse(workflow["availability"]["measured"])

    def test_async_media_job_example_wraps_sync_request_body(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(intent="image.generate", backend="comfyui", workflow="image.generate.slow")
            ],
        )

        request = payload["workflows"][0]["example_request"]
        self.assertEqual(request["path"], "/gateway/v1/media/jobs")
        self.assertEqual(request["content_type"], "application/json")
        self.assertEqual(request["body"]["intent"], "image.generate")
        self.assertEqual(request["body"]["request"]["model"], "slow")
        self.assertEqual(request["body"]["request"]["prompt"], "A small teal fish mark on a clean white card.")

    def test_multipart_example_declares_form_and_files(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(intent="image.edit", backend="comfyui", workflow="image.edit.photo")
            ],
            base_url="http://testserver",
        )

        request = payload["workflows"][0]["example_request"]
        self.assertEqual(request["path"], "/v1/images/edits")
        self.assertEqual(request["content_type"], "multipart/form-data")
        self.assertEqual(request["form"]["model"], "photo")
        self.assertEqual(request["form"]["prompt"], "A small teal fish mark on a clean white card.")
        self.assertEqual(request["form"]["extensions"], '{"com.azizam.workflow":{"strength":0.5}}')
        # The catalog names the input and kind of each file part; a made-up
        # path appears only inside the code examples, never as a value a
        # screen could show as helper text.
        self.assertEqual(
            request["files"],
            {"source_image": {"input": "source_image", "field": "image[]",
                              "required": True, "media_type": "image/*"}},
        )
        self.assertNotIn("./", json.dumps(request["files"]))
        self.assertIn(" -F 'prompt=", request["curl"])
        self.assertIn(" -F 'image[]=@./source-image.png'", request["curl"])
        self.assertIn("await readFile(\"./source-image.png\")", request["javascript"])
        self.assertIn("new Blob", request["javascript"])

    def test_audio_example_snippets_treat_response_as_binary(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(intent="audio.generate", backend="comfyui", workflow="audio.generate.sfx")
            ],
        )

        request = payload["workflows"][0]["example_request"]
        self.assertEqual(request["path"], "/v1/audio/generate")
        self.assertEqual(request["content_type"], "application/json")
        self.assertIn("workflow-output.bin", request["python"])
        self.assertIn("writeFile", request["javascript"])
        self.assertIn("response.arrayBuffer()", request["javascript"])
        self.assertIn("--output workflow-output.bin", request["curl"])

    def test_video_async_example_uses_async_mapping_and_video_model_prefix(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(intent="video.generate", backend="comfyui", workflow="video.generate.simple")
            ],
        )

        request = payload["workflows"][0]["example_request"]
        self.assertEqual(request["path"], "/v1/videos")
        self.assertEqual(request["body"]["model"], "video-simple")
        self.assertEqual(request["body"]["prompt"], "A small teal fish mark on a clean white card.")
        self.assertIn("seconds", request["body"])
        self.assertIn("size", request["body"])

    def test_asset_inputs_are_flagged_and_exampled_as_media(self) -> None:
        manifest_root = Path(__file__).resolve().parents[1] / "manifests" / "workflows"
        registry = WorkflowRegistry.from_directory(manifest_root)
        records = [r for r in registry.records() if r.id == "video.generate.ltx-2.5-i2v"]
        payload = build_workflows_catalog(
            records=records,
            capabilities=[
                Capability(intent=r.intent, backend=r.supported_backends[0], workflow=r.id)
                for r in records
            ],
        )
        workflow = payload["workflows"][0]
        inputs = {row["name"]: row for row in workflow["inputs"]}
        # The row type stays the JSON type; the flag is how a UI finds media.
        self.assertEqual(inputs["image"]["type"], "string")
        self.assertIs(inputs["image"]["asset"], True)
        self.assertEqual(inputs["image"]["request_field"], "input_reference")
        self.assertNotIn("asset", inputs["prompt"])
        self.assertNotIn("asset", inputs["negative_prompt"])
        # The example shows media as media, not as the word "value".
        body = workflow["example_request"]["body"]
        self.assertTrue(body["input_reference"].startswith("data:image/"))

    def test_measured_unavailable_capability_is_not_configured_available(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                unavailable_capability(
                    intent="image.generate",
                    backend="comfyui",
                    reason="backend stopped",
                    workflow="image.generate.simple",
                    lifecycle="stopped",
                )
            ],
        )

        availability = payload["workflows"][0]["availability"]
        self.assertEqual(availability["status"], "unavailable")
        self.assertFalse(availability["available"])
        self.assertTrue(availability["measured"])

    def test_mixed_unhealthy_and_unknown_backend_is_configured_not_ready(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                unavailable_capability(
                    intent="image.generate",
                    backend="comfyui-a",
                    reason="backend unhealthy",
                    workflow="image.generate.simple",
                    lifecycle="stopped",
                ),
                Capability(
                    intent="image.generate",
                    backend="comfyui-b",
                    workflow="image.generate.simple",
                    health="unknown",
                    lifecycle="unknown",
                ),
            ],
        )

        availability = payload["workflows"][0]["availability"]
        self.assertEqual(availability["status"], "configured")
        self.assertTrue(availability["available"])
        self.assertFalse(availability["measured"])

    def test_member_catalog_omits_policy_denied_workflows(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            capabilities=[
                Capability(
                    intent="image.generate",
                    backend="comfyui",
                    workflow="image.generate.simple",
                    policy_available=True,
                ),
                replace(
                    unavailable_capability(
                        intent="video.generate",
                        backend="comfyui",
                        reason="not in member access plan",
                        workflow="video.generate.simple",
                    ),
                    policy_available=False,
                ),
            ],
        )

        self.assertEqual([item["id"] for item in payload["workflows"]], ["image.generate.simple"])

    def test_owner_catalog_can_include_all_policy_allowed_workflows(self) -> None:
        payload = build_workflows_catalog(
            records=_registry().records(),
            aliases=_aliases(),
            capabilities=[
                Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple"),
                Capability(intent="video.generate", backend="comfyui", workflow="video.generate.simple"),
            ],
        )

        self.assertEqual(
            [item["id"] for item in payload["workflows"]],
            ["image.generate.simple", "video.generate.simple"],
        )
        video = next(item for item in payload["workflows"] if item["id"] == "video.generate.simple")
        self.assertEqual(video["aliases"], ["video"])

    def test_router_returns_401_from_auth_provider(self) -> None:
        def provider(_request):
            raise UserPortalError("not_authenticated", "Sign in", status_code=401)

        app = FastAPI()
        app.include_router(create_workflows_catalog_router(provider))
        response = TestClient(app).get("/me/api/workflows")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_proxy_route_scopes_jobs_to_empty_app_list_when_user_has_no_apps(self) -> None:
        import proxy

        with tempfile.TemporaryDirectory() as raw:
            jobs_db = Path(raw) / "media_jobs.db"
            media_jobs.init_db(jobs_db)
            media_jobs.create(
                media_jobs.MediaJob(
                    request_id="foreign",
                    intent="image.generate",
                    workflow="image.generate.simple",
                    app_id="foreign-app",
                    status=media_jobs.STATUS_SUCCEEDED,
                ),
                path=jobs_db,
            )

            with patch.object(proxy, "_oidc_session_from_request", return_value=object()), \
                 patch.object(proxy, "build_user_portal_snapshot", return_value={"authenticated": True, "apps": []}), \
                 patch.object(proxy, "_session_catalog_auth_contexts", return_value=[object()]), \
                 patch.object(proxy, "_gateway_capabilities_for_auth_context", return_value=[
                     Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple")
                 ]), \
                 patch.object(proxy, "build_user_activity_snapshot", return_value={"receipts": []}), \
                 patch.object(proxy.media_jobs, "DB_PATH", jobs_db), \
                 patch.object(proxy, "_gateway_aliases_provider", return_value={}), \
                 patch.object(proxy, "_capabilities_availability_evidence", new=AsyncMock(return_value={})), \
                 patch.dict(proxy.LIVE_PROBES, {}, clear=True), \
                 patch.object(proxy, "_public_base_url", return_value="http://testserver"), \
                 patch.object(proxy, "GATEWAY_REGISTRY_PROVIDER", provider_of(_registry())):
                app = FastAPI()
                app.include_router(create_workflows_catalog_router(proxy._user_workflows_catalog_payload))
                response = TestClient(app).get("/me/api/workflows")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["workflows"][0]["recent_jobs"], [])

    def test_proxy_route_owner_instance_scope_includes_service_and_local_jobs(self) -> None:
        import proxy

        with tempfile.TemporaryDirectory() as raw:
            jobs_db = Path(raw) / "media_jobs.db"
            media_jobs.init_db(jobs_db)
            media_jobs.create(
                media_jobs.MediaJob(
                    request_id="service",
                    intent="image.generate",
                    workflow="image.generate.simple",
                    app_id="service-app",
                    status=media_jobs.STATUS_SUCCEEDED,
                    assets=[{"asset_id": "img_service"}],
                ),
                path=jobs_db,
            )
            media_jobs.create(
                media_jobs.MediaJob(
                    request_id="local",
                    intent="image.generate",
                    workflow="image.generate.simple",
                    status=media_jobs.STATUS_SUCCEEDED,
                    assets=[{"asset_id": "img_local"}],
                ),
                path=jobs_db,
            )

            with patch.object(proxy, "_oidc_session_from_request", return_value=object()), \
                 patch.object(proxy, "build_user_portal_snapshot", return_value={"authenticated": True, "apps": []}), \
                 patch.object(proxy, "_session_catalog_auth_contexts", return_value=[object()]), \
                 patch.object(proxy, "_gateway_capabilities_for_auth_context", return_value=[
                     Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple")
                 ]), \
                 patch.object(proxy, "build_user_activity_snapshot", return_value={"receipts": [], "scope": "instance"}), \
                 patch.object(proxy.media_jobs, "DB_PATH", jobs_db), \
                 patch.object(proxy, "_gateway_aliases_provider", return_value={}), \
                 patch.object(proxy, "_capabilities_availability_evidence", new=AsyncMock(return_value={})), \
                 patch.dict(proxy.LIVE_PROBES, {}, clear=True), \
                 patch.object(proxy, "_public_base_url", return_value="http://testserver"), \
                 patch.object(proxy, "GATEWAY_REGISTRY_PROVIDER", provider_of(_registry())):
                app = FastAPI()
                app.include_router(create_workflows_catalog_router(proxy._user_workflows_catalog_payload))
                response = TestClient(app).get("/me/api/workflows")

        self.assertEqual(response.status_code, 200, response.text)
        recent_jobs = response.json()["workflows"][0]["recent_jobs"]
        self.assertEqual(len(recent_jobs), 2)
        self.assertTrue(all(job["status"] == media_jobs.STATUS_SUCCEEDED for job in recent_jobs))

    def test_proxy_payload_does_not_swallow_media_job_scope_errors(self) -> None:
        import proxy

        with patch.object(proxy, "_oidc_session_from_request", return_value=object()), \
             patch.object(proxy, "build_user_portal_snapshot", return_value={"authenticated": True, "apps": []}), \
             patch.object(proxy, "_session_catalog_auth_contexts", return_value=[object()]), \
             patch.object(proxy, "_gateway_capabilities_for_auth_context", return_value=[
                 Capability(intent="image.generate", backend="comfyui", workflow="image.generate.simple")
             ]), \
             patch.object(proxy, "build_user_activity_snapshot", return_value={"receipts": []}), \
             patch.object(proxy.media_jobs, "recent", side_effect=RuntimeError("scope failed")):
            with self.assertRaises(RuntimeError):
                asyncio.run(proxy._user_workflows_catalog_payload(object()))


class JobRunExampleTests(unittest.TestCase):
    """Execute published clients against a finite, offline response script."""

    def example(self) -> dict:
        manifest = yaml.safe_load(VIDEO_MANIFEST)
        manifest.update(id="video.edit.fixture", intent="video.edit", typical_seconds=1)
        manifest["bindings"]["comfyui"]["map"] = {}
        manifest["inputs"] = {
            "source_video": {"type": "asset", "accepts": ["video/*"], "required": True},
            "multiplier": {"type": "integer", "default": 2},
        }
        registry = WorkflowRegistry()
        registry.add_manifest(manifest, source_path="video-edit.yml", facts=STANDARD_FACTS)
        return build_workflows_catalog(
            records=registry.records(),
            capabilities=[Capability(intent="video.edit", backend="comfyui", workflow="video.edit.fixture")],
            base_url="https://gateway.test",
        )["workflows"][0]["example_request"]

    def steps(self, status: int, terminal: str = "succeeded", asset_status: int = 200) -> list[dict]:
        return [
            {"status": status, "json": {"job_id": "job-1", "status": "running"}},
            {"status": 200, "json": {"job_id": "job-1", "status": terminal}},
            {"status": 200, "json": {"data": [{"url": "https://assets.test/movie?sig=fixture"}]}},
            {"status": asset_status, "text": "movie-bytes"},
        ]

    def test_response_declaration_preserves_multipart_request(self) -> None:
        example = self.example()
        self.assertEqual(example["path"], "/gateway/v1/workflows/video.edit.fixture/run")
        self.assertEqual(example["content_type"], "multipart/form-data")
        self.assertEqual(example["body"], {})
        self.assertEqual(example["form"]["multiplier"], 2)
        self.assertEqual(example["files"]["source_video"]["field"], "source_video")
        for language in ("curl", "python", "javascript"):
            code = example[language]
            self.assertIn("job_id", code)
            self.assertIn("succeeded", code)
            self.assertIn("failed", code)
            self.assertIn("canceled", code)
            self.assertIn("deadline", code)
            self.assertIn("/gateway/v1/media/jobs/", code)
            self.assertIn("/content", code)

    def run_python(self, steps: list[dict], *, expired: bool = False) -> tuple[bytes | None, list[str], Exception | None]:
        import io
        import sys
        import types

        calls, writes = [], {}
        queue = list(steps)

        class Response:
            def __init__(self, step):
                self.step = step
                self.status_code = step["status"]
                self.headers = {"content-type": "application/json"}
                self.content = (json.dumps(step["json"]) if "json" in step else step["text"]).encode()

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self.step["json"]

        def request(method, url, **kwargs):
            calls.append(url)
            if url.startswith("https://assets.test/"):
                self.assertNotIn("Authorization", kwargs.get("headers", {}))
            return Response(queue.pop(0))

        class Output(io.BytesIO):
            def close(self):
                if not self.closed:
                    writes["output"] = self.getvalue()
                super().close()

        def open_file(path, mode="r"):
            return Output() if "w" in mode else io.BytesIO(b"input-video")

        requests = types.SimpleNamespace(request=request, get=lambda url, **kwargs: request("GET", url, **kwargs))
        failure = None
        clock = patch("time.monotonic", side_effect=[0, 601]) if expired else nullcontext()
        code = self.example()["python"]
        with clock, patch.dict(sys.modules, {"requests": requests}), patch("builtins.open", side_effect=open_file), \
             patch("time.sleep", return_value=None):
            try:
                exec(code, {})
            except Exception as exc:
                failure = exc
        return writes.get("output"), calls, failure

    def run_process(self, language: str, steps: list[dict]) -> tuple[bytes | None, list[str], str]:
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source-video.mp4").write_bytes(b"input-video")
            (root / "steps.json").write_text(json.dumps(steps))
            if language == "curl":
                # Only HTTP is faked. Bash and the snippet's JSON decoder execute normally.
                curl = root / "curl"
                curl.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, pathlib, sys\n"
                    "root = pathlib.Path(__file__).parent\n"
                    "steps = json.loads((root / 'steps.json').read_text())\n"
                    "step = steps.pop(0)\n"
                    "(root / 'steps.json').write_text(json.dumps(steps))\n"
                    "args = sys.argv[1:]\n"
                    "url = next(a for a in args if a.startswith('https://'))\n"
                    "with (root / 'calls').open('a') as f: f.write(url + '\\n')\n"
                    "if url.startswith('https://assets.test/') and any('Authorization:' in a for a in args): sys.exit(98)\n"
                    "if step['status'] >= 400 and ('--fail' in args or '--fail-with-body' in args): sys.exit(22)\n"
                    "body = json.dumps(step['json']) if 'json' in step else step['text']\n"
                    "if '--output' in args: pathlib.Path(args[args.index('--output') + 1]).write_text(body)\n"
                    "else: print(body)\n"
                )
                curl.chmod(0o755)
                (root / "sleep").write_text("#!/bin/sh\nexit 0\n")
                (root / "sleep").chmod(0o755)
                command = ["bash", "-c", self.example()["curl"]]
            else:
                bootstrap = (
                    "import { appendFileSync, readFileSync } from 'node:fs';\n"
                    "const steps = JSON.parse(readFileSync('steps.json', 'utf8'));\n"
                    "globalThis.setTimeout = (fn) => { queueMicrotask(fn); return 0; };\n"
                    "globalThis.fetch = async (url, options = {}) => {\n"
                    "  appendFileSync('calls', String(url) + '\\n');\n"
                    "  if (String(url).startsWith('https://assets.test/') && options.headers?.authorization) throw Error('asset bearer leak');\n"
                    "  const step = steps.shift();\n"
                    "  return new Response(step.json ? JSON.stringify(step.json) : step.text, {status: step.status});\n"
                    "};\n"
                )
                (root / "client.mjs").write_text(bootstrap + self.example()["javascript"])
                command = ["node", "client.mjs"]
            result = subprocess.run(
                command, cwd=root, env={**os.environ, "PATH": f"{root}:{os.environ.get('PATH', '')}",
                                       "AZIZAM_API_KEY": "fixture"},
                capture_output=True, text=True, timeout=20, check=False,
            )
            output = root / "workflow-output.bin"
            return (
                output.read_bytes() if output.exists() else None,
                (root / "calls").read_text().splitlines() if (root / "calls").exists() else [],
                (result.stderr or f"exit {result.returncode}") if result.returncode else "",
            )

    def check_clients(self, language: str) -> None:
        runner = self.run_python if language == "python" else lambda steps: self.run_process(language, steps)
        for status, ready in ((202, False), (200, False), (200, True)):
            with self.subTest(language=language, submit_status=status, ready=ready):
                steps = self.steps(status)
                if ready:
                    steps[0]["json"]["status"] = "succeeded"
                    steps.pop(1)
                output, calls, failure = runner(steps)
                self.assertFalse(failure, str(failure))
                self.assertEqual(output, b"movie-bytes")
                self.assertEqual(calls, [
                    "https://gateway.test/gateway/v1/workflows/video.edit.fixture/run",
                    *([] if ready else ["https://gateway.test/gateway/v1/media/jobs/job-1"]),
                    "https://gateway.test/gateway/v1/media/jobs/job-1/content",
                    "https://assets.test/movie?sig=fixture",
                ])
        for terminal in ("failed", "canceled"):
            with self.subTest(language=language, terminal=terminal):
                steps = self.steps(202, terminal)
                steps[1]["json"]["failure"] = {"code": "engine_failed", "message": "fixture reason"}
                output, calls, failure = runner(steps)
                self.assertIsNone(output)
                self.assertIn(terminal, str(failure))
                self.assertIn("fixture reason", str(failure))
                self.assertEqual(len(calls), 2)
        output, calls, failure = runner(self.steps(202, asset_status=403))
        self.assertIsNone(output)
        self.assertTrue(failure)
        self.assertEqual(len(calls), 4)

    def test_python_202_and_200_jobs_poll_then_download(self) -> None:
        self.check_clients("python")

    def test_python_deadline_stops_without_content_or_asset(self) -> None:
        output, calls, failure = self.run_python(self.steps(202), expired=True)
        self.assertIsNone(output)
        self.assertIn("deadline", str(failure))
        self.assertEqual(len(calls), 1)

    def test_curl_202_and_200_jobs_poll_then_download(self) -> None:
        self.check_clients("curl")

    def test_javascript_example_polls_reads_content_then_downloads_the_asset(self) -> None:
        # The gate sandbox has no Node, so this reads the published order of calls. The same
        # fixtures drive the curl and Python examples end to end above.
        code = self.example()["javascript"]
        order = ["await response.json()", "job.job_id", "const deadline", "['failed', 'canceled']", "checkedGet(pollUrl",
                 "checkedGet(contentUrl", "content.data?.[0]?.url", "checkedGet(assetUrl)", "asset.arrayBuffer()"]
        positions = [code.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("response.arrayBuffer()", code)
        self.assertIn("job.failure", code)

    def test_rife_recipe_downloads_the_signed_asset_not_content_json(self) -> None:
        recipe = (Path(__file__).resolve().parents[1] / "docs/LOCAL_CLIENT_RECIPES.md").read_text()
        fence = chr(96) * 3
        recipe = recipe.split("### RIFE video interpolation", 1)[1].split(fence + "bash", 1)[1].split(fence, 1)[0]
        self.assertIn("succeeded", recipe)
        self.assertIn("failed|canceled", recipe)
        self.assertIn("deadline", recipe)
        self.assertIn(".data[0].url", recipe)
        self.assertIn('--output interpolated.mp4', recipe)
        self.assertNotIn('content" \\\n  -H "Authorization: Bearer $AZIZAM_API_KEY" -o', recipe)


if __name__ == "__main__":
    unittest.main()
