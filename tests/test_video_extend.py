"""Long video: planning, graph building, POST /v1/videos/extensions, `shots`,
and `seconds` past a model's native length.

The route tests drive the real FastAPI app against a fake ComfyUI and inspect
the graph the gateway submits. GPU renders are proven separately (see the
long-video report); these tests pin the contract and the arithmetic.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs, video_extend as ve, video_inputs  # noqa: E402
from test_video_remix_replace import (  # noqa: E402
    NEW_ID,
    NEW_PROMPT_ID,
    NS,
    PNG,
    SOURCE_BYTES,
    SOURCE_ID,
    RemixReplaceBase,
)

FPS = 24.0


def _spec(segments, *, source=None, ctx=3, **kw):
    return ve.ChainSpec(segments=segments, width=960, height=512, fps=FPS, seed=10,
                        context_frames_latents=ctx, source=source, **kw)


class PlanTests(unittest.TestCase):
    def test_first_segment_is_8a_plus_1(self) -> None:
        for seconds in (1, 3, 8, 8.4):
            self.assertEqual((ve.first_segment_frames(seconds, FPS) - 1) % 8, 0, seconds)
        self.assertEqual(ve.first_segment_frames(8, FPS), 193)

    def test_plain_seconds_split_into_a_first_and_added_segments(self) -> None:
        segs = ve.plan_segments([("walk", 20)], fps=FPS, has_source=False)
        self.assertEqual([s.frames for s in segs], [193, 144, 144])
        self.assertAlmostEqual(sum(s.frames for s in segs) / FPS, 20, delta=0.1)
        self.assertTrue(all(s.shot == 0 for s in segs))

    def test_every_added_segment_is_a_multiple_of_8_and_within_the_cap(self) -> None:
        for seconds in (1.0, 2.5, 8, 9, 16, 24, 31.3, 40):
            segs = ve.plan_segments([("x", seconds)], fps=FPS, has_source=False)
            self.assertEqual((segs[0].frames - 1) % 8, 0)
            for s in segs[1:]:
                self.assertEqual(s.frames % 8, 0, seconds)
                self.assertLessEqual(s.frames, 192)
            self.assertAlmostEqual(sum(s.frames for s in segs) / FPS, seconds, delta=0.4, msg=seconds)

    def test_shots_keep_their_own_prompt_and_index(self) -> None:
        segs = ve.plan_segments([("a", 5), ("b", 12), ("c", 3)], fps=FPS, has_source=False)
        self.assertEqual([s.shot for s in segs], [0, 1, 1, 2])
        self.assertEqual([s.prompt for s in segs], ["a", "b", "b", "c"])

    def test_extension_has_no_first_segment(self) -> None:
        segs = ve.plan_segments([("more", 6)], fps=FPS, has_source=True)
        self.assertEqual([s.frames for s in segs], [144])

    def test_state_ledger_is_prepended_to_every_segment(self) -> None:
        segs = ve.plan_segments([("a", 20)], fps=FPS, has_source=False, state_ledger="Red jacket, left of frame.")
        self.assertTrue(all(s.prompt.startswith("Red jacket, left of frame.\n\na") for s in segs))

    def test_empty_shots_are_an_error_with_a_code(self) -> None:
        with self.assertRaises(ve.PlanError) as ctx:
            ve.plan_segments([], fps=FPS, has_source=False)
        self.assertEqual(ctx.exception.code, "invalid_shots")


def _check_refs(test: unittest.TestCase, graph: dict) -> None:
    for node_id, node in graph.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                test.assertIn(value[0], graph, (node_id, key))


class GraphTests(unittest.TestCase):
    def test_graph_refs_resolve_and_only_ltx25_models_are_loaded(self) -> None:
        segs = ve.plan_segments([("a", 20)], fps=FPS, has_source=False)
        built = ve.build_chain_graph(_spec(segs))
        _check_refs(self, built.graph)
        files = {v for n in built.graph.values() for v in n["inputs"].values()
                 if isinstance(v, str) and v.endswith(".safetensors")}
        self.assertTrue(files and all("ltx-2.5" in f for f in files), files)
        self.assertEqual(len(built.units), 3)
        self.assertEqual(built.total_frames, sum(s.frames for s in segs))

    def test_added_segment_conditions_on_the_last_frames_not_one_frame(self) -> None:
        segs = [ve.Segment("a", 193, 0), ve.Segment("b", 96, 1)]
        g = ve.build_chain_graph(_spec(segs, ctx=3)).graph
        tail = g["s1_tail"]["inputs"]
        self.assertEqual(tail["length"], 25)  # 8*3+1 frames of real context
        self.assertEqual(tail["batch_index"], 193 - 25)
        self.assertEqual(g["s1_guide"]["inputs"]["latent_idx"], 0)
        self.assertEqual(g["s1_vlat"]["inputs"]["length"], 25 + 96)
        # the pinned context is dropped; only the new frames are kept
        self.assertEqual(g["s1_new"]["inputs"]["batch_index"], 25)
        self.assertEqual(g["s1_new"]["inputs"]["length"], 96)

    def test_each_segment_has_its_own_prompt_and_seed(self) -> None:
        segs = [ve.Segment("first", 193, 0), ve.Segment("second", 96, 1), ve.Segment("third", 96, 2)]
        g = ve.build_chain_graph(_spec(segs)).graph
        self.assertEqual([g[f"s{i}_text"]["inputs"]["text"] for i in range(3)], ["first", "second", "third"])
        self.assertEqual([g[f"s{i}_noise"]["inputs"]["noise_seed"] for i in range(3)], [10, 11, 12])

    def test_audio_length_equals_video_length(self) -> None:
        segs = [ve.Segment("a", 193, 0), ve.Segment("b", 96, 1), ve.Segment("c", 144, 2)]
        built = ve.build_chain_graph(_spec(segs))
        g = built.graph
        audio = 193 / FPS + sum(g[k]["inputs"]["duration"] for k in g if k.endswith("_atrim"))
        self.assertAlmostEqual(audio, built.total_frames / FPS, places=6)
        self.assertIn("audio", g["create"]["inputs"])

    def test_source_video_is_loaded_continued_and_counted(self) -> None:
        src = ve.SourceVideo("src.mp4", 193, FPS, 960, 512, True)
        g = ve.build_chain_graph(_spec([ve.Segment("more", 96, 0)], source=src)).graph
        self.assertEqual(g["src_file"]["inputs"]["file"], "src.mp4")
        self.assertEqual(g["s0_tail"]["inputs"]["batch_index"], 193 - 25)
        self.assertNotIn("src_scale", g)
        self.assertEqual(g["src_audio"]["inputs"]["duration"], 193 / FPS)
        _check_refs(self, g)

    def test_source_of_another_size_is_scaled_to_the_output_size(self) -> None:
        src = ve.SourceVideo("src.mp4", 193, FPS, 832, 480, True)
        g = ve.build_chain_graph(_spec([ve.Segment("more", 96, 0)], source=src)).graph
        self.assertEqual((g["src_scale"]["inputs"]["width"], g["src_scale"]["inputs"]["height"]), (960, 512))

    def test_silent_source_gives_a_silent_result(self) -> None:
        src = ve.SourceVideo("src.mp4", 193, FPS, 960, 512, False)
        g = ve.build_chain_graph(_spec([ve.Segment("more", 96, 0)], source=src)).graph
        self.assertNotIn("audio", g["create"]["inputs"])
        self.assertNotIn("src_audio", g)

    def test_first_frame_image_pins_the_first_segment_only(self) -> None:
        g = ve.build_chain_graph(_spec([ve.Segment("a", 193, 0), ve.Segment("b", 96, 1)],
                                       first_frame_image="ref.png", first_frame_strength=0.8)).graph
        self.assertEqual(g["s0_image"]["inputs"]["image"], "ref.png")
        self.assertEqual(g["s0_i2v"]["inputs"]["strength"], 0.8)
        self.assertNotIn("s1_image", g)

    def test_segment_of_node_maps_progress_nodes(self) -> None:
        built = ve.build_chain_graph(_spec([ve.Segment("a", 193, 0), ve.Segment("b", 96, 1)]))
        self.assertEqual(ve.segment_of_node(built.units, "s1_sample"), 1)
        self.assertEqual(ve.segment_of_node(built.units, "s0_decode"), 0)
        self.assertIsNone(ve.segment_of_node(built.units, "unet"))


class ProgressTests(unittest.TestCase):
    def test_progress_spans_all_segments_and_never_runs_backwards(self) -> None:
        built = ve.build_chain_graph(_spec([ve.Segment("a", 193, 0), ve.Segment("b", 96, 1)]))
        with patch.dict(proxy._video_chain_units, {"p": built.units}):
            first = proxy._video_percent("p", "s0_sample", "sampling", 1.0)
            second_start = proxy._video_percent("p", "s1_sample", "sampling", 0.0)
            second_end = proxy._video_percent("p", "s1_sample", "sampling", 1.0)
        self.assertLessEqual(first, second_start)
        self.assertLess(second_start, second_end)
        self.assertLessEqual(second_end, 92)
        # a job with no chain keeps the old single-stage mapping
        self.assertEqual(proxy._video_percent("other", "x", "sampling", 1.0), proxy._phase_percent("sampling", 1.0))


SOURCE_PROBE = video_inputs.VideoProbe(fps=24.0, duration=193 / 24, frames=193, width=960, height=512, has_audio=True)


class ExtensionRouteTests(RemixReplaceBase):
    probe = SOURCE_PROBE

    def setUp(self) -> None:
        super().setUp()
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stack.enter_context(patch.object(media_jobs, "DB_PATH", Path(self._tmp.name) / "m.db"))

    def _extend(self, **body):
        body.setdefault("video", {"id": SOURCE_ID})
        body.setdefault("prompt", "the cyclist reaches the village")
        return self.client.post("/v1/videos/extensions", json=body)

    def test_extension_returns_a_video_resource_with_lineage_and_stitched_seconds(self) -> None:
        r = self._extend(seconds=8)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], NEW_ID)
        self.assertEqual(body["object"], "video")
        self.assertEqual((body["status"], body["progress"]), ("queued", 0))
        # The OpenAPI spec has no extension field; remixed_from_video_id carries the source.
        self.assertEqual(body["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(body["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(body["azizam_resolved_model"], "video-ltx-2.5-long")
        self.assertEqual(body["model"], "ltx-2.5")  # the source's model
        self.assertEqual(body["size"], "960x512")
        # 193 source frames + 192 new frames, stitched
        self.assertAlmostEqual(body["total_seconds"], 385 / 24, places=3)
        self.assertEqual(float(body["seconds"]), body["total_seconds"])
        self.assertEqual(body["prompt"], "the cyclist reaches the village")
        self.assertEqual(self.comfy.uploads[0][1], SOURCE_BYTES)

    def test_graph_loads_the_uploaded_source_and_adds_one_segment(self) -> None:
        self._extend(seconds=8, seed=4)
        g = self.graph
        self.assertEqual(g["src_file"]["inputs"]["file"], "up1_source_00001_.mp4")
        self.assertEqual(g["s0_text"]["inputs"]["text"], "the cyclist reaches the village")
        self.assertEqual(g["s0_noise"]["inputs"]["noise_seed"], 4)
        self.assertEqual(g["s0_tail"]["inputs"]["length"], 25)
        self.assertNotIn("s1_sample", g)

    def test_long_extensions_become_several_segments_with_one_prompt(self) -> None:
        r = self._extend(seconds=16)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["segments"], 2)
        self.assertEqual(self.graph["s1_text"]["inputs"]["text"], self.graph["s0_text"]["inputs"]["text"])

    def test_context_seconds_sets_the_overlap_window(self) -> None:
        self._extend(seconds=4, context_seconds=2.0)
        self.assertEqual(self.graph["s0_tail"]["inputs"]["length"], 8 * 6 + 1)

    def test_state_ledger_goes_in_front_of_the_prompt(self) -> None:
        self._extend(seconds=4, extensions={NS: {"state_ledger": "Red jacket, riding right."}})
        self.assertTrue(self.graph["s0_text"]["inputs"]["text"].startswith("Red jacket, riding right.\n\n"))

    def test_total_length_cap_returns_a_clear_400(self) -> None:
        r = self._extend(seconds=40)  # 8 s source + 40 s > 40 s cap
        self.assertEqual(r.status_code, 400, r.text)
        err = r.json()["error"]
        self.assertEqual(err["code"], "video_total_length_exceeded")
        self.assertEqual(err["param"], "seconds")
        self.assertIn("max_total_seconds", err["message"])
        self.assertEqual(self.comfy.submitted, [])
        self.assertEqual(self.comfy.uploads, [], "nothing is uploaded for a rejected request")

    def test_total_length_cap_is_a_config_key(self) -> None:
        with patch.object(proxy, "VIDEO_MAX_TOTAL_SECONDS", 120.0):
            r = self._extend(seconds=40)
        self.assertEqual(r.status_code, 200, r.text)
        with patch.object(proxy, "VIDEO_MAX_TOTAL_SECONDS", 10.0):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 400)

    def test_default_cap_is_40_seconds(self) -> None:
        self.assertEqual(proxy.VIDEO_MAX_TOTAL_SECONDS, 40.0)

    def test_record_is_tracked_and_persisted_with_chain_info(self) -> None:
        self._extend(seconds=8)
        record = proxy._video_job_records[NEW_PROMPT_ID]
        self.assertEqual(record["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(record["chain"]["segments"], 1)
        from gateway import video_job_records
        stored = video_job_records.load_all(path=media_jobs.DB_PATH)[NEW_PROMPT_ID]
        self.assertEqual(stored["chain"]["segment_frames"], [192])
        status = self.client.get(f"/v1/videos/{NEW_ID}").json()
        self.assertEqual(status["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(status["remixed_from_video_id"], SOURCE_ID)

    def test_other_keys_video_cannot_be_extended(self) -> None:
        proxy._video_job_records[proxy._prompt_id_from_video_id(SOURCE_ID)]["owner_key_hash"] = "sha256:other"
        with patch.object(proxy, "_asset_owner_key", return_value="sha256:me"):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.comfy.submitted, [])

    def test_unknown_source_id_is_404_when_auth_is_on(self) -> None:
        with patch.object(proxy, "_asset_owner_key", return_value="sha256:me"):
            r = self._extend(video={"id": "video_unknown"}, seconds=4)
        self.assertEqual(r.status_code, 404, r.text)

    def test_validation_errors(self) -> None:
        cases = [
            ({"video": None}, "video"),
            ({"prompt": ""}, "prompt"),
            ({"seconds": "abc"}, "seconds"),
            ({"seconds": 0.2}, "seconds"),
            ({"context_seconds": 9}, "context_seconds"),
            ({"shots": [{"prompt": "x", "seconds": 2}]}, "shots"),
            ({"input_reference": "data:image/png;base64,AAAA"}, "input_reference"),
            ({"bogus": 1}, "bogus"),
            ({"extensions": {NS: {"nope": 1}}}, f"extensions.{NS}.nope"),
        ]
        for extra, param in cases:
            body = {"video": {"id": SOURCE_ID}, "prompt": "x", "seconds": 4}
            body.update(extra)
            body = {k: v for k, v in body.items() if v is not None}
            r = self.client.post("/v1/videos/extensions", json=body)
            self.assertEqual(r.status_code, 400, (extra, r.text))
            self.assertEqual(r.json()["error"]["param"], param, extra)
        self.assertEqual(self.comfy.submitted, [])

    def test_model_that_cannot_extend_is_rejected(self) -> None:
        r = self._extend(model="bernini-r", seconds=4)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_extend_not_supported")

    def test_501_when_not_configured(self) -> None:
        with patch.object(proxy, "VIDEO_EXTEND_WORKFLOW", None):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 501)
        self.assertEqual(r.json()["error"]["code"], "video_extend_not_configured")

    def test_source_shorter_than_the_context_window_is_rejected(self) -> None:
        short = video_inputs.VideoProbe(fps=24.0, duration=0.5, frames=12, width=960, height=512, has_audio=True)
        with patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: short):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_too_short")

    def test_unreadable_source_is_rejected_before_any_upload(self) -> None:
        with patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: None):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.comfy.uploads, [])

    def test_uploaded_source_works_and_has_no_lineage(self) -> None:
        r = self.client.post("/v1/videos/extensions", data={"prompt": "continue", "seconds": "4"},
                             files={"video": ("clip.mp4", b"clip", "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["remixed_from_video_id"])
        self.assertEqual(self.comfy.uploads[0][1], b"clip")

    def test_sdk_zero_byte_upload_named_by_video_id_extends_that_video(self) -> None:
        """openai-python's videos.extend treats `video` as a file part only: the {"id": ...}
        form raises client-side (extract_files rejects a dict). A zero-byte upload named
        `<video id>.mp4` is therefore how an SDK caller extends by id."""
        r = self.client.post("/v1/videos/extensions", data={"prompt": "continue", "seconds": "8"},
                             files={"video": (f"{SOURCE_ID}.mp4", b"", "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(r.json()["model"], "ltx-2.5")  # the source's model, as for the JSON form
        self.assertEqual(self.comfy.uploads[0][1], SOURCE_BYTES)  # the stored video, not the empty part

    def test_an_empty_upload_not_named_by_a_video_id_is_still_rejected(self) -> None:
        r = self.client.post("/v1/videos/extensions", data={"prompt": "continue", "seconds": "8"},
                             files={"video": ("clip.mp4", b"", "video/mp4")})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.comfy.uploads, [])

    def test_the_empty_upload_shortcut_is_not_available_on_other_video_fields(self) -> None:
        r = self.client.post("/v1/videos/edits", data={"prompt": "x"},
                             files={"video": (f"{SOURCE_ID}.mp4", b"", "video/mp4")})
        self.assertNotEqual(r.status_code, 200, r.text)

    def _sdk(self):
        import openai

        return openai.OpenAI(base_url="http://testserver/v1", api_key="test", http_client=self.client, max_retries=0)

    @unittest.skipUnless(hasattr(__import__("openai").OpenAI(api_key="x").videos, "extend"), "installed openai SDK has no videos.extend")
    def test_the_real_sdk_extends_by_id_and_parses_the_result(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            video = self._sdk().videos.extend(
                video=(f"{SOURCE_ID}.mp4", b"", "video/mp4"), prompt="continue", seconds="8")
        self.assertEqual(video.id, NEW_ID)
        self.assertEqual((video.object, video.status, video.progress), ("video", "queued", 0))
        self.assertEqual(video.remixed_from_video_id, SOURCE_ID)
        self.assertEqual(video.size, "960x512")
        self.assertEqual(video.prompt, "continue")

    @unittest.skipUnless(hasattr(__import__("openai").OpenAI(api_key="x").videos, "extend"), "installed openai SDK has no videos.extend")
    def test_the_real_sdk_extends_an_uploaded_video(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            video = self._sdk().videos.extend(video=("clip.mp4", b"clip", "video/mp4"), prompt="continue", seconds="4")
        self.assertEqual((video.id, video.status), (NEW_ID, "queued"))
        self.assertEqual(self.comfy.uploads[0][1], b"clip")

    def test_sdk_multipart_shape_with_bracketed_video_id(self) -> None:
        """A hand-written multipart client may send `video[id]`, plus gateway extras as
        bracketed fields. (openai-python cannot: it sends `video` as a file part.)"""
        r = self.client.post("/v1/videos/extensions", data={
            "prompt": "continue", "seconds": "8", "video[id]": SOURCE_ID, "context_seconds": "2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(self.graph["s0_tail"]["inputs"]["length"], 8 * 6 + 1)

    def test_openai_prefixed_alias_route_exists(self) -> None:
        r = self.client.post("/openai/v1/videos/extensions",
                             json={"video": {"id": SOURCE_ID}, "prompt": "x", "seconds": 4})
        self.assertEqual(r.status_code, 200, r.text)


class FailureInfoTests(unittest.TestCase):
    def test_failed_chain_names_the_segment_and_what_finished(self) -> None:
        units = [{"sample": f"s{i}_sample", "decode": f"s{i}_decode"} for i in range(4)]
        record = {"chain": {"segments": 4, "units": units, "fps": 24.0, "segment_frames": [193, 144, 144, 144]}}
        job = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_id": "s2_sample", "node_type": "SamplerCustomAdvanced",
                                 "exception_message": "CUDA out of memory"}]]}}
        error = proxy._chain_failure_error(record, job, "ComfyUI SamplerCustomAdvanced node s2_sample: CUDA out of memory")
        self.assertEqual(error["code"], "video_generation_failed")
        self.assertEqual((error["failed_segment"], error["segments_completed"], error["segments_total"]), (3, 2, 4))
        self.assertAlmostEqual(error["completed_seconds"], (193 + 144) / 24, places=2)
        self.assertIn("segment 3 of 4", error["message"])
        self.assertIn("no partial video", error["message"])

    def test_error_outside_a_segment_keeps_the_plain_message(self) -> None:
        record = {"chain": {"segments": 2, "units": [{"sample": "s0_sample", "decode": "s0_decode"},
                                                     {"sample": "s1_sample", "decode": "s1_decode"}], "fps": 24.0,
                            "segment_frames": [193, 96]}}
        job = {"status": {"messages": [["execution_error", {"node_id": "unet", "exception_message": "boom"}]]}}
        error = proxy._chain_failure_error(record, job, "ComfyUI node unet: boom")
        self.assertNotIn("failed_segment", error)
        self.assertEqual(error["message"], "ComfyUI node unet: boom")


class CreateLongVideoTests(RemixReplaceBase):
    probe = None

    def _create(self, **body):
        return self.client.post("/v1/videos", json=body)

    def test_shots_render_each_prompt_in_order(self) -> None:
        r = self._create(model="video-long", shots=[
            {"prompt": "wide shot of a harbor", "seconds": 5},
            {"prompt": "close on a fisherman", "seconds": 4},
            {"prompt": "the boat leaves", "seconds": 4}], seed=3)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["segments"], 3)
        g = self.graph
        self.assertEqual([g[f"s{i}_text"]["inputs"]["text"] for i in range(3)],
                         ["wide shot of a harbor", "close on a fisherman", "the boat leaves"])
        self.assertAlmostEqual(body["total_seconds"], 13, delta=0.4)
        self.assertEqual(body["prompt"], "wide shot of a harbor\nclose on a fisherman\nthe boat leaves")
        self.assertIsNone(body["extended_from_video_id"])

    def test_shots_work_on_the_default_ltx_model_too(self) -> None:
        r = self._create(model="ltx-2.5", shots=[{"prompt": "a", "seconds": 3}, {"prompt": "b", "seconds": 3}])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["azizam_resolved_model"], "video-ltx-2.5-long")
        self.assertEqual(r.json()["model"], "ltx-2.5")

    def test_plain_seconds_past_native_length_is_the_same_chain(self) -> None:
        r = self._create(model="ltx-2.5", prompt="a long take", seconds=25)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["azizam_resolved_model"], "video-ltx-2.5-long")
        self.assertAlmostEqual(r.json()["total_seconds"], 25, delta=0.4)
        self.assertEqual(r.json()["segments"], 4)

    def test_sdk_multipart_shots_are_regrouped(self) -> None:
        """openai-python sends shots as `shots[][prompt]` x N then `shots[][seconds]` x N."""
        fields = [("model", "video-long"), ("prompt", ""),
                  ("shots[][prompt]", "a"), ("shots[][prompt]", "b"),
                  ("shots[][seconds]", "5"), ("shots[][seconds]", "4")]
        r = self.client.post("/v1/videos", files=[(k, (None, v)) for k, v in fields])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([self.graph[f"s{i}_text"]["inputs"]["text"] for i in range(2)], ["a", "b"])
        self.assertEqual(r.json()["segments"], 2)

    def test_shots_as_a_json_string(self) -> None:
        r = self.client.post("/v1/videos", data={
            "model": "video-long", "prompt": "", "shots": json.dumps([{"prompt": "a", "seconds": 3}])})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/v1/videos", data={"model": "video-long", "prompt": "", "shots": "not json"})
        self.assertEqual(r.json()["error"]["code"], "invalid_shots")

    def test_native_length_still_uses_the_template_graph(self) -> None:
        r = self._create(model="ltx-2.5", prompt="short", seconds=5)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["azizam_resolved_model"], "video-ltx-2.5-t2v")
        self.assertNotIn("s0_text", self.graph)

    def test_long_names_keep_working(self) -> None:
        for name in ("video-long", "ltx-2.5-long", "ltx-2.3-long", "video-ltx-2.5-long"):
            self.comfy.submitted.clear()
            r = self._create(model=name, prompt="x", seconds=12)
            self.assertEqual(r.status_code, 200, (name, r.text))
            self.assertEqual(r.json()["model"], name)
            self.assertEqual(r.json()["azizam_resolved_model"], "video-ltx-2.5-long")

    def test_total_length_cap_applies_to_create(self) -> None:
        r = self._create(model="video-long", prompt="x", seconds=41)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_total_length_exceeded")
        r = self._create(model="video-long", shots=[{"prompt": "a", "seconds": 30}, {"prompt": "b", "seconds": 15}])
        self.assertEqual(r.json()["error"]["code"], "video_total_length_exceeded")

    def test_invalid_shots(self) -> None:
        bad = [
            ([], "shots"),
            ("nope", "shots"),
            ([{"prompt": "a"}], "shots[0].seconds"),
            ([{"prompt": "", "seconds": 3}], "shots[0].prompt"),
            ([{"prompt": "a", "seconds": 0.5}], "shots[0].seconds"),
            (["a"], "shots[0]"),
            ([{"prompt": "a", "seconds": 1}] * 13, "shots"),
        ]
        for shots, param in bad:
            r = self._create(model="video-long", shots=shots)
            self.assertEqual(r.status_code, 400, (shots, r.text))
            self.assertEqual(r.json()["error"]["param"], param)
        r = self._create(model="video-long", seconds=20, shots=[{"prompt": "a", "seconds": 3}])
        self.assertEqual(r.json()["error"]["code"], "shots_seconds_mismatch")
        self.assertEqual(self.comfy.submitted, [])

    def test_model_without_a_chain_rejects_shots_and_long_seconds(self) -> None:
        r = self._create(model="video-wan-2.2-ti2v-5b-q8", shots=[{"prompt": "a", "seconds": 3}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "long_video_not_supported")
        r = self._create(model="video-wan-2.2-ti2v-5b-q8", prompt="x", seconds=30)
        self.assertEqual(r.status_code, 400)

    def test_state_ledger_prefixes_every_segment(self) -> None:
        r = self._create(model="video-long", prompt="walk", seconds=20,
                         extensions={NS: {"state_ledger": "Woman in red coat."}})
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertTrue(all(g[f"s{i}_text"]["inputs"]["text"].startswith("Woman in red coat.\n\nwalk") for i in range(3)))

    def test_i2v_first_frame_on_a_chain(self) -> None:
        import base64
        r = self._create(model="video-long", prompt="x", seconds=12,
                         input_reference="data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.graph["s0_image"]["inputs"]["image"], "up1_" + self.comfy.uploads[0][0])

    def test_progress_units_are_registered_for_the_job(self) -> None:
        self._create(model="video-long", prompt="x", seconds=20)
        self.assertEqual(len(proxy._video_chain_units[NEW_PROMPT_ID]), 3)


if __name__ == "__main__":
    unittest.main()
