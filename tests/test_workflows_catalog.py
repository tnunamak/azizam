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
    _curl_example,
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

    def run_python(self, steps: list[dict], *, expired: bool = False,
                   poll_elapsed: float | None = None,
                   arrives_at: dict[str, float] | None = None) -> tuple[bytes | None, list[str], Exception | None]:
        import io
        import sys
        import types

        calls, writes = [], {}
        queue = list(steps)
        now = [0.0]

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
            if poll_elapsed is not None and url.endswith("/media/jobs/job-1"):
                self.assertEqual(kwargs["timeout"], 60)
                now[0] = poll_elapsed  # The response arrives after time passes inside the poll.
            for suffix, at in (arrives_at or {}).items():
                if url.endswith(suffix) or url.startswith(suffix):
                    self.assertLessEqual(kwargs["timeout"], 600 - now[0])  # never waits past the deadline
                    now[0] = at  # The response arrives after time passes inside this request.
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
        clock = (patch("time.monotonic", side_effect=[0, 601]) if expired else
                 patch("time.monotonic", side_effect=lambda: now[0])
                 if poll_elapsed is not None or arrives_at else nullcontext())
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
                    "if url.startswith('https://assets.test/') and any(c in url for c in '[]{}'):\n"
                    "    if '--globoff' not in args or '--url' not in args or args[args.index('--url') + 1] != url: sys.exit(97)\n"
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


    def test_python_success_arriving_at_or_after_deadline_is_not_downloaded(self) -> None:
        for elapsed in (600, 601):
            with self.subTest(poll_elapsed=elapsed):
                output, calls, failure = self.run_python(self.steps(202), poll_elapsed=elapsed)
                self.assertIsNone(output)
                self.assertIsInstance(failure, TimeoutError)
                self.assertIn("deadline", str(failure))
                self.assertEqual(calls, [
                    "https://gateway.test/gateway/v1/workflows/video.edit.fixture/run",
                    "https://gateway.test/gateway/v1/media/jobs/job-1",
                ])

    def test_python_downloads_finishing_after_deadline_are_not_saved(self) -> None:
        run = "https://gateway.test/gateway/v1/workflows/video.edit.fixture/run"
        poll = "https://gateway.test/gateway/v1/media/jobs/job-1"
        cases = {
            # A timely success, then a slow content read: the asset is never requested.
            "content": ({"/media/jobs/job-1": 590, "/content": 640}, [run, poll, poll + "/content"]),
            # The asset body finishes after the deadline: nothing is written.
            "asset": ({"/media/jobs/job-1": 590, "https://assets.test/": 640},
                      [run, poll, poll + "/content", "https://assets.test/movie?sig=fixture"]),
        }
        for name, (arrives_at, expected_calls) in cases.items():
            with self.subTest(late=name):
                output, calls, failure = self.run_python(self.steps(202), arrives_at=arrives_at)
                self.assertIsNone(output)
                self.assertIsInstance(failure, TimeoutError)
                self.assertEqual(calls, expected_calls)

    def test_bash_and_javascript_bound_downloads_by_the_deadline(self) -> None:
        example = self.example()
        curl_lines = [line for line in example["curl"].replace("\\\n", " ").splitlines() if "curl --" in line]
        self.assertEqual(len(curl_lines), 4)  # submit, poll, content, asset
        for line in curl_lines:
            with self.subTest(curl=line[:60]):
                self.assertIn("--globoff", line)
        self.assertIn("remaining < 60 ? remaining : 60", example["curl"])
        self.assertIn("remaining < 300 ? remaining : 300", example["curl"])
        # A download cut off by --max-time never takes the output name.
        part = example["curl"].index("--output workflow-output.bin.part")
        self.assertLess(part, example["curl"].index("mv workflow-output.bin.part workflow-output.bin"))
        js = example["javascript"]
        order = ["const timeLeft", "checkedGet(contentUrl, jobHeaders, Math.min(60000, timeLeft()))",
                 "checkedGet(assetUrl, {}, Math.min(300000, timeLeft()))", "timeLeft();", "writeFile("]
        positions = [js.find(marker) for marker in order]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_python_success_arriving_before_deadline_is_downloaded(self) -> None:
        output, calls, failure = self.run_python(self.steps(202), poll_elapsed=599)
        self.assertIsNone(failure)
        self.assertEqual(output, b"movie-bytes")
        self.assertEqual(len(calls), 4)

    def test_generated_and_recipe_signed_url_curls_disable_globbing(self) -> None:
        import shlex

        recipe = (Path(__file__).resolve().parents[1] / "docs/LOCAL_CLIENT_RECIPES.md").read_text()
        fence = chr(96) * 3
        recipe = recipe.split("### RIFE video interpolation", 1)[1].split(fence + "bash", 1)[1].split(fence, 1)[0]
        for name, code in (("generated", self.example()["curl"]), ("recipe", recipe)):
            with self.subTest(source=name):
                calls = [line.strip() for line in code.replace("\\\n", " ").splitlines()
                         if line.strip().startswith("curl ") and '"$asset_url"' in line]
                self.assertEqual(len(calls), 1)
                self.assertIn('--url "$asset_url"', calls[0])
                args = shlex.split(calls[0])
                self.assertIn("--globoff", args)
                self.assertEqual(args[args.index("--url") + 1], "$asset_url")
                self.assertNotIn("-H", args)  # Signed URLs must remain bearerless.

    def test_curl_signed_url_with_braces_and_brackets_is_passed_literally(self) -> None:
        steps = self.steps(202)
        asset_url = "https://assets.test/movie?sig=[fixture]{one,two}"
        steps[2]["json"]["data"][0]["url"] = asset_url
        output, calls, failure = self.run_process("curl", steps)
        self.assertFalse(failure, failure)
        self.assertEqual(output, b"movie-bytes")
        self.assertEqual(calls[-1], asset_url)

    def test_non_job_curl_example_text_is_unchanged(self) -> None:
        self.assertEqual(
            _curl_example("https://gateway.test/v1/images/generations",
                          content_type="application/json", body={"prompt": "fixture"},
                          form={}, files=[], binary_response=False),
            "curl 'https://gateway.test/v1/images/generations' \\\n"
            "  -H 'Authorization: Bearer '$AZIZAM_API_KEY \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -d '{\"prompt\":\"fixture\"}'",
        )

    def test_curl_202_and_200_jobs_poll_then_download(self) -> None:
        self.check_clients("curl")

    def test_javascript_example_polls_reads_content_then_downloads_the_asset(self) -> None:
        # The gate sandbox has no Node, so this reads the published order of calls. The same
        # fixtures drive the curl and Python examples end to end above.
        code = self.example()["javascript"]
        order = ["await response.json()", "job.job_id", "const deadline", "['failed', 'canceled']", "checkedGet(pollUrl",
                 "checkedGet(contentUrl", "content.data?.[0]?.url", "checkedGet(assetUrl,", "asset.arrayBuffer()"]
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




