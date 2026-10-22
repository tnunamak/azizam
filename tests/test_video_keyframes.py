"""Keyframe video workflows: a clip that starts and ends on exact pictures.

Three workflows ship as data next to the first-frame i2v graphs: a first and a
last picture on MiniMax H3 and on LTX-2.5, and H3 with a middle picture as well.
The tests pin the graph files, the manifests that bind them, and what
POST /v1/videos does with the extra picture inputs (URL, data URI, base64,
upload), against a fake ComfyUI and the shipped proxy_config.yml.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_ingestion, media_jobs, media_metadata  # noqa: E402
from gateway.workflow_openapi import ROOT_EXTENSION, schema_name  # noqa: E402
from gateway.manifests import load_workflow_manifest  # noqa: E402
from tests.support.access import access_context, authenticated  # noqa: E402
from tests.media_test_bytes import mp4_bytes  # noqa: E402
from _fake_comfy_video import NEW_PROMPT_ID, FakeComfy, succeeded_history  # noqa: E402

NS = "com.azizam.workflow"
PNG_FIRST = b"\x89PNG\r\n\x1a\n" + b"1" * 64
PNG_LAST = b"\x89PNG\r\n\x1a\n" + b"2" * 64
PNG_MID = b"\x89PNG\r\n\x1a\n" + b"3" * 64
NEW_ID = f"video_{NEW_PROMPT_ID}"

# model -> (parent i2v model, picture inputs beyond `image`)
KEYFRAME_MODELS = {
    "video-minimax-h3-keyframes": ("video-minimax-h3-i2v", ("last_frame",)),
    "video-minimax-h3-keyframes-mid": ("video-minimax-h3-i2v", ("last_frame", "middle_frame")),
    "video-ltx-2.5-keyframes": ("video-ltx-2.5-i2v", ("last_frame",)),
    "video-ltx-2.5-keyframes-mid": ("video-ltx-2.5-i2v", ("last_frame", "mid_frame")),
}
# Nodes of the parent graph that the keyframe graph rewires or renames, and nothing else.
REWIRED = {
    "video-minimax-h3-keyframes": {"9", "18"},
    "video-minimax-h3-keyframes-mid": {"9", "13", "18"},
    "video-ltx-2.5-keyframes": {"12", "13", "41", "42", "43", "47", "60"},
    "video-ltx-2.5-keyframes-mid": {"12", "13", "41", "42", "43", "47", "60"},
}
# Every preprocess node of an LTX keyframe graph is driven by one compression input.
LTX_PREPROCESS = {
    "video-ltx-2.5-keyframes": ("72", "75", "103", "107"),
    "video-ltx-2.5-keyframes-mid": ("72", "75", "103", "107", "122", "124"),
}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _uri(raw: bytes) -> str:
    return "data:image/png;base64," + _b64(raw)


def _graph(name: str) -> dict:
    return json.loads((ROOT / "workflows/videos" / f"{name}.json").read_text())


def _manifest(name: str) -> dict:
    return load_workflow_manifest(ROOT / "manifests/workflows" / f"{name}.yml")


class KeyframeDataTests(unittest.TestCase):
    def test_inputs_bound_to_integer_node_inputs_are_declared_integer(self) -> None:
        signatures = json.loads((ROOT / "tests/fixtures/comfy_node_signatures.json").read_text())
        for model in KEYFRAME_MODELS:
            manifest, graph = _manifest(model), _graph(model)
            for name, targets in manifest["bindings"]["comfyui"]["map"].items():
                for target in targets:
                    signature = signatures[graph[target["node"]]["class_type"]]["input"]
                    types = {**signature.get("required", {}), **signature.get("optional", {})}
                    if types.get(target["input"]) == "INT":
                        self.assertEqual(manifest["inputs"][name]["type"], "integer", (model, name, target))

    def test_every_binding_names_an_existing_node_and_input(self) -> None:
        for model in KEYFRAME_MODELS:
            manifest, graph = _manifest(model), _graph(model)
            bound_inputs = manifest["bindings"]["comfyui"]["map"]
            self.assertEqual(manifest["bindings"]["comfyui"]["workflow"], f"workflows/videos/{model}.json")
            for name, targets in bound_inputs.items():
                self.assertIn(name, manifest["inputs"], (model, name))
                for target in targets:
                    node = graph.get(target["node"])
                    self.assertIsNotNone(node, (model, name, target))
                    self.assertIn(target["input"], node["inputs"], (model, name, target))
            for name in manifest["inputs"]:
                self.assertIn(name, bound_inputs, (model, name))

    def test_picture_inputs_are_required_assets_and_strength_is_bounded(self) -> None:
        for model, (_parent, pictures) in KEYFRAME_MODELS.items():
            inputs = _manifest(model)["inputs"]
            for name in ("image", *pictures):
                self.assertEqual(inputs[name]["type"], "asset", (model, name))
                self.assertTrue(inputs[name]["required"], (model, name))
        for model, name in (("video-ltx-2.5-keyframes", "last_frame_strength"),
                            ("video-ltx-2.5-keyframes-mid", "last_frame_strength"),
                            ("video-ltx-2.5-keyframes-mid", "mid_frame_strength")):
            strength = _manifest(model)["inputs"][name]
            self.assertEqual((strength["type"], strength["minimum"], strength["maximum"], strength["default"]),
                             ("number", 0, 1, 1), (model, name))

    def test_id_follows_the_model_name_the_catalog_derives(self) -> None:
        for model in KEYFRAME_MODELS:
            self.assertEqual(_manifest(model)["id"], "video.generate." + model.removeprefix("video-"))

    def test_license_requirements_and_timing_come_from_the_parent(self) -> None:
        for model, (parent, _pictures) in KEYFRAME_MODELS.items():
            ours, theirs = _manifest(model), _manifest(parent)
            for key in ("license", "requires", "intent", "outputs", "trusted"):
                self.assertEqual(ours[key], theirs[key], (model, key))

    def test_config_entries_point_at_the_graphs_and_need_both_inputs(self) -> None:
        workflows = proxy.config["videos"]["workflows"]
        for model, (parent, _pictures) in KEYFRAME_MODELS.items():
            cfg = workflows[model]
            self.assertEqual(cfg["file"], f"workflows/videos/{model}.json")
            self.assertTrue(cfg["requires_input_reference"])
            self.assertTrue(cfg["requires_extensions"])
            self.assertNotIn("chain_workflow", cfg)
            for key in ("max_seconds", "max_size", "frame_rate", "length_step", "length_offset", "size_multiple"):
                self.assertEqual(cfg.get(key), workflows[parent].get(key), (model, key))
            meta = _graph(model)["_meta"]
            self.assertEqual(meta["name"], model)
            self.assertIn("inputs", _graph(model)[meta["image_node"]])

    def test_short_names_resolve(self) -> None:
        for alias, model in (
            ("minimax-h3-keyframes", "video-minimax-h3-keyframes"),
            ("h3-keyframes", "video-minimax-h3-keyframes"),
            ("minimax-h3-keyframes-mid", "video-minimax-h3-keyframes-mid"),
            ("h3-keyframes-mid", "video-minimax-h3-keyframes-mid"),
            ("ltx-2.5-keyframes", "video-ltx-2.5-keyframes"),
            ("ltx-2.5-keyframes-mid", "video-ltx-2.5-keyframes-mid"),
            *((m, m) for m in KEYFRAME_MODELS),
        ):
            self.assertEqual(proxy._resolve_video_workflow(alias)[0], model, alias)

    def test_graph_is_the_parent_graph_plus_the_added_nodes(self) -> None:
        for model, (parent, _pictures) in KEYFRAME_MODELS.items():
            ours, theirs = _graph(model), _graph(parent)
            changed = {k for k in theirs if k != "_meta" and ours[k] != theirs[k]}
            self.assertEqual(changed, REWIRED[model], model)
            added = set(ours) - set(theirs)
            self.assertTrue(added, model)
            # nothing the parent graph had is gone, and the sampling settings are untouched
            self.assertTrue(set(theirs) <= set(ours), model)
            for key in ("model_file", "frame_rate", "default_steps", "max_seconds", "default_size"):
                self.assertEqual(ours["_meta"].get(key), theirs["_meta"].get(key), (model, key))

    def test_h3_last_picture_is_cropped_like_the_first_and_fed_to_last_frame(self) -> None:
        for model in ("video-minimax-h3-keyframes", "video-minimax-h3-keyframes-mid"):
            g = _graph(model)
            self.assertEqual(g["9"]["inputs"]["first_frame"], ["21", 0])
            self.assertEqual(g["9"]["inputs"]["last_frame"], ["23", 0])
            self.assertEqual(g["22"]["class_type"], "LoadImage")
            resize = copy.deepcopy(g["23"])
            self.assertEqual(resize["inputs"].pop("image"), ["22", 0])
            first_resize = copy.deepcopy(g["21"])
            first_resize["inputs"].pop("image")
            self.assertEqual(resize, first_resize)

    def test_h3_without_a_middle_picture_has_no_guide_node(self) -> None:
        classes = {n["class_type"] for k, n in _graph("video-minimax-h3-keyframes").items() if k != "_meta"}
        self.assertNotIn("MiniMaxH3AddGuide", classes)

    def test_h3_middle_picture_is_anchored_at_half_the_frame_count(self) -> None:
        g = _graph("video-minimax-h3-keyframes-mid")
        self.assertEqual(g["24"]["class_type"], "LoadImage")
        self.assertEqual(g["25"]["inputs"]["image"], ["24", 0])
        self.assertEqual(g["26"]["inputs"], {"expression": "a // 2", "values.a": ["8", 0]})  # node 8 holds the length
        guide = g["27"]["inputs"]
        self.assertEqual(guide["image"], ["25", 0])
        self.assertEqual(guide["frame_idx"], ["26", 1])  # the integer output
        self.assertEqual((guide["positive"], guide["latent"]), (["9", 0], ["9", 1]))
        self.assertEqual(g["13"]["inputs"]["conditioning"], ["27", 0])
        self.assertEqual(g["14"]["inputs"]["latent_image"], ["9", 1])

    def test_ltx_last_picture_is_a_guide_on_the_final_frame_at_both_stages(self) -> None:
        g = _graph("video-ltx-2.5-keyframes")
        self.assertEqual(g["100"]["inputs"]["value"], 1.0)
        for resize, pre, guide, crop, parent_resize, latent in (
            ("102", "103", "104", "105", "71", ["73", 0]),  # stage 1, half size
            ("106", "107", "108", "109", "74", ["76", 0]),  # stage 2, full size
        ):
            expected = copy.deepcopy(g[parent_resize])
            expected["inputs"]["image"] = ["101", 0]
            self.assertEqual(g[resize], expected)
            self.assertEqual(g[pre]["class_type"], "LTXVPreprocess")
            self.assertEqual(g[pre]["inputs"], {"image": [resize, 0], "img_compression": 18})
            inputs = g[guide]["inputs"]
            self.assertEqual(g[guide]["class_type"], "LTXVAddGuide")
            self.assertEqual(inputs["frame_idx"], -1)
            self.assertEqual(inputs["strength"], ["100", 0])
            self.assertEqual((inputs["image"], inputs["latent"]), ([pre, 0], latent))
            self.assertEqual(g[crop]["class_type"], "LTXVCropGuides")
            self.assertEqual((g[crop]["inputs"]["positive"], g[crop]["inputs"]["negative"]),
                             ([guide, 0], [guide, 1]))
        # the guided latents and conditioning feed the samplers; the guide frames are cropped after each
        self.assertEqual(g["12"]["inputs"]["video_latent"], ["104", 2])
        self.assertEqual(g["13"]["inputs"]["positive"], ["104", 0])
        self.assertEqual(g["105"]["inputs"]["latent"], ["17", 0])
        self.assertEqual(g["41"]["inputs"]["samples"], ["105", 2])
        self.assertEqual(g["42"]["inputs"]["video_latent"], ["108", 2])
        self.assertEqual(g["43"]["inputs"]["positive"], ["108", 0])
        self.assertEqual(g["109"]["inputs"]["latent"], ["46", 0])
        self.assertEqual(g["47"]["inputs"]["samples"], ["109", 2])
        # the first-frame strength of stage 2 is still the one the route's image_strength sets
        self.assertEqual(g["_meta"]["image_strength_node"], "79")
        self.assertEqual(g["76"]["inputs"]["strength"], ["79", 0])
        self.assertEqual(g["79"]["inputs"]["value"], 0.7)

    def test_ltx_mid_picture_is_a_guide_on_the_middle_frame_at_both_stages(self) -> None:
        g = _graph("video-ltx-2.5-keyframes-mid")
        self.assertEqual(g["125"]["class_type"], "PrimitiveFloat")
        self.assertEqual(g["125"]["inputs"]["value"], 1.0)
        # The omitted mid_frame_index defaults to half the frame count, like H3.
        self.assertEqual(g["126"]["class_type"], "ComfyMathExpression")
        self.assertEqual(g["126"]["inputs"], {"expression": "a // 2", "values.a": ["22", 0]})
        for resize, pre, guide, crop, parent_resize, (last_guide, last_crop) in (
            ("121", "122", "110", "105", "71", ("104", "105")),  # stage 1, half size
            ("123", "124", "111", "109", "74", ("108", "109")),  # stage 2, full size
        ):
            expected = copy.deepcopy(g[parent_resize])
            expected["inputs"]["image"] = ["120", 0]
            self.assertEqual(g[resize], expected)
            self.assertEqual(g[pre]["class_type"], "LTXVPreprocess")
            self.assertEqual(g[pre]["inputs"], {"image": [resize, 0], "img_compression": 18})
            inputs = g[guide]["inputs"]
            self.assertEqual(g[guide]["class_type"], "LTXVAddGuide")
            self.assertEqual(inputs["frame_idx"], ["126", 1])
            self.assertEqual(inputs["strength"], ["125", 0])
            self.assertEqual((inputs["image"], inputs["latent"]), ([pre, 0], [last_guide, 2]))
            # The mid guide follows the last-frame guide; one crop removes both.
            self.assertEqual((inputs["positive"], inputs["negative"]),
                             ([last_guide, 0], [last_guide, 1]))
            self.assertEqual(g[crop]["class_type"], "LTXVCropGuides")
            self.assertEqual((g[crop]["inputs"]["positive"], g[crop]["inputs"]["negative"]),
                             ([guide, 0], [guide, 1]))
        self.assertEqual(g["120"]["class_type"], "LoadImage")
        # the guided latents and conditioning feed the samplers
        self.assertEqual(g["12"]["inputs"]["video_latent"], ["110", 2])
        self.assertEqual(g["13"]["inputs"]["positive"], ["110", 0])
        self.assertEqual(g["42"]["inputs"]["video_latent"], ["111", 2])
        self.assertEqual(g["43"]["inputs"]["positive"], ["111", 0])
        self.assertEqual(g["105"]["inputs"]["latent"], ["17", 0])
        self.assertEqual(g["109"]["inputs"]["latent"], ["46", 0])
        self.assertEqual(g["60"]["inputs"]["filename_prefix"], "video/ltx-2.5-keyframes-mid")

    def test_every_ltx_preprocess_node_is_bound_to_img_compression(self) -> None:
        for model, nodes in LTX_PREPROCESS.items():
            manifest, graph = _manifest(model), _graph(model)
            targets = manifest["bindings"]["comfyui"]["map"]["img_compression"]
            self.assertEqual({t["node"] for t in targets}, set(nodes), model)
            for node in nodes:
                self.assertEqual(graph[node]["class_type"], "LTXVPreprocess", (model, node))
                self.assertEqual(graph[node]["inputs"]["img_compression"], 18, (model, node))
            spec = manifest["inputs"]["img_compression"]
            self.assertEqual((spec["type"], spec["minimum"], spec["maximum"], spec["default"]),
                             ("integer", 0, 100, 18), model)

    def test_frames_is_bound_to_each_length_node_on_a_documented_grid(self) -> None:
        for model, (node, step, offset, literal) in (
            ("video-ltx-2.5-keyframes", ("22", 8, 1, 121)),
            ("video-ltx-2.5-keyframes-mid", ("22", 8, 1, 121)),
            ("video-minimax-h3-keyframes", ("8", 17, 5, 124)),
            ("video-minimax-h3-keyframes-mid", ("8", 17, 5, 124)),
        ):
            manifest, graph = _manifest(model), _graph(model)
            spec = manifest["inputs"]["frames"]
            self.assertEqual((spec["type"], spec["step"], spec["offset"]),
                             ("integer", step, offset), model)
            self.assertEqual(manifest["bindings"]["comfyui"]["map"]["frames"],
                             [{"node": node, "input": "value"}], model)
            self.assertEqual(graph[node]["inputs"]["value"], literal, model)

    def test_mid_frame_index_is_optional_and_the_graph_computes_the_default(self) -> None:
        ltx = _manifest("video-ltx-2.5-keyframes-mid")
        self.assertEqual(ltx["bindings"]["comfyui"]["map"]["mid_frame_index"],
                         [{"node": "110", "input": "frame_idx"}, {"node": "111", "input": "frame_idx"}])
        self.assertEqual(ltx["inputs"]["mid_frame_index"]["type"], "integer")
        self.assertEqual(ltx["inputs"]["mid_frame_index"]["minimum"], 1)
        h3 = _manifest("video-minimax-h3-keyframes-mid")
        self.assertEqual(h3["bindings"]["comfyui"]["map"]["mid_frame_index"],
                         [{"node": "27", "input": "frame_idx"}])
        self.assertNotIn("default", h3["inputs"]["mid_frame_index"])
        # Every keyframe model with a middle guide can set its strength except H3, whose
        # guide node has no strength input.
        self.assertEqual(ltx["inputs"]["mid_frame_strength"]["default"], 1)
        self.assertNotIn("mid_frame_strength", h3["inputs"])

    def test_parent_i2v_files_are_untouched(self) -> None:
        # The keyframe graphs are derived, never edited in place: the parents still pin one frame only.
        for parent in ("video-minimax-h3-i2v", "video-ltx-2.5-i2v"):
            classes = {n["class_type"] for k, n in _graph(parent).items() if k != "_meta"}
            self.assertFalse(classes & {"MiniMaxH3AddGuide", "LTXVAddGuide", "LTXVCropGuides"}, parent)
            self.assertNotIn("last_frame", _graph(parent).get("9", {}).get("inputs", {}))
            self.assertNotIn("last_frame", _manifest(parent)["bindings"]["comfyui"]["map"])


@pytest.mark.usefixtures("isolated_receipts")
class KeyframeRouteBase(unittest.TestCase):
    def setUp(self) -> None:
        self.comfy = FakeComfy()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for p in self.comfy.patches():
            self.stack.enter_context(p)
        self.stack.enter_context(authenticated())
        self.stack.enter_context(patch.object(proxy, "_enforce_runtime_route_policy", return_value=None))
        for name in ("_video_job_records", "_video_progress", "_video_workflows"):
            saved = dict(getattr(proxy, name))
            getattr(proxy, name).clear()
            self.addCleanup(lambda n=name, s=saved: (getattr(proxy, n).clear(), getattr(proxy, n).update(s)))
        self.client = TestClient(proxy.app)

    @property
    def graph(self) -> dict:
        self.assertEqual(len(self.comfy.submitted), 1, "expected one submitted graph")
        return self.comfy.submitted[0]

    def uploaded(self, node: str) -> bytes:
        """The bytes ComfyUI received for the LoadImage `node` of the submitted graph."""
        name = self.graph[node]["inputs"]["image"]
        index = int(name.split("_", 1)[0].removeprefix("up")) - 1
        filename, data, _mime = self.comfy.uploads[index]
        self.assertEqual(name, f"up{index + 1}_{filename}")
        return data

    def assert_refused(self, response, *, param: str) -> None:
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["param"], param)
        self.assertEqual(self.comfy.uploads, [], "a refused request must leave no files in ComfyUI")
        self.assertEqual(self.comfy.submitted, [])


class KeyframeJsonRequestTests(KeyframeRouteBase):
    def _body(self, model="video-minimax-h3-keyframes", **ext) -> dict:
        extensions = {"last_frame": _uri(PNG_LAST), **ext}
        return {"model": model, "prompt": "a lighthouse at dusk", "seconds": 5, "seed": 7,
                "input_reference": _uri(PNG_FIRST), "extensions": {NS: extensions}}

    def test_h3_first_and_last_picture_reach_their_load_nodes(self) -> None:
        r = self.client.post("/v1/videos", json=self._body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.comfy.uploads), 2)
        self.assertEqual(len(self.comfy.submitted), 1)
        self.assertEqual(r.json()["azizam_resolved_model"], "video-minimax-h3-keyframes")
        g = self.graph
        self.assertEqual(self.uploaded("20"), PNG_FIRST)
        self.assertEqual(self.uploaded("22"), PNG_LAST)
        self.assertEqual(g["9"]["inputs"]["prompt"], "a lighthouse at dusk")
        self.assertEqual(g["8"]["inputs"]["value"], 124)
        self.assertEqual(g["10"]["inputs"]["noise_seed"], 7)
        self.assertEqual(g["9"]["inputs"]["last_frame"], ["23", 0])
        self.assertNotIn("_meta", g)

    def test_each_input_form_is_accepted_for_the_last_picture(self) -> None:
        fetched = proxy.media_fetch.FetchedMedia(PNG_LAST, "end.png", "image/png")
        fetch = self.stack.enter_context(patch.object(proxy, "_fetch_media_url", AsyncMock(return_value=fetched)))
        forms = {
            "data_uri": _uri(PNG_LAST),
            "base64": _b64(PNG_LAST),
            "url": "https://example.test/pics/end.png?sig=1",
            "url_object": {"url": "https://example.test/pics/end.png?sig=1"},
        }
        for label, value in forms.items():
            with self.subTest(label):
                self.comfy.uploads.clear()
                self.comfy.submitted.clear()
                body = self._body()
                body["extensions"][NS]["last_frame"] = value
                r = self.client.post("/v1/videos", json=body)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.uploaded("22"), PNG_LAST)
                self.assertEqual(self.uploaded("20"), PNG_FIRST)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(fetch.call_args.args[0], "https://example.test/pics/end.png?sig=1")

    def test_inputs_block_is_the_same_as_the_extension_namespace(self) -> None:
        body = self._body()
        body["inputs"] = body.pop("extensions")[NS]
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.uploaded("22"), PNG_LAST)

    def test_ltx_strengths_default_and_override(self) -> None:
        r = self.client.post("/v1/videos", json=self._body("video-ltx-2.5-keyframes"))
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertEqual(self.uploaded("70"), PNG_FIRST)
        self.assertEqual(self.uploaded("101"), PNG_LAST)
        self.assertEqual(g["100"]["inputs"]["value"], 1.0)
        self.assertEqual(g["79"]["inputs"]["value"], 0.7)
        self.assertEqual(g["5"]["inputs"]["text"], "a lighthouse at dusk")
        self.assertEqual(g["22"]["inputs"]["value"], 121)

        self.comfy.submitted.clear()
        self.comfy.uploads.clear()
        body = self._body("video-ltx-2.5-keyframes", last_frame_strength=0.4)
        body["image_strength"] = 0.9
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["100"]["inputs"]["value"], 0.4)
        self.assertEqual(self.graph["79"]["inputs"]["value"], 0.9)

    def test_ltx_last_frame_strength_out_of_range_is_refused(self) -> None:
        r = self.client.post("/v1/videos", json=self._body("video-ltx-2.5-keyframes", last_frame_strength=1.5))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(self.comfy.submitted, [])

    def test_ltx_mid_picture_strength_and_index_override(self) -> None:
        body = self._body("video-ltx-2.5-keyframes-mid", mid_frame=_uri(PNG_MID))
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertEqual([self.uploaded(n) for n in ("70", "101", "120")],
                         [PNG_FIRST, PNG_LAST, PNG_MID])
        self.assertEqual(g["125"]["inputs"]["value"], 1.0)
        self.assertEqual(g["110"]["inputs"]["frame_idx"], ["126", 1])
        self.assertEqual(g["111"]["inputs"]["frame_idx"], ["126", 1])
        self.assertEqual(g["126"]["inputs"]["values.a"], ["22", 0])

        self.comfy.submitted.clear()
        self.comfy.uploads.clear()
        body = self._body("video-ltx-2.5-keyframes-mid", mid_frame=_uri(PNG_MID),
                          mid_frame_strength=0.35, mid_frame_index=30)
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertEqual(g["125"]["inputs"]["value"], 0.35)
        self.assertEqual(g["110"]["inputs"]["frame_idx"], 30)
        self.assertEqual(g["111"]["inputs"]["frame_idx"], 30)

    def test_ltx_mid_index_out_of_range_or_off_grid_is_refused(self) -> None:
        for invalid in ({"mid_frame_index": 0}, {"mid_frame_index": -1}, {"mid_frame_index": 1.5}):
            with self.subTest(invalid=invalid):
                body = self._body("video-ltx-2.5-keyframes-mid", mid_frame=_uri(PNG_MID), **invalid)
                r = self.client.post("/v1/videos", json=body)
                self.assert_refused(r, param="mid_frame_index")

    def test_frames_accepts_the_grid_and_refuses_a_240_frame_request(self) -> None:
        for model, valid, invalid, node in (
            ("video-ltx-2.5-keyframes", 241, 240, "22"),
            ("video-minimax-h3-keyframes", 243, 240, "8"),
        ):
            with self.subTest(model=model, valid=valid):
                self.comfy.submitted.clear()
                self.comfy.uploads.clear()
                r = self.client.post("/v1/videos", json=self._body(model, frames=valid))
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.graph[node]["inputs"]["value"], valid)
            with self.subTest(model=model, invalid=invalid):
                self.comfy.uploads.clear()
                self.comfy.submitted.clear()
                r = self.client.post("/v1/videos", json=self._body(model, frames=invalid))
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["error"]["param"], "frames")
                self.assertIn("frames", r.json()["error"]["message"])
                self.assertEqual(self.comfy.uploads, [])
                self.assertEqual(self.comfy.submitted, [])

    def test_frames_wins_over_seconds(self) -> None:
        body = self._body("video-ltx-2.5-keyframes", frames=241)
        body["seconds"] = 5
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["22"]["inputs"]["value"], 241)
        self.assertEqual(r.json()["actual_seconds"], round(241 / 24, 4))

    def test_img_compression_reaches_every_preprocess_node(self) -> None:
        for model, nodes in LTX_PREPROCESS.items():
            with self.subTest(model=model):
                self.comfy.submitted.clear()
                self.comfy.uploads.clear()
                extras = {"img_compression": 7}
                if model == "video-ltx-2.5-keyframes-mid":
                    extras["mid_frame"] = _uri(PNG_MID)
                r = self.client.post("/v1/videos", json=self._body(model, **extras))
                self.assertEqual(r.status_code, 200, r.text)
                g = self.graph
                for node in nodes:
                    self.assertEqual(g[node]["inputs"]["img_compression"], 7, (model, node))
        for invalid in (101, -1, 1.5):
            with self.subTest(invalid=invalid):
                self.comfy.uploads.clear()
                self.comfy.submitted.clear()
                r = self.client.post("/v1/videos", json=self._body("video-ltx-2.5-keyframes", img_compression=invalid))
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["error"]["param"], "img_compression")
                self.assertEqual(self.comfy.uploads, [])
                self.assertEqual(self.comfy.submitted, [])

    def test_invalid_extensions_are_refused_before_asset_resolution(self) -> None:
        for model in (*KEYFRAME_MODELS, "video-minimax-h3-i2v", "video-ltx-2.5-i2v"):
            invalid_inputs = [{"unknown_input": 1}, {"image": _uri(PNG_FIRST)}, {"last_frame": 42}]
            if model in ("video-ltx-2.5-keyframes", "video-ltx-2.5-keyframes-mid"):
                invalid_inputs.extend([{"last_frame_strength": 1.5}, {"last_frame_strength": []}])
            if model == "video-ltx-2.5-keyframes-mid":
                invalid_inputs.extend([{"mid_frame_strength": 1.5}, {"mid_frame_strength": []}])
            for invalid in invalid_inputs:
                with self.subTest(model=model, invalid=invalid):
                    body = self._body(model, middle_frame=_uri(PNG_MID))
                    if model == "video-ltx-2.5-keyframes-mid":
                        body["extensions"][NS]["mid_frame"] = _uri(PNG_MID)
                    elif model != "video-minimax-h3-keyframes-mid":
                        del body["extensions"][NS]["middle_frame"]
                    body["extensions"][NS].update(invalid)
                    with patch.object(proxy, "_resolve_input_reference", AsyncMock(wraps=proxy._resolve_input_reference)) as resolve:
                        r = self.client.post("/v1/videos", json=body)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")
                    resolve.assert_not_awaited()
                    self.assertEqual(self.comfy.uploads, [])
                    self.assertEqual(self.comfy.submitted, [])

    def test_h3_middle_picture(self) -> None:
        body = self._body("video-minimax-h3-keyframes-mid", middle_frame=_uri(PNG_MID))
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertEqual(self.uploaded("20"), PNG_FIRST)
        self.assertEqual(self.uploaded("22"), PNG_LAST)
        self.assertEqual(self.uploaded("24"), PNG_MID)
        self.assertEqual(g["8"]["inputs"]["value"], 124)  # the guide frame index is half of this, in the graph
        self.assertEqual(g["26"]["inputs"]["values.a"], ["8", 0])

    def test_missing_last_frame_is_refused_before_any_upload(self) -> None:
        for model in ("video-minimax-h3-keyframes", "video-ltx-2.5-keyframes", "video-minimax-h3-keyframes-mid"):
            with self.subTest(model):
                body = self._body(model, middle_frame=_uri(PNG_MID))
                del body["extensions"][NS]["last_frame"]
                r = self.client.post("/v1/videos", json=body)
                self.assert_refused(r, param=f"extensions.{NS}.last_frame")
                self.assertIn("last_frame is required", r.json()["error"]["message"])

    def test_no_extensions_at_all_is_refused(self) -> None:
        body = self._body()
        del body["extensions"]
        self.assert_refused(self.client.post("/v1/videos", json=body), param=f"extensions.{NS}.last_frame")

    def test_missing_middle_frame_is_refused(self) -> None:
        r = self.client.post("/v1/videos", json=self._body("video-minimax-h3-keyframes-mid"))
        self.assert_refused(r, param=f"extensions.{NS}.middle_frame")

    def test_missing_first_picture_is_refused(self) -> None:
        body = self._body()
        del body["input_reference"]
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["param"], "input_reference")
        self.assertEqual(self.comfy.submitted, [])

    def test_first_picture_cannot_be_sent_as_an_extension(self) -> None:
        r = self.client.post("/v1/videos", json=self._body(image=_uri(PNG_FIRST)))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("input_reference", r.json()["error"]["message"])
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(self.comfy.submitted, [])

    def test_an_unreadable_last_picture_is_refused(self) -> None:
        body = self._body()
        body["extensions"][NS]["last_frame"] = "not a picture or a link!"
        r = self.client.post("/v1/videos", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["param"], f"extensions.{NS}.last_frame")
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(self.comfy.submitted, [])

    def test_unreadable_middle_picture_is_refused_before_any_upload(self) -> None:
        body = self._body("video-minimax-h3-keyframes-mid", middle_frame="not a picture or a link!")
        self.assert_refused(self.client.post("/v1/videos", json=body), param=f"extensions.{NS}.middle_frame")

    def test_empty_and_oversized_assets_are_refused_before_any_upload(self) -> None:
        for data, message in ((b"", "empty"), (PNG_LAST, "larger than")):
            with self.subTest(message=message):
                body = self._body()
                with patch.object(proxy, "_resolve_video_workflow_asset", AsyncMock(return_value=(data, "end.png", "image/png"))), \
                     patch.object(proxy.video_inputs, "MAX_SOURCE_VIDEO_BYTES", len(PNG_LAST) - 1):
                    response = self.client.post("/v1/videos", json=body)
                self.assert_refused(response, param=f"extensions.{NS}.last_frame")
                self.assertIn(message, response.json()["error"]["message"])

    def test_invalid_seconds_are_refused_before_fetching_a_url_asset(self) -> None:
        body = self._body(last_frame="https://example.test/end.png")
        body["seconds"] = 99
        with patch.object(proxy, "_fetch_media_url", AsyncMock(side_effect=AssertionError("unexpected fetch"))) as fetch:
            response = self.client.post("/v1/videos", json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("seconds must be between", response.json()["error"]["message"])
        fetch.assert_not_awaited()
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(self.comfy.submitted, [])

    def test_first_frame_only_models_do_not_take_a_last_picture(self) -> None:
        for model in ("video-minimax-h3-i2v", "video-ltx-2.5-i2v"):
            with self.subTest(model):
                r = self.client.post("/v1/videos", json=self._body(model))
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")
                self.assertEqual(self.comfy.submitted, [])


class KeyframeMultipartRequestTests(KeyframeRouteBase):
    """The OpenAI SDK sends videos.create as multipart; pictures can be files."""

    def test_unknown_file_parts_are_refused_without_reading(self) -> None:
        for name in ("unknown_file", "prompt", "extensions[other][picture]", "inputs[last_frame][extra]"):
            with self.subTest(name=name):
                read = AsyncMock(side_effect=AssertionError("unknown part was read"))
                upload = SimpleNamespace(filename="large.bin", read=read, close=AsyncMock())
                form = FormData([(name, upload)])
                with patch.object(proxy.request_forms, "read_form", AsyncMock(return_value=form)):
                    response = self.client.post("/v1/videos", data={"model": "irrelevant"})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "unknown_parameter")
                self.assertEqual(response.json()["error"]["param"], name)
                read.assert_not_awaited()
                self.assertEqual(self.comfy.uploads, [])

    def test_inputs_picture_as_file_upload(self) -> None:
        response = self.client.post(
            "/v1/videos", data={"model": "video-minimax-h3-keyframes", "prompt": "p", "seconds": "5"},
            files={"input_reference": ("first.png", PNG_FIRST, "image/png"),
                   "inputs[last_frame]": ("last.png", PNG_LAST, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.uploaded("22"), PNG_LAST)

    def test_both_pictures_as_file_uploads(self) -> None:
        r = self.client.post(
            "/v1/videos",
            data={"model": "video-ltx-2.5-keyframes", "prompt": "p", "seconds": "2", "seed": "5",
                  f"extensions[{NS}][last_frame_strength]": "0.5"},
            files={"input_reference": ("first.png", PNG_FIRST, "image/png"),
                   f"extensions[{NS}][last_frame]": ("last.png", PNG_LAST, "image/png")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertEqual(self.uploaded("70"), PNG_FIRST)
        self.assertEqual(self.uploaded("101"), PNG_LAST)
        self.assertEqual(g["100"]["inputs"]["value"], 0.5)
        self.assertEqual(g["30"]["inputs"]["noise_seed"], 5)

    def test_last_picture_as_text_forms(self) -> None:
        fetched = proxy.media_fetch.FetchedMedia(PNG_LAST, "end.png", "image/png")
        self.stack.enter_context(patch.object(proxy, "_fetch_media_url", AsyncMock(return_value=fetched)))
        for label, value in (("data_uri", _uri(PNG_LAST)), ("base64", _b64(PNG_LAST)), ("url", "https://example.test/e.png")):
            with self.subTest(label):
                self.comfy.uploads.clear()
                self.comfy.submitted.clear()
                r = self.client.post(
                    "/v1/videos",
                    data={"model": "video-minimax-h3-keyframes", "prompt": "p", "seconds": "5",
                          "input_reference": _uri(PNG_FIRST), f"extensions[{NS}][last_frame]": value},
                )
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.uploaded("22"), PNG_LAST)

    def test_middle_picture_as_file_upload(self) -> None:
        r = self.client.post(
            "/v1/videos",
            data={"model": "video-minimax-h3-keyframes-mid", "prompt": "p", "seconds": "5"},
            files={"input_reference": ("a.png", PNG_FIRST, "image/png"),
                   f"extensions[{NS}][last_frame]": ("b.png", PNG_LAST, "image/png"),
                   f"extensions[{NS}][middle_frame]": ("c.png", PNG_MID, "image/png")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([self.uploaded(n) for n in ("20", "22", "24")], [PNG_FIRST, PNG_LAST, PNG_MID])

    def test_missing_last_picture_is_refused(self) -> None:
        r = self.client.post(
            "/v1/videos",
            data={"model": "video-minimax-h3-keyframes", "prompt": "p"},
            files={"input_reference": ("a.png", PNG_FIRST, "image/png")},
        )
        self.assert_refused(r, param=f"extensions.{NS}.last_frame")


class FirstFrameOnlyParityTests(KeyframeRouteBase):
    """Requests that omit the new inputs build the graph they always built."""

    def _expected(self, parent: str, edits: dict[str, dict[str, object]]) -> dict:
        graph = _graph(parent)
        graph.pop("_meta")
        for node, fields in edits.items():
            graph[node]["inputs"].update(fields)
        return graph

    def test_ltx_i2v_graph_is_the_stored_graph_with_only_the_request_values(self) -> None:
        r = self.client.post("/v1/videos", json={
            "model": "video-ltx-2.5-i2v", "prompt": "p", "seconds": 5, "seed": 3, "size": "1280x704",
            "input_reference": _uri(PNG_FIRST)})
        self.assertEqual(r.status_code, 200, r.text)
        expected = self._expected("video-ltx-2.5-i2v", {
            "5": {"text": "p"}, "20": {"value": 1280}, "21": {"value": 704}, "22": {"value": 121},
            "23": {"value": 24.0}, "30": {"noise_seed": 3}, "31": {"noise_seed": 3},
            "70": {"image": "up1_input_reference.png"}, "79": {"value": 0.7},
        })
        self.assertEqual(json.dumps(self.graph, sort_keys=True), json.dumps(expected, sort_keys=True))

    def test_h3_i2v_graph_is_the_stored_graph_with_only_the_request_values(self) -> None:
        r = self.client.post("/v1/videos", json={
            "model": "video-minimax-h3-i2v", "prompt": "p", "seconds": 5, "seed": 3,
            "input_reference": _uri(PNG_FIRST)})
        self.assertEqual(r.status_code, 200, r.text)
        expected = self._expected("video-minimax-h3-i2v", {
            "9": {"prompt": "p"}, "6": {"value": 960}, "7": {"value": 544}, "8": {"value": 124},
            "17": {"fps": 24.0}, "10": {"noise_seed": 3}, "20": {"image": "up1_input_reference.png"},
        })
        self.assertEqual(json.dumps(self.graph, sort_keys=True), json.dumps(expected, sort_keys=True))

    def test_keyframe_graphs_are_byte_identical_when_new_fields_are_omitted(self) -> None:
        for model, edits in (
            ("video-ltx-2.5-keyframes", {
                "5": {"text": "p"}, "20": {"value": 960}, "21": {"value": 512}, "22": {"value": 121},
                "23": {"value": 24.0}, "30": {"noise_seed": 3}, "31": {"noise_seed": 3},
                "70": {"image": "up1_input_reference.png"}, "101": {"image": "up2_input_reference.png"},
            }),
            ("video-minimax-h3-keyframes", {
                "9": {"prompt": "p"}, "6": {"value": 960}, "7": {"value": 544}, "8": {"value": 124},
                "17": {"fps": 24.0}, "10": {"noise_seed": 3}, "20": {"image": "up1_input_reference.png"},
                "22": {"image": "up2_input_reference.png"},
            }),
            ("video-minimax-h3-keyframes-mid", {
                "9": {"prompt": "p"}, "6": {"value": 960}, "7": {"value": 544}, "8": {"value": 124},
                "17": {"fps": 24.0}, "10": {"noise_seed": 3}, "20": {"image": "up1_input_reference.png"},
                "22": {"image": "up2_input_reference.png"}, "24": {"image": "up3_input_reference.png"},
            }),
            ("video-ltx-2.5-keyframes-mid", {
                "5": {"text": "p"}, "20": {"value": 960}, "21": {"value": 512}, "22": {"value": 121},
                "23": {"value": 24.0}, "30": {"noise_seed": 3}, "31": {"noise_seed": 3},
                "70": {"image": "up1_input_reference.png"}, "101": {"image": "up2_input_reference.png"},
                "120": {"image": "up3_input_reference.png"},
            }),
        ):
            with self.subTest(model=model):
                self.maxDiff = None
                self.comfy.submitted.clear()
                self.comfy.uploads.clear()
                body = {"model": model, "prompt": "p", "seconds": 5, "seed": 3,
                        "input_reference": _uri(PNG_FIRST),
                        "extensions": {NS: {"last_frame": _uri(PNG_LAST)}}}
                if model.endswith("-mid"):
                    body["extensions"][NS]["middle_frame" if model.startswith("video-minimax") else "mid_frame"] = _uri(PNG_MID)
                r = self.client.post("/v1/videos", json=body)
                self.assertEqual(r.status_code, 200, r.text)
                expected = self._expected(model, edits)
                self.assertEqual(json.dumps(self.graph, sort_keys=True), json.dumps(expected, sort_keys=True))

    def test_i2v_multipart_upload_still_works(self) -> None:
        r = self.client.post(
            "/v1/videos", data={"model": "video-minimax-h3-i2v", "prompt": "p", "seconds": "5"},
            files={"input_reference": ("first.png", PNG_FIRST, "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.uploaded("20"), PNG_FIRST)


class KeyframeJobRecordTests(KeyframeRouteBase):
    """A keyframe clip is a normal video job: tracked, listed, and indexed in the Library."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.media_db = root / "media_jobs.db"
        media_jobs.init_db(self.media_db)
        for p in (
            patch.object(media_jobs, "DB_PATH", self.media_db),
            patch.object(proxy, "IMAGE_CACHE_DIR", root / "cache"),
            patch.object(proxy, "_image_url_signing_secret", b"keyframe-test-secret"),
            patch.object(media_ingestion.media_delivery, "attach_provenance", lambda content, suffix, record: content),
        ):
            self.stack.enter_context(p)

    def _create(self) -> dict:
        r = self.client.post("/v1/videos", json={
            "model": "video-minimax-h3-keyframes", "prompt": "a lighthouse", "seconds": 5,
            "input_reference": _uri(PNG_FIRST), "extensions": {NS: {"last_frame": _uri(PNG_LAST)}}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_job_is_tracked_listed_and_reports_status(self) -> None:
        created = self._create()
        self.assertEqual(created["id"], NEW_ID)
        self.assertEqual((created["object"], created["status"]), ("video", "queued"))
        record = proxy._video_job_records[NEW_PROMPT_ID]
        self.assertEqual(record["resolved_model"], "video-minimax-h3-keyframes")
        self.assertEqual(record["provenance"]["workflow_id"], "video.generate.video-minimax-h3-keyframes")
        self.assertEqual(record["size"], "960x544")
        self.assertIsNotNone(record["owner_key_hash"])
        self.assertNotIn("last_frame", json.dumps(record["provenance"]))
        listed = self.client.get("/v1/videos").json()
        self.assertEqual([item["id"] for item in listed["data"]], [NEW_ID])
        self.assertEqual(self.client.get(f"/v1/videos/{NEW_ID}").json()["azizam_resolved_model"],
                         "video-minimax-h3-keyframes")

    def test_finished_clip_lands_in_the_library_as_a_video(self) -> None:
        import anyio

        self._create()
        mp4 = mp4_bytes()
        self.comfy.history[NEW_PROMPT_ID] = succeeded_history("kf_00001_.mp4")
        self.comfy.view_bytes["kf_00001_.mp4"] = mp4

        async def fetch():
            return await proxy.proxy_graph_video_content(NEW_ID, access_context(), metadata_mode="none")

        response = anyio.run(fetch)
        self.assertEqual(response.status_code, 200, getattr(response, "body", b"")[:200])
        job = media_jobs.get_by_request_id(NEW_ID, path=self.media_db)
        assert job is not None
        self.assertEqual(job["intent"], "video.generate")
        self.assertEqual(job["workflow"], "video.generate.video-minimax-h3-keyframes")
        self.assertEqual(job["backend"], "comfyui")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual((job["assets"][0]["kind"], job["assets"][0]["media_type"]), ("video", "video/mp4"))
        self.assertTrue(job["assets"][0]["asset_id"].startswith("vid_"))


HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _mp4_with_audio() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "with-audio.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.25:r=5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
             "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(output)],
            capture_output=True, check=True,
        )
        return output.read_bytes()


def _audio_stream_count(content: bytes) -> int:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "check.mp4"
        source.write_bytes(content)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(source)],
            capture_output=True, check=True,
        )
    return len([line for line in result.stdout.decode().splitlines() if line.strip()])


