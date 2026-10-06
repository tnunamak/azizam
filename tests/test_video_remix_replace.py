"""Video remix (Bernini-R) and character replace (SCAIL-2) on the video routes.

Drives the real FastAPI routes against a fake ComfyUI, with the shipped
proxy_config.yml and workflow JSON, and inspects the graph the gateway submits.
"""

from __future__ import annotations

import base64
import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import video_inputs  # noqa: E402
from _fake_comfy_video import (  # noqa: E402
    NEW_PROMPT_ID,
    SOURCE_BYTES,
    SOURCE_PROMPT_ID,
    FakeComfy,
)

SOURCE_ID = f"video_{SOURCE_PROMPT_ID}"
NEW_ID = f"video_{NEW_PROMPT_ID}"
PROBE_24 = video_inputs.VideoProbe(fps=24.0, duration=3.5, frames=84, width=832, height=480)
NS = "com.azizam.workflow"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class RemixReplaceBase(unittest.TestCase):
    probe = PROBE_24

    def setUp(self) -> None:
        self.comfy = FakeComfy()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for p in self.comfy.patches(probe=self.probe):
            self.stack.enter_context(p)
        self.stack.enter_context(patch.object(proxy, "AUTH_ENABLED", False))
        self.stack.enter_context(patch.object(proxy, "_enforce_runtime_route_policy", return_value=None))
        for name in ("_video_job_records", "_video_progress", "_video_workflows"):
            saved = dict(getattr(proxy, name))
            getattr(proxy, name).clear()
            self.addCleanup(lambda n=name, s=saved: (getattr(proxy, n).clear(), getattr(proxy, n).update(s)))
        proxy._video_job_records[SOURCE_PROMPT_ID] = {
            "model": "ltx-2.5", "resolved_model": "video-ltx-2.5-t2v", "size": "960x512",
            "seconds": "4", "created_at": 1000, "completed_at": 1100, "owner_key_hash": None,
            "deleted_at": None,
        }
        self.client = TestClient(proxy.app)

    @property
    def graph(self) -> dict:
        self.assertEqual(len(self.comfy.submitted), 1, "expected one submitted graph")
        return self.comfy.submitted[0]