# Frozen from 8333cbd7a2c24ca9d51492e94089dc15855476b8. This oracle has its own
# route selection and response rendering, so a change to the live generator
# cannot silently redefine byte-for-byte parity. Request helpers are unchanged.
_BASE_RESPONSE_RENDERERS = r'''def _execution(record: WorkflowRecord, typical_seconds: float | None) -> dict[str, Any]:
    """Sync and async routes for one workflow, and which to prefer."""
    intent = record.intent
    # Video workflows are configured as ``video-<short id>``; the rest use the short id.
    model = f"video-{short_id(record)}" if intent.startswith("video.") else short_id(record)
    sync: dict[str, Any] | None = None
    async_: dict[str, Any] | None = None

    def job_route(wrapped_intent: str) -> dict[str, Any]:
        return {
            "kind": "media_job",
            "submit": {
                "method": "POST",
                "path": JOBS_SUBMIT,
                "content_type": _JSON,
                "body": {"intent": wrapped_intent, "request": "<the sync request body>"},
                "model": model,
            },
            "poll": {"method": "GET", "path": JOB_POLL},
            "content": {"method": "GET", "path": JOB_CONTENT},
            "cancel": {"method": "POST", "path": JOB_CANCEL},
            "statuses": _MEDIA_JOB_STATUSES,
            "terminal": ["succeeded", "failed", "canceled"],
            "fields": request_mapping(record, "async"),
        }

    native = gateway_route(record)
    if native:
        sync = {"method": "POST", "path": native, "content_type": _MULTIPART,
                "fields": request_mapping(record, "sync"),
                "output": "JSON (see the route's response schema)"}
        if intent == "image.edit":
            sync["alternate"] = {"method": "POST", "path": "/v1/images/edits", "model": model,
                                 "note": "options go in extensions['com.azizam.workflow']"}
    elif intent == "image.generate":
        sync = {"method": "POST", "path": "/v1/images/generations", "content_type": _JSON,
                "model": model, "fields": request_mapping(record, "sync"),
                "output": "data[].b64_json or data[].url"}
        async_ = job_route("image.generate")
    elif intent == "image.edit":
        sync = {"method": "POST", "path": "/v1/images/edits", "content_type": _MULTIPART,
                "model": model, "fields": request_mapping(record, "sync"),
                "extensions_field": "extensions (JSON string)",
                "output": "data[].b64_json or data[].url"}
    elif intent == "audio.generate":
        sync = {"method": "POST", "path": "/v1/audio/generate", "content_type": _JSON,
                "model": model, "fields": request_mapping(record, "sync"),
                "output": "audio bytes (see Content-Type)"}
        async_ = job_route("audio.generate")
    elif intent == "mesh.generate":
        sync = {"method": "POST", "path": _MESH_PATH, "content_type": _MULTIPART,
                "fields": request_mapping(record, "sync"),
                "output": "model/gltf-binary (GLB bytes)"}
        # The same route answers 202 with a media job when the request says ``Prefer: respond-async`` (RFC 7240). The
        # run takes minutes under load, and a held connection that drops loses the answer; the job does not.
        async_ = {
            "kind": "respond_async",
            "submit": {"method": "POST", "path": _MESH_PATH, "content_type": _MULTIPART,
                       "headers": {"Prefer": "respond-async"}},
            "poll": {"method": "GET", "path": JOB_POLL},
            "content": {"method": "GET", "path": JOB_CONTENT, "output": "model/gltf-binary (GLB bytes)"},
            "cancel": {"method": "POST", "path": JOB_CANCEL,
                       "note": "the engine cannot stop mid-run: the job ends as canceled when the run returns"},
            "statuses": _MEDIA_JOB_STATUSES,
            "terminal": ["succeeded", "failed", "canceled"],
            "fields": request_mapping(record, "sync"),
            "output": "202 with a media job (Location, Preference-Applied: respond-async)",
        }
    elif intent.startswith("video."):
        async_ = {
            "kind": "video",
            "submit": {"method": "POST", "path": "/v1/videos", "content_type": _JSON,
                       "model": model},
            "poll": {"method": "GET", "path": "/v1/videos/{video_id}"},
            "content": {"method": "GET", "path": "/v1/videos/{video_id}/content"},
            "cancel": {"method": "DELETE", "path": "/v1/videos/{video_id}"},
            "statuses": _VIDEO_STATUSES,
            "terminal": ["completed", "failed"],
            "fields": request_mapping(record, "async"),
        }
        if intent == "video.edit":
            sync = {"method": "POST", "path": f"/gateway/v1/workflows/{record.id}/run",
                    "content_type": _MULTIPART, "fields": request_mapping(record, "sync"),
                    "output": "a media job; its content is the video",
                    "response": {"kind": "media_job", "poll": JOB_POLL, "content": JOB_CONTENT}}
    if typical_seconds is not None and typical_seconds <= SLOW_SECONDS and sync:
        recommended = "sync"
    elif async_:
        recommended = "async"
    else:
        recommended = "sync"
    return {"recommended": recommended, "sync": sync, "async": async_}


# --------------------------------------------------------------------- aliases

_ALIAS_OPERATION_INTENTS = {
    "images": ("image.generate",),
    "image_edits": ("image.edit",),
    "videos": ("video.generate", "video.edit"),
    "audio_generate": ("audio.generate",),
}

def _example_request(
    record: WorkflowRecord,
    inputs: list[Mapping[str, Any]],
    route: Mapping[str, Any] | None,
    base_url: str,
) -> dict[str, Any]:
    path = _route_path(route) or f"/gateway/v1/workflows/{record.id}/run"
    # The wire of the request: a route that names its ``submit`` is sent as that submit says.
    wire = {**(route or {}), **((route or {}).get("submit") or {})}
    method = str(wire.get("method") or "POST")
    content_type = str(wire.get("content_type") or "application/json")
    url = _join_url(base_url, path)
    mode = "async" if (route or {}).get("kind") == "video" else "sync"
    selected_model = _route_model(route)
    body = _example_body(record, inputs, mode=mode, model=selected_model)
    form: dict[str, Any] = {}
    files: dict[str, dict[str, Any]] = {}
    if (route or {}).get("kind") == "media_job":
        submit = route.get("submit") if isinstance(route, Mapping) else {}
        wrapped_intent = _nested(submit or {}, "body", "intent") or record.intent
        body = {
            "intent": wrapped_intent,
            "request": _example_body(record, inputs, mode="sync", model=_route_model(submit)),
        }
        content_type = "application/json"
    elif content_type == "multipart/form-data":
        form, files = _example_multipart_parts(record, inputs, model=selected_model)
        body = {}
    # Response semantics are independent of the upload format. In particular,
    # the generic video-edit run remains multipart even though it returns a job.
    response = wire.get("response")
    job_urls = (
        (_join_url(base_url, response["poll"]), _join_url(base_url, response["content"]))
        if isinstance(response, Mapping) and response.get("kind") == "media_job"
        else None
    )
    answers_job = job_urls is not None or (route or {}).get("kind") == "respond_async"
    binary_response = ((record.intent == "audio.generate" and (route or {}).get("kind") != "media_job")
                       or content_type == "multipart/form-data") and not answers_job
    # Made-up local paths belong in the code examples only. The catalog's
    # `files` says which input each file part carries, never a path, so no
    # screen can show "./source-image.bin" as if it were helper text.
    paths = [
        (str(part.get("field") or name), _example_file_for(part["input"], part.get("media_type")))
        for name, part in files.items()
    ]
    extra_headers = wire.get("headers")
    curl = _curl_example(
        url, content_type=content_type, body=body, form=form, files=paths,
        binary_response=binary_response, headers=extra_headers, job_urls=job_urls,
    )
    return {
        "method": method,
        "path": path,
        "content_type": content_type,
        "body": body,
        "form": form,
        "files": files,
        "curl": curl,
        "python": _python_example(
            method, url, content_type=content_type, body=body, form=form, files=paths,
            binary_response=binary_response, headers=extra_headers, job_urls=job_urls,
        ),
        "javascript": _javascript_example(
            method, url, content_type=content_type, body=body, form=form, files=paths,
            binary_response=binary_response, headers=extra_headers, job_urls=job_urls,
        ),
    }

def _curl_example(
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Sequence[tuple[str, str]],
    binary_response: bool,
    headers: Mapping[str, str] | None = None,
    job_urls: tuple[str, str] | None = None,
) -> str:
    if job_urls is not None:
        submit = _curl_example(
            url, content_type=content_type, body=body, form=form, files=files,
            binary_response=False, headers=headers,
        )
        return _curl_job_example(submit, *job_urls)
    lines = [
        f"curl {_shell_single_quote(url)} \\",
        "  -H 'Authorization: Bearer '$AZIZAM_API_KEY \\",
        *[f"  -H {_shell_single_quote(f'{name}: {value}')} \\" for name, value in (headers or {}).items()],
    ]
    if content_type == "multipart/form-data":
        for key, value in form.items():
            lines.append(f"  -F {_shell_single_quote(f'{key}={value}')} \\")
        for key, path in files:
            lines.append(f"  -F {_shell_single_quote(f'{key}=@{path}')} \\")
        if binary_response:
            lines.append("  --output workflow-output.bin \\")
        return "\n".join(lines).rstrip(" \\")
    lines.append("  -H 'Content-Type: application/json' \\")
    lines.append(f"  -d {_shell_single_quote(json.dumps(body, separators=(',', ':')))}" + (" \\" if binary_response else ""))
    if binary_response:
        lines.append("  --output workflow-output.bin")
    return "\n".join(lines)

def _python_example(
    method: str,
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Sequence[tuple[str, str]],
    binary_response: bool,
    headers: Mapping[str, str] | None = None,
    job_urls: tuple[str, str] | None = None,
) -> str:
    if job_urls is not None:
        submit = _python_example(
            method, url, content_type=content_type, body=body, form=form, files=files,
            binary_response=False, headers=headers,
        ).rsplit("\n", 1)[0]
        return submit + "\n" + _python_job_followup(*job_urls)
    if content_type == "multipart/form-data":
        extra = "".join(f", {name!r}: {value!r}" for name, value in (headers or {}).items())
        file_lines = "\n".join(
            f"        ({key!r}, stack.enter_context(open({path!r}, 'rb'))),"
            for key, path in files
        )
        return (
            "import os\nfrom contextlib import ExitStack\nimport requests\n\n"
            "with ExitStack() as stack:\n"
            f"    files = [\n{file_lines}\n    ]\n"
            f"    response = requests.request({method!r}, {url!r},\n"
            "        headers={'Authorization': f\"Bearer {os.environ.get('AZIZAM_API_KEY') or ''}\""
            + extra + "},\n"
            f"        data={dict(form)!r}, files=files, timeout=300)\n"
            "response.raise_for_status()\n"
            + (
                "open('workflow-output.bin', 'wb').write(response.content)\n"
                if binary_response else
                "print(response.json() if response.headers.get('content-type', '').startswith('application/json') else response.content[:80])"
            )
        )
    return (
        "import os\nimport requests\n\n"
        f"response = requests.request({method!r}, {url!r},\n"
        "    headers={'Authorization': f\"Bearer {os.environ.get('AZIZAM_API_KEY') or ''}\"},\n"
        f"    json={dict(body)!r}, timeout=300)\n"
        "response.raise_for_status()\n"
        + (
            "open('workflow-output.bin', 'wb').write(response.content)\n"
            if binary_response else
            "print(response.json())"
        )
    )

def _javascript_example(
    method: str,
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Sequence[tuple[str, str]],
    binary_response: bool,
    headers: Mapping[str, str] | None = None,
    job_urls: tuple[str, str] | None = None,
) -> str:
    if job_urls is not None:
        submit = _javascript_example(
            method, url, content_type=content_type, body=body, form=form, files=files,
            binary_response=False, headers=headers,
        ).rsplit("\n", 1)[0]
        if content_type != "multipart/form-data":
            submit = "import { writeFile } from 'node:fs/promises';\n" + submit
        submit = submit.replace("  body:", "  signal: AbortSignal.timeout(300000),\n  body:", 1)
        return submit + "\n" + _javascript_job_followup(*job_urls)
    if content_type == "multipart/form-data":
        extra = "".join(f", {json.dumps(name)}: {json.dumps(value)}" for name, value in (headers or {}).items())
        lines = [
            "import { readFile, writeFile } from 'node:fs/promises';",
            "const form = new FormData();",
            *[f"form.append({json.dumps(key)}, {json.dumps(str(value))});" for key, value in form.items()],
            *[
                (
                    f"form.append({json.dumps(key)}, "
                    f"new Blob([await readFile({json.dumps(path)})]), "
                    f"{json.dumps(path.rsplit('/', 1)[-1])});"
                )
                for key, path in files
            ],
            f"const response = await fetch({json.dumps(url)}, {{",
            f"  method: {json.dumps(method)},",
            "  headers: { authorization: `Bearer ${process.env.AZIZAM_API_KEY}`" + extra + " },",
            "  body: form",
            "});",
            "if (!response.ok) throw new Error(await response.text());",
            (
                "await writeFile('workflow-output.bin', Buffer.from(await response.arrayBuffer()));"
                if binary_response else
                "console.log(await response.text());"
            ),
        ]
        return "\n".join(lines)
    result_line = (
        "await writeFile('workflow-output.bin', Buffer.from(await response.arrayBuffer()));"
        if binary_response else
        "console.log(await response.json());"
    )
    import_line = "import { writeFile } from 'node:fs/promises';\n" if binary_response else ""
    return (
        f"{import_line}const response = await fetch({json.dumps(url)}, {{\n"
        f"  method: {json.dumps(method)},\n"
        "  headers: { authorization: `Bearer ${process.env.AZIZAM_API_KEY}`, 'content-type': 'application/json' },\n"
        f"  body: JSON.stringify({json.dumps(dict(body), indent=2)}),\n"
        f"}});\nif (!response.ok) throw new Error(await response.text());\n{result_line}"
    )
'''


