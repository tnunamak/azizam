"""Video remix checks and character replace (SCAIL-2) on the video routes.

Drives the real FastAPI routes against a fake ComfyUI, with the shipped
proxy_config.yml and workflow JSON, and inspects the graph the gateway submits.
Remix is off in the shipped config, so only its checks that run before a
workflow is submitted are tested here. See RemixRouteTests.
"""

from __future__ import annotations

import base64
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import authenticated  # noqa: E402
from gateway import video_inputs  # noqa: E402
from _fake_comfy_video import (  # noqa: E402
    NEW_PROMPT_ID,
    SOURCE_BYTES,  # noqa: F401 -- imported by test_video_extend.py from this module
    SOURCE_PROMPT_ID,
    FakeComfy,
)

SOURCE_ID = f"video_{SOURCE_PROMPT_ID}"
NEW_ID = f"video_{NEW_PROMPT_ID}"
PROBE_24 = video_inputs.VideoProbe(fps=24.0, duration=3.5, frames=84, width=832, height=480)
NS = "com.azizam.workflow"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
# Stand-in for videos.remix_workflow. The shipped config leaves remix off, and
# the route's checks below its 501 gate need a truthy value. No test in this
# module lets a request reach submission under this id.
REMIX_STAND_IN = "video-remix-stand-in"


@pytest.mark.usefixtures("isolated_receipts")
class RemixReplaceBase(unittest.TestCase):
    probe = PROBE_24

    def setUp(self) -> None:
        self.comfy = FakeComfy()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for p in self.comfy.patches(probe=self.probe):
            self.stack.enter_context(p)
        self.stack.enter_context(authenticated())
        self.stack.enter_context(patch.object(proxy, "_enforce_runtime_route_policy", return_value=None))
        for name in ("_video_job_records", "_video_progress", "_video_workflows"):
            saved = dict(getattr(proxy, name))
            getattr(proxy, name).clear()
            self.addCleanup(lambda n=name, s=saved: (getattr(proxy, n).clear(), getattr(proxy, n).update(s)))
        proxy._video_job_records[SOURCE_PROMPT_ID] = {
            "model": "ltx-2.5", "resolved_model": "video-ltx-2.5-t2v", "size": "960x512",
            "seconds": "4", "created_at": 1000, "completed_at": 1100, "owner_key_hash": "sha256:test",
            "deleted_at": None,
        }
        self.client = TestClient(proxy.app)

    @property
    def graph(self) -> dict:
        self.assertEqual(len(self.comfy.submitted), 1, "expected one submitted graph")
        return self.comfy.submitted[0]

    def assert_refused(self, response, *, param: str) -> None:
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(response.json()["error"]["param"].endswith(param), response.json())
        self.assertEqual(self.comfy.uploads, [], "a refused request must leave no files in ComfyUI")
        self.assertEqual(self.comfy.submitted, [])


class RemixShippedStateTests(RemixReplaceBase):
    def test_shipped_config_leaves_remix_off(self) -> None:
        self.assertIsNone(proxy.VIDEO_REMIX_WORKFLOW)
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 501, r.text)
        self.assertEqual(r.json()["error"]["code"], "video_remix_not_configured")
        self.assertEqual(self.comfy.submitted, [])