class RemixRouteTests(RemixReplaceBase):
    def test_remix_creates_a_new_job_that_points_at_the_source(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "make it snowy"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], NEW_ID)
        self.assertEqual(body["object"], "video")
        self.assertEqual((body["status"], body["progress"]), ("queued", 0))
        self.assertEqual(body["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(body["model"], "video-remix")
        self.assertEqual(body["azizam_resolved_model"], "video-bernini-r-remix")
        self.assertEqual(body["prompt"], "make it snowy")
        self.assertEqual(body["size"], "832x480")
        self.assertEqual(body["seconds"], "3.4")
        # the source bytes were fetched from ComfyUI /view and re-uploaded as the input
        self.assertEqual(self.comfy.uploads[0][1], SOURCE_BYTES)
        g = self.graph
        self.assertEqual(g["1"]["inputs"]["file"], "up1_source_00001_.mp4")
        self.assertEqual(g["10"]["inputs"]["value"], "make it snowy")
        self.assertEqual(g["22"]["inputs"]["value"], 81)
        self.assertAlmostEqual(g["2"]["inputs"]["duration"], 81.5 / 24, places=3)
        self.assertNotIn("_meta", g)

    def test_remix_record_is_tracked_for_status_and_list(self) -> None:
        self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        record = proxy._video_job_records[NEW_PROMPT_ID]
        self.assertEqual(record["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(record["model"], "video-remix")
        status = self.client.get(f"/v1/videos/{NEW_ID}").json()
        self.assertEqual(status["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(status["azizam_resolved_model"], "video-bernini-r-remix")

    def test_client_model_name_is_echoed(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x", "model": "bernini-r"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "bernini-r")
        self.assertEqual(r.json()["azizam_resolved_model"], "video-bernini-r-remix")

    def test_seed_and_seconds_are_accepted(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x", "seed": 9, "seconds": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["30"]["inputs"]["value"], 9)
        self.assertEqual(self.graph["22"]["inputs"]["value"], 49)

    def test_seconds_beyond_the_source_is_rejected(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x", "seconds": 4})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "seconds")
        self.assertEqual(self.comfy.uploads, [])

    def test_remix_requires_prompt(self) -> None:
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "prompt")

    def test_unknown_source_is_404(self) -> None:
        r = self.client.post("/v1/videos/video_nope/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.comfy.submitted, [])

    def test_source_not_completed_is_rejected_with_video_not_ready(self) -> None:
        self.comfy.history[SOURCE_PROMPT_ID] = {"status": {"completed": False}, "outputs": {}}
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["code"], "video_not_ready")

    def test_other_keys_video_is_hidden(self) -> None:
        proxy._video_job_records[SOURCE_PROMPT_ID]["owner_key_hash"] = "sha256:someone-else"
        with patch.object(proxy, "_asset_owner_key", return_value="sha256:me"):
            r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 404)

    def test_source_video_cannot_be_overridden_by_an_extension(self) -> None:
        r = self.client.post(
            f"/v1/videos/{SOURCE_ID}/remix",
            json={"prompt": "x", "extensions": {NS: {"source_video": "video_other"}}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")

    def test_remix_is_501_when_not_configured(self) -> None:
        with patch.object(proxy, "VIDEO_REMIX_WORKFLOW", None):
            r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 501)
        self.assertEqual(r.json()["error"]["code"], "video_remix_not_configured")

    def test_source_that_is_not_a_readable_video_is_rejected(self) -> None:
        self.stack.enter_context(patch.object(proxy.video_inputs, "probe_video", lambda *a, **k: None))
        self.stack.enter_context(patch.object(proxy.shutil, "which", lambda _n: "/usr/bin/ffprobe"))
        r = self.client.post(f"/v1/videos/{SOURCE_ID}/remix", json={"prompt": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("not a readable video", r.json()["error"]["message"])
        self.assertEqual(self.comfy.uploads, [])


class EditRouteTests(RemixReplaceBase):
    def test_upload_remix_has_no_source_video_id(self) -> None:
        r = self.client.post(
            "/v1/videos/edits",
            data={"prompt": "to watercolor"},
            files={"video": ("clip.mp4", b"uploaded-clip-bytes", "video/mp4")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body["remixed_from_video_id"])
        self.assertEqual(body["azizam_resolved_model"], "video-bernini-r-remix")
        self.assertEqual(self.comfy.uploads[0][0], "source.mp4")
        self.assertEqual(self.comfy.uploads[0][1], b"uploaded-clip-bytes")
        self.assertEqual(self.graph["10"]["inputs"]["value"], "to watercolor")

    def test_existing_video_by_id_sets_remixed_from(self) -> None:
        r = self.client.post("/v1/videos/edits", json={"prompt": "x", "video": {"id": SOURCE_ID}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(self.comfy.uploads[0][1], SOURCE_BYTES)

    def test_form_style_video_id_reference(self) -> None:
        r = self.client.post("/v1/videos/edits", data={"prompt": "x", "video[id]": SOURCE_ID})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["remixed_from_video_id"], SOURCE_ID)

    def test_missing_video_and_prompt_are_rejected(self) -> None:
        self.assertEqual(self.client.post("/v1/videos/edits", json={"prompt": "x"}).json()["error"]["param"], "video")
        self.assertEqual(
            self.client.post("/v1/videos/edits", json={"video": {"id": SOURCE_ID}}).json()["error"]["param"],
            "prompt",
        )

    def test_url_source_is_downloaded(self) -> None:
        class Resp:
            status_code = 200
            content = b"url-clip"
            headers = {"content-type": "video/webm"}

            def raise_for_status(self) -> None:
                return None

        orig = self.comfy.client_class()

        class Client(orig):  # type: ignore[misc, valid-type]
            async def get(self, url, **kwargs):
                if url.startswith("https://example.test/"):
                    return Resp()
                return await super().get(url, **kwargs)

        self.stack.enter_context(patch.object(proxy.httpx, "AsyncClient", Client))
        r = self.client.post(
            "/v1/videos/edits",
            json={"prompt": "x", "video": "https://example.test/a/clip.webm?sig=1"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.comfy.uploads[0][:2], ("source.webm", b"url-clip"))


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

        r = self.client.post("/v1/videos", json={"model": "character-replace", "prompt": "p"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_stable_alias_and_names_resolve(self) -> None:
        for name in ("character-replace", "scail-2", "video-scail-2-replace"):
            self.assertEqual(proxy._resolve_video_workflow(name)[0], "video-scail-2-replace")
        res = proxy.resolve_model_for_operation("videos", "character-replace")
        self.assertEqual(res.effective_model, "video-scail-2-replace")
        for name in ("video-remix", "bernini-r"):
            self.assertEqual(proxy._resolve_video_workflow(name)[0], proxy.VIDEO_REMIX_WORKFLOW)

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