@pytest.mark.usefixtures("isolated_receipts")
class VideoAudioControlTests(KeyframeRouteBase):
    """`extensions["com.azizam.video"].audio = false` strips the track before storage."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.media_db = root / "media_jobs.db"
        media_jobs.init_db(self.media_db)
        for p in (
            patch.object(media_jobs, "DB_PATH", self.media_db),
            patch.object(proxy, "IMAGE_CACHE_DIR", root / "cache"),
            patch.object(proxy, "_image_url_signing_secret", b"keyframe-test-secret"),
            patch.object(media_ingestion.media_delivery, "attach_provenance", lambda content, suffix, record: content),
        ):
            self.stack.enter_context(p)

    def _create(self, audio: object = None) -> dict:
        controls = {} if audio is None else {"audio": audio}
        body = {
            "model": "video-minimax-h3-keyframes", "prompt": "a lighthouse", "seconds": 5,
            "input_reference": _uri(PNG_FIRST),
            "extensions": {NS: {"last_frame": _uri(PNG_LAST)}, "com.azizam.video": controls},
        }
        response = self.client.post("/v1/videos", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _finished(self, mp4: bytes) -> bytes:
        self.comfy.history[NEW_PROMPT_ID] = succeeded_history("aud_00001_.mp4")
        self.comfy.view_bytes["aud_00001_.mp4"] = mp4

        async def fetch():
            return await proxy.proxy_graph_video_content(NEW_ID, access_context(), metadata_mode="none")

        response = anyio.run(fetch)
        self.assertEqual(response.status_code, 200, getattr(response, "body", b"")[:200])
        return bytes(response.body)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
    def test_audio_false_removes_the_track_from_the_stored_video(self) -> None:
        mp4 = _mp4_with_audio()
        self.assertEqual(_audio_stream_count(mp4), 1)
        created = self._create(audio=False)
        self.assertIs(proxy._video_job_records[created["id"].removeprefix("video_")].get("audio"), False)
        stored = self._finished(mp4)
        self.assertEqual(_audio_stream_count(stored), 0)
        job = media_jobs.get_by_request_id(NEW_ID, path=self.media_db)
        self.assertEqual(job["assets"][0]["media_type"], "video/mp4")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
    def test_absent_audio_leaves_the_bytes_unchanged(self) -> None:
        mp4 = _mp4_with_audio()
        self._create(audio=None)
        stored = self._finished(mp4)
        self.assertEqual(_audio_stream_count(stored), 1)
        self.assertEqual(stored, media_metadata.strip_metadata(mp4, ".mp4"))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
    def test_true_audio_leaves_the_bytes_unchanged(self) -> None:
        mp4 = _mp4_with_audio()
        self._create(audio=True)
        stored = self._finished(mp4)
        self.assertEqual(_audio_stream_count(stored), 1)
        self.assertEqual(stored, media_metadata.strip_metadata(mp4, ".mp4"))

    def test_audio_must_be_a_boolean(self) -> None:
        for invalid in ("no", 0, "false", []):
            with self.subTest(invalid=invalid):
                self.comfy.uploads.clear()
                self.comfy.submitted.clear()
                response = self.client.post("/v1/videos", json={
                    "model": "video-minimax-h3-keyframes", "prompt": "p", "seconds": 5,
                    "input_reference": _uri(PNG_FIRST),
                    "extensions": {NS: {"last_frame": _uri(PNG_LAST)},
                                   "com.azizam.video": {"audio": invalid}},
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["param"], "extensions.com.azizam.video.audio")
                self.assertEqual(self.comfy.uploads, [])
                self.assertEqual(self.comfy.submitted, [])

    def test_unknown_video_control_is_refused(self) -> None:
        response = self.client.post("/v1/videos", json={
            "model": "video-minimax-h3-keyframes", "prompt": "p", "seconds": 5,
            "input_reference": _uri(PNG_FIRST),
            "extensions": {NS: {"last_frame": _uri(PNG_LAST)},
                           "com.azizam.video": {"sound": False}},
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["param"], "extensions.com.azizam.video.sound")
        self.assertEqual(self.comfy.uploads, [])


class KeyframeCatalogTests(unittest.TestCase):
    """The workflow catalog, which the MCP tools read, describes the new inputs from the manifest."""

    @classmethod
    def setUpClass(cls) -> None:
        response = TestClient(proxy.app).get("/openapi.json")
        assert response.status_code == 200, response.text
        cls.spec = response.json()
        cls.catalog = {w["id"]: w for w in cls.spec[ROOT_EXTENSION]}

    def entry(self, model: str) -> dict:
        return self.catalog["video.generate." + model.removeprefix("video-")]

    def test_catalog_places_pictures_as_extension_inputs_and_the_first_as_input_reference(self) -> None:
        for model, (_parent, pictures) in KEYFRAME_MODELS.items():
            execution = self.entry(model)["execution"]
            for route in (execution["sync"], execution["async"]):
                if route is None:
                    continue
                fields = route["fields"]
                self.assertEqual(fields["image"], {"in": "body", "field": "input_reference"}, model)
                for name in pictures:
                    self.assertEqual(fields[name], {"in": "extension", "field": name}, (model, name))
            self.assertEqual(execution["async"]["submit"]["model"], model)
        fields = self.entry("video-ltx-2.5-keyframes")["execution"]["async"]["fields"]
        self.assertEqual(fields["last_frame_strength"], {"in": "extension", "field": "last_frame_strength"})
        self.assertEqual(fields["image_strength"], {"in": "body", "field": "image_strength"})

    def test_input_schema_marks_the_pictures_as_required_assets(self) -> None:
        for model, (_parent, pictures) in KEYFRAME_MODELS.items():
            wid = self.entry(model)["id"]
            schema = self.spec["components"]["schemas"][schema_name(wid)]
            for name in pictures:
                self.assertTrue(schema["properties"][name]["x-azizam-asset"], (model, name))
                self.assertIn(name, schema["required"], (model, name))
            self.assertTrue(schema["properties"]["image"]["x-azizam-asset"], model)

    def test_extension_schema_never_lists_route_managed_inputs(self) -> None:
        routes = []
        for route_paths in (("/v1/videos", "/openai/v1/videos"),):
            for path in route_paths:
                routes.append(path)
        schemas = self.spec["components"]["schemas"]
        for path in routes:
            options = self.spec["paths"][path]["post"]["requestBody"]["content"]["application/json"][
                "schema"]["properties"]["extensions"]["properties"][NS]["anyOf"]
            self.assertEqual(len(options), len({o["$ref"] for o in options}))
            for option in options:
                name = option["$ref"].rsplit("/", 1)[1]
                properties = schemas[name]["properties"]
                self.assertFalse(properties.keys() & {"image", "image_strength", "negative_prompt", "seed", "prompt"},
                                 (path, name))
        # The keyframe pictures and the new data inputs remain extension inputs.
        for model in KEYFRAME_MODELS:
            wid = self.entry(model)["id"]
            extension_name = "WorkflowExtensionInputs_" + wid.replace(".", "_").replace("-", "_")
            properties = schemas[extension_name]["properties"]
            for name in _manifest(model)["inputs"]:
                if name in ("prompt", "negative_prompt", "seed", "image", "image_strength", "frame_rate"):
                    self.assertNotIn(name, properties, (model, name))
                else:
                    self.assertIn(name, properties, (model, name))

    def test_models_are_listed_for_video_generate(self) -> None:
        by_id = {e["id"]: e for e in proxy._build_workflow_model_entries()}
        for model in KEYFRAME_MODELS:
            self.assertEqual(by_id[model]["azizam_intent"], "video.generate", model)
            self.assertIsNotNone(proxy._resolve_video_workflow(model)[1], model)


class KeyframeMcpTests(unittest.TestCase):
    """The MCP `run` tool builds the keyframe request from the real catalog, with no model-specific code."""

    def _server(self, sent: list):
        from types import SimpleNamespace

        import httpx
        from fastapi import FastAPI
        from gateway import mcp_server

        async def core_call(_request, method, path, **kwargs):
            sent.append((method, path, kwargs))
            if method == "POST":
                return httpx.Response(200, json={"id": NEW_ID, "object": "video", "status": "queued", "progress": 0})
            return httpx.Response(200, json={"id": NEW_ID, "object": "video", "status": "in_progress", "progress": 10})

        deps = mcp_server.McpDeps(
            authenticate=lambda _request, _path: SimpleNamespace(identity="k"), call=core_call,
            catalog=proxy._mcp_catalog, aliases=lambda: proxy._gateway_aliases_provider(),
            store_asset=None, read_asset=lambda *_a: None, public_base_url=lambda _r: "https://gw.test",
            poll_interval=0.0, allowed_origins=lambda _r: ["https://gw.test"])
        app = FastAPI()
        mcp_server.register_mcp_routes(app, deps)
        return TestClient(app)

    def _rpc(self, client, method: str, params: dict) -> dict:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"mcp-protocol-version": "2025-11-25"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_run_sends_first_picture_as_input_reference_and_the_rest_as_extensions(self) -> None:
        sent: list = []
        client = self._server(sent)
        arguments = {"target": "ltx-2.5-keyframes", "mode": "async", "wait_seconds": 0, "inputs": {
            "prompt": "a lighthouse", "image": _uri(PNG_FIRST), "last_frame": _uri(PNG_LAST),
            "last_frame_strength": 0.6, "image_strength": 0.8}}
        result = self._rpc(client, "tools/call", {"name": "run", "arguments": arguments})
        self.assertFalse(result.get("isError"), result)
        submits = [kwargs for method, path, kwargs in sent if method == "POST"]
        self.assertEqual(len(submits), 1)
        body = submits[0]["json"]
        request = body.get("request", body)  # the gateway wraps the request for a media job
        self.assertEqual(request["input_reference"], _uri(PNG_FIRST))
        self.assertEqual(request["image_strength"], 0.8)
        self.assertEqual(request["extensions"], {NS: {"last_frame": _uri(PNG_LAST), "last_frame_strength": 0.6}})
        self.assertEqual(request["model"], "video-ltx-2.5-keyframes")

    def test_run_refuses_a_missing_last_picture_before_calling_the_core(self) -> None:
        sent: list = []
        result = self._rpc(self._server(sent), "tools/call", {"name": "run", "arguments": {
            "target": "minimax-h3-keyframes", "inputs": {"prompt": "x", "image": _uri(PNG_FIRST)}}})
        self.assertTrue(result["isError"])
        self.assertIn("last_frame", json.dumps(result))
        self.assertEqual([call for call in sent if call[0] == "POST"], [])


if __name__ == "__main__":
    unittest.main()