class RemixRouteTests(RemixReplaceBase):
    """Remix input checks. Each one runs before a workflow is submitted.

    setUp sets REMIX_STAND_IN so the checks run past the 501 gate. It also adds the `video-remix` alias that the
    config asks for. Without the alias, model lookup returns model_not_found (404) before the source checks run.
    """

    def setUp(self) -> None:
        super().setUp()
        self.stack.enter_context(patch.object(proxy, "VIDEO_REMIX_WORKFLOW", REMIX_STAND_IN))
        self.stack.enter_context(patch.dict(proxy.VIDEO_ALIASES, {"video-remix": "video-scail-2-replace"}))

    def test_remix_requires_prompt(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "prompt")

    def test_unknown_source_is_404(self) -> None:
        r = self.client.post("/v1/videos/video_nope/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error"]["message"], "video_id not found")
        self.assertEqual(self.comfy.submitted, [])

    def test_source_not_completed_is_rejected_with_video_not_ready(self) -> None:
        self.comfy.history[SOURCE_PROMPT_ID] = {"status": {"completed": False}, "outputs": {}}
        # The stand-in target (SCAIL) also needs a reference image. Without one it refuses before it checks the source.
        reference = "data:image/png;base64," + base64.b64encode(PNG).decode()
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x", "extensions": {NS: {"reference_image": reference}}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"].get("code"), "video_not_ready", r.text)

    def test_other_keys_video_is_hidden(self) -> None:
        proxy._video_job_records[SOURCE_PROMPT_ID]["owner_key_hash"] = "sha256:someone-else"
        with patch.object(proxy, "_asset_owner_key", return_value="sha256:me"):
            r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["message"], "video_id not found")

    def test_source_video_cannot_be_overridden_by_an_extension(self) -> None:
        r = self.client.post(
            f"/v1/videos/{SOURCE_ID}/remix",
            json={"prompt": "x", "extensions": {NS: {"source_video": "video_other"}}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")


class EditRouteTests(RemixReplaceBase):
    def setUp(self) -> None:
        super().setUp()
        self.stack.enter_context(patch.object(proxy, "VIDEO_REMIX_WORKFLOW", REMIX_STAND_IN))

    def test_missing_video_and_prompt_are_rejected(self) -> None:
        self.assertEqual(self.client.post("/v1/videos/edits", json={"prompt": "x"}).json()["error"]["param"], "video")
        self.assertEqual(
            self.client.post("/v1/videos/edits", json={"video": {"id": SOURCE_ID}}).json()["error"]["param"],
            "prompt",
        )


class CharacterReplaceTests(RemixReplaceBase):
    probe = video_inputs.VideoProbe(fps=24.0, duration=5.0, frames=120, width=832, height=448)

    def _body(self, **extra) -> dict:
        ext = {
            "source_video": {"video_id": SOURCE_ID},
            "reference_image": "data:image/png;base64," + base64.b64encode(PNG).decode(),
            "source_subject": "man in gray hoodie",
            "reference_subject": "woman",
        }
        ext.update(extra)
        return {"model": "character-replace", "prompt": "a woman in a red raincoat", "extensions": {NS: ext}}

    def test_json_create_binds_video_image_and_subjects(self) -> None:
        r = self.client.post("/v1/videos", json=self._body())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["model"], "character-replace")
        self.assertEqual(body["azizam_resolved_model"], "video-scail-2-replace")
        self.assertIsNone(body["remixed_from_video_id"])
        self.assertEqual(body["size"], "832x448")
        g = self.graph
        self.assertEqual(g["1"]["inputs"]["file"], "up1_source_00001_.mp4")
        self.assertTrue(g["2"]["inputs"]["image"].startswith("up2_"))
        self.assertEqual(g["16"]["inputs"]["text"], "man in gray hoodie")
        self.assertEqual(g["17"]["inputs"]["text"], "woman")
        self.assertEqual(g["10"]["inputs"]["value"], "a woman in a red raincoat")
        self.assertEqual(g["22"]["inputs"]["value"], 81)  # capped at one segment

    def test_sdk_multipart_bracket_fields_are_rebuilt(self) -> None:
        r = self.client.post(
            "/v1/videos",
            data={
                "model": "character-replace",
                "prompt": "p",
                "seed": "5",
                f"extensions[{NS}][source_video][video_id]": SOURCE_ID,
                f"extensions[{NS}][reference_image]": "data:image/png;base64," + base64.b64encode(PNG).decode(),
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["30"]["inputs"]["value"], 5)
        self.assertEqual(self.graph["16"]["inputs"]["text"], "person")  # workflow default

    def test_multipart_data_uri_larger_than_one_megabyte_is_accepted(self) -> None:
        big = base64.b64encode(PNG + b"\x00" * (2 * 1024 * 1024)).decode()
        r = self.client.post(
            "/v1/videos",
            data={
                "model": "character-replace",
                "prompt": "p",
                f"extensions[{NS}][source_video][video_id]": SOURCE_ID,
                f"extensions[{NS}][reference_image]": "data:image/png;base64," + big,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(len(self.comfy.uploads[1][1]), 2 * 1024 * 1024)

    def test_missing_inputs_are_rejected_before_any_upload(self) -> None:
        body = self._body()
        del body["extensions"][NS]["reference_image"]
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(self.comfy.submitted, [])

        body = self._body()
        del body["extensions"][NS]["source_video"]
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("source_video", r.json()["error"]["param"])
        self.assertEqual(r.json()["error"]["message"],
                         "source_video is required for this model: pass a video id, URL or data URI")
        self.assertEqual(self.comfy.uploads, [])

        r = self.client.post("/v1/videos", json={"model": "character-replace", "prompt": "p"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_stable_alias_and_names_resolve(self) -> None:
        for name in ("character-replace", "scail-2", "video-scail-2-replace"):
            self.assertEqual(proxy._resolve_video_workflow(name)[0], "video-scail-2-replace")
        res = proxy.resolve_model_for_operation("videos", "character-replace")
        self.assertEqual(res.effective_model, "video-scail-2-replace")

    def test_unknown_extension_and_route_managed_input_are_rejected(self) -> None:
        r = self.client.post("/v1/videos", json=self._body(bogus=1))
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/v1/videos", json=self._body(seed=3))
        self.assertEqual(r.status_code, 400)

    def test_source_video_must_be_a_reference_not_a_path(self) -> None:
        r = self.client.post("/v1/videos", json=self._body(source_video="/etc/passwd"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.comfy.uploads, [])

    def test_input_reference_is_not_a_second_way_to_pass_the_image(self) -> None:
        body = self._body()
        body["input_reference"] = "data:image/png;base64," + base64.b64encode(PNG).decode()
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 400, r.text)

    def test_short_source_clamps_default_seconds_to_its_length(self) -> None:
        self.stack.enter_context(patch.object(
            proxy.video_inputs, "probe_video",
            lambda *a, **k: video_inputs.VideoProbe(fps=30.0, duration=2.0, frames=60, width=1, height=1),
        ))
        r = self.client.post("/v1/videos", json=self._body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["22"]["inputs"]["value"], 57)  # 4k+1 <= 60 frames


class VideoInputsUnitTests(unittest.TestCase):
    def test_bracket_form_rebuilds_nested_mapping(self) -> None:
        out = video_inputs.parse_bracket_form([
            ("prompt", "p"),
            ("extensions[a.b][x][y]", "1"),
            ("extensions[a.b][z]", "2"),
        ])
        self.assertEqual(out, {"prompt": "p", "extensions": {"a.b": {"x": {"y": "1"}, "z": "2"}}})

    def test_bracket_form_rejects_scalar_container_conflicts(self) -> None:
        with self.assertRaises(ValueError):
            video_inputs.parse_bracket_form([("a", "1"), ("a[b]", "2")])
        with self.assertRaises(ValueError):
            video_inputs.parse_bracket_form([("a[b]", "2"), ("a[b][c]", "3")])
        with self.assertRaises(ValueError):
            video_inputs.parse_bracket_form([("a[]", "2")])

    def test_snap_length_down(self) -> None:
        self.assertEqual(video_inputs.snap_length_down(97, step=4, offset=1, available=84), 81)
        self.assertEqual(video_inputs.snap_length_down(97, step=4, offset=1, available=None), 97)
        self.assertEqual(video_inputs.snap_length_down(2, step=4, offset=1, available=2), 1)

    def test_coerce_form_scalars_follows_manifest_types(self) -> None:
        specs = {"n": {"type": "integer"}, "x": {"type": "number"}, "b": {"type": "boolean"}, "s": {"type": "string"}}
        out = video_inputs.coerce_form_scalars({"n": "3", "x": "1.5", "b": "true", "s": "7", "n2": "q"}, specs)
        self.assertEqual(out, {"n": 3, "x": 1.5, "b": True, "s": "7", "n2": "q"})

    def test_suffix_prefers_filename_then_mime(self) -> None:
        self.assertEqual(video_inputs.suffix_for_video("a.MOV"), ".mov")
        self.assertEqual(video_inputs.suffix_for_video("a.bin", "video/webm"), ".webm")
        self.assertEqual(video_inputs.suffix_for_video(None, None), ".mp4")


if __name__ == "__main__":
    unittest.main()