class DeclaredResponseKindExampleTests(unittest.TestCase):
    @staticmethod
    def example(intent: str, route: dict) -> dict:
        from types import SimpleNamespace
        from gateway.workflows_catalog import _example_request

        record = SimpleNamespace(
            id=f"{intent}.fixture", intent=intent,
            contract={"inputs": {}, "bindings": {}},
        )
        return _example_request(record, [], route, "https://gateway.test")

    def assert_json_example(self, example: dict, *, multipart: bool = False,
                            legacy_job: bool = False) -> None:
        for language in ("curl", "python", "javascript"):
            self.assertNotIn("workflow-output.bin", example[language], language)
        self.assertIn("response.json()", example["python"])
        # Existing multipart job displays remain byte-identical; an explicitly
        # declared JSON route parses JSON rather than treating it as media bytes.
        terminal = "response.text()" if multipart and legacy_job else "response.json()"
        self.assertTrue(example["javascript"].endswith(f"console.log(await {terminal});"))

    def assert_binary_example(self, example: dict) -> None:
        self.assertIn("--output workflow-output.bin", example["curl"])
        self.assertIn("write(response.content)", example["python"])
        self.assertIn("Buffer.from(await response.arrayBuffer())", example["javascript"])

    def test_sync_examples_follow_response_kind_for_every_upload_shape(self) -> None:
        cases = (
            ("image.generate", "application/json", {"kind": "json"}, False),
            ("image.edit", "multipart/form-data", {"kind": "json"}, False),
            ("mesh.generate", "multipart/form-data",
             {"kind": "binary", "media_type": "model/gltf-binary"}, True),
            ("audio.generate", "application/json", {"kind": "binary"}, True),
            ("unfamiliar.render", "application/json", {"kind": "binary"}, True),
            ("audio.generate", "application/json", {"kind": "json"}, False),
        )
        for intent, content_type, response, binary in cases:
            with self.subTest(intent=intent, content_type=content_type, response=response):
                example = self.example(intent, {
                    "method": "POST", "path": "/fixture",
                    "content_type": content_type, "response": response,
                })
                self.assertEqual(example["content_type"], content_type)
                if binary:
                    self.assert_binary_example(example)
                else:
                    self.assert_json_example(example, multipart=content_type == "multipart/form-data")

    def test_native_route_json_response_is_not_inferred_from_intent(self) -> None:
        example = self.example("unfamiliar.render", {
            "method": "POST", "path": "/gateway/v1/native/fixture",
            "content_type": "multipart/form-data", "response": {"kind": "json"},
        })
        self.assert_json_example(example, multipart=True)

    def test_job_route_kinds_override_declared_binary(self) -> None:
        for kind, content_type in (
            ("media_job", "application/json"),
            ("respond_async", "multipart/form-data"),
        ):
            with self.subTest(kind=kind):
                example = self.example("audio.generate", {
                    "kind": kind, "response": {"kind": "binary"},
                    "submit": {"method": "POST", "path": "/jobs",
                               "content_type": content_type},
                })
                self.assert_json_example(
                    example, multipart=content_type == "multipart/form-data", legacy_job=True,
                )

    def test_multipart_media_job_parses_submit_then_downloads_only_the_asset(self) -> None:
        example = self.example("video.edit", {
            "method": "POST", "path": "/run", "content_type": "multipart/form-data",
            "response": {"kind": "media_job", "poll": "/jobs/{job_id}",
                         "content": "/jobs/{job_id}/content"},
        })
        self.assertIn("job=$(curl", example["curl"])
        self.assertIn("job = response.json()", example["python"])
        self.assertIn("let job = await response.json()", example["javascript"])
        self.assertNotIn("write(response.content)", example["python"])
        self.assertNotIn("response.arrayBuffer()", example["javascript"])
        self.assertIn("output.write(asset.content)", example["python"])
        self.assertIn("asset.arrayBuffer()", example["javascript"])

    def test_missing_response_kind_never_infers_binary_from_multipart_or_intent(self) -> None:
        example = self.example("audio.generate", {
            "method": "POST", "path": "/fixture", "content_type": "multipart/form-data",
        })
        self.assert_json_example(example, multipart=True, legacy_job=True)

    def test_every_shipped_example_matches_base_except_exact_json_output_fix(self) -> None:
        import gateway.workflow_openapi as route_module
        import gateway.workflows_catalog as catalog_module

        namespace = {**vars(route_module), **vars(catalog_module)}
        exec(compile(_BASE_RESPONSE_RENDERERS, "<8333cbd7 example oracle>", "exec"), namespace)
        registry = WorkflowRegistry.from_directory(
            Path(__file__).resolve().parents[1] / "manifests" / "workflows",
        )
        self.assertEqual(registry.errors, [])
        records = registry.records()
        self.assertTrue(records)
        openapi_rows = {row["id"]: row for row in route_module.workflow_catalog(records)}
        changed = set()
        affected = set()
        for record in records:
            with self.subTest(workflow=record.id):
                openapi_row = openapi_rows[record.id]
                actual = catalog_module._workflow_row(
                    record, capability_rows=[], openapi_row=openapi_row, aliases=None,
                    receipts=[], jobs=[], base_url="https://gateway.test",
                )["example_request"]
                old_routes = namespace["_execution"](record, openapi_row["typical_seconds"])
                old_route = (old_routes.get(old_routes["recommended"])
                             or old_routes.get("sync") or old_routes.get("async"))
                old = namespace["_example_request"](
                    record, catalog_module._input_rows(record, old_routes), old_route,
                    "https://gateway.test",
                )
                old_wire = {**(old_route or {}), **((old_route or {}).get("submit") or {})}
                # Derive the exception from the frozen base's promised output,
                # not the new kind: a wrong new declaration must fail parity.
                is_fixed_json = (old_wire.get("content_type") == "multipart/form-data"
                                 and old_wire.get("output") in {
                                     "JSON (see the route's response schema)",
                                     "data[].b64_json or data[].url",
                                 })
                expected = dict(old)
                if is_fixed_json:
                    affected.add(record.id)
                    expected["curl"] = old["curl"].replace(
                        "\n  --output workflow-output.bin", "",
                    ).rstrip(" \\")
                    expected["python"] = old["python"].replace(
                        "open('workflow-output.bin', 'wb').write(response.content)\n",
                        "print(response.json() if response.headers.get('content-type', '').startswith('application/json') else response.content[:80])",
                    )
                    expected["javascript"] = old["javascript"].replace(
                        "await writeFile('workflow-output.bin', Buffer.from(await response.arrayBuffer()));",
                        "console.log(await response.json());",
                    )
                    self.assert_json_example(actual, multipart=True)
                    for language in ("curl", "python", "javascript"):
                        self.assertNotEqual(actual[language], old[language], language)
                if actual != old:
                    changed.add(record.id)
                # Full dictionary equality also pins method/path/content_type,
                # body/form/files and every non-output byte of all three clients.
                self.assertEqual(actual, expected)
        self.assertTrue(affected, "the shipped image-edit regression must be exercised")
        self.assertEqual(changed, affected, f"changed workflows: {sorted(changed)}")
if __name__ == "__main__":
    unittest.main()
