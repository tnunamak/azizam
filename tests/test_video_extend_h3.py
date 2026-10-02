"""Long video with MiniMax H3: the `h3_chain` engine.

Planning and graph building are checked on their own; the routes are driven
through the real FastAPI app against a fake ComfyUI (as in test_video_extend.py).
GPU renders are proven separately (see the H3 long-video report); these tests
pin the contract and the frame arithmetic.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs, video_extend as ve, video_extend_h3 as h3, video_inputs  # noqa: E402
from test_video_remix_replace import (  # noqa: E402
    NEW_ID,
    NEW_PROMPT_ID,
    NS,
    PNG,
    SOURCE_BYTES,
    SOURCE_ID,
    SOURCE_PROMPT_ID,
    RemixReplaceBase,
)

FPS = 24.0
H3_MODELS = {h3.UNET, h3.TURBO_LORA, h3.VIDEO_VAE, h3.AUDIO_VAE, h3.TEXT_ENCODER}


def _spec(segments, *, source=None, context=22, **kw):
    return ve.ChainSpec(segments=segments, width=960, height=544, fps=FPS, seed=10,
                        context_frames=context, source=source, **kw)


def _plan(shots, *, has_source=False, max_seconds=10.0, ledger=None):
    return h3.plan_segments(shots, fps=FPS, has_source=has_source, max_segment_seconds=max_seconds,
                            state_ledger=ledger)


def _sampled_length(graph: dict, j: int) -> int:
    return graph[f"s{j}_cond"]["inputs"]["length"]


class ContextWindowTests(unittest.TestCase):
    def test_window_is_a_whole_number_of_latent_steps(self) -> None:
        for seconds, frames in ((0.34, 5), (0.5, 5), (1.0, 22), (1.5, 22), (2.0, 39), (3.0, 56), (4.0, 56)):
            self.assertEqual(h3.context_window(seconds), frames, seconds)
        for frames in h3.CONTEXT_WINDOWS:
            self.assertEqual((frames - 5) % 17, 0)

    def test_engine_reports_the_window_the_route_checks_the_source_against(self) -> None:
        engine = ve.engine_for("h3_chain")
        self.assertIsInstance(engine, h3.H3ChainEngine)
        self.assertEqual(engine.tail_frames(1.0, FPS), 22)
        self.assertEqual(ve.engine_for("ltx25_chain").tail_frames(1.0, FPS), 25)


class ConfigTests(unittest.TestCase):
    def test_h3_long_workflow_is_built_in_code_and_h3_models_lead_to_it(self) -> None:
        workflows = proxy.config["videos"]["workflows"]
        self.assertEqual(workflows["video-minimax-h3-long"]["builder"], "h3_chain")
        for name in ("video-minimax-h3-t2v", "video-minimax-h3-i2v"):
            self.assertEqual(workflows[name]["chain_workflow"], "video-minimax-h3-long")
        for alias in ("minimax-h3-long", "h3-long"):
            self.assertEqual(proxy._resolve_video_workflow(alias)[0], "video-minimax-h3-long")
        for alias in ("minimax-h3", "h3", "minimax-h3-i2v"):
            self.assertEqual(proxy._chain_workflow_for(alias)[0], "video-minimax-h3-long")

    def test_manifest_reference_graph_matches_the_builder(self) -> None:
        """workflows/videos/video-minimax-h3-long.json is the one-segment graph the
        manifest binds. It must not drift from gateway/video_extend_h3.py."""
        import json
        stored = json.loads((ROOT / "workflows/videos/video-minimax-h3-long.json").read_text())
        stored.pop("_meta")
        spec = ve.ChainSpec(
            [ve.Segment("A cinematic shot of a serene scene with natural motion.", 243, 0)],
            960, 544, 24.0, 42, context_frames=22)
        self.assertEqual(stored, h3.build_chain_graph(spec).graph)

    def test_segment_length_cap_is_a_workflow_key(self) -> None:
        self.assertEqual(proxy.config["videos"]["workflows"]["video-minimax-h3-long"]["chain_segment_seconds"], 10)


class PlanTests(unittest.TestCase):
    def test_first_segment_is_17a_plus_5(self) -> None:
        for seconds in (2, 5, 8, 10):
            frames = h3.first_segment_frames(seconds)
            self.assertEqual((frames - 5) % 17, 0, seconds)
            self.assertAlmostEqual(frames / FPS, seconds, delta=0.45)
        self.assertEqual(h3.first_segment_frames(5), 124)

    def test_added_segments_grow_in_steps_of_17_within_the_cap(self) -> None:
        segs = _plan([("a", 10), ("b", 25)])
        self.assertEqual(segs[0].frames, h3.first_segment_frames(10))
        for seg in segs[1:]:
            self.assertEqual(seg.frames % 17, 0, seg)
            self.assertLessEqual(seg.frames, 10 * FPS)
        self.assertAlmostEqual(sum(s.frames for s in segs) / FPS, 35, delta=0.9)

    def test_a_long_shot_splits_into_near_equal_segments_with_one_prompt(self) -> None:
        segs = _plan([("walk", 25)], max_seconds=10)
        self.assertEqual([s.shot for s in segs], [0, 0, 0])
        self.assertEqual({s.prompt for s in segs}, {"walk"})
        added = [s.frames for s in segs[1:]]
        self.assertLessEqual(max(added) - min(added), 17)

    def test_extension_has_no_first_segment(self) -> None:
        segs = _plan([("more", 10)], has_source=True)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].frames % 17, 0)

    def test_state_ledger_is_prepended_and_empty_shots_are_an_error(self) -> None:
        segs = _plan([("a", 4), ("b", 4)], ledger="Red coat.")
        self.assertTrue(all(s.prompt.startswith("Red coat.\n\n") for s in segs))
        with self.assertRaises(ve.PlanError) as ctx:
            _plan([])
        self.assertEqual(ctx.exception.code, "invalid_shots")

    def test_source_of_another_frame_rate_is_refused(self) -> None:
        engine = ve.engine_for("h3_chain")
        engine.check_source(24.0)
        engine.check_source(23.976)
        with self.assertRaises(ve.PlanError) as ctx:
            engine.check_source(30.0)
        self.assertEqual(ctx.exception.code, "video_fps_unsupported")
        ve.engine_for("ltx25_chain").check_source(30.0)  # LTX follows the source rate


def _refs_resolve(test: unittest.TestCase, graph: dict) -> None:
    for node_id, node in graph.items():
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                test.assertIn(value[0], graph, f"{node_id}.{name}")


class GraphTests(unittest.TestCase):
    def _chain(self, shots, **kw):
        return h3.build_chain_graph(_spec(_plan(shots), **kw))

    def test_refs_resolve_and_only_h3_models_are_loaded(self) -> None:
        built = self._chain([("a", 10), ("b", 6)])
        _refs_resolve(self, built.graph)
        loaded = {n["inputs"].get("unet_name") or n["inputs"].get("vae_name") or n["inputs"].get("clip_name")
                  or n["inputs"].get("lora_name") for n in built.graph.values()
                  if n["class_type"] in ("UNETLoader", "VAELoader", "CLIPLoader", "LoraLoaderModelOnly")}
        self.assertEqual(loaded, H3_MODELS)
        self.assertEqual(built.graph["clip"]["inputs"]["type"], "minimax")

    def test_every_sampled_length_is_on_the_17k_plus_5_grid(self) -> None:
        built = self._chain([("a", 10), ("b", 10), ("c", 4)])
        for j in range(len(built.units)):
            self.assertEqual((_sampled_length(built.graph, j) - 5) % 17, 0, j)

    def test_first_segment_has_no_context_and_trims_nothing(self) -> None:
        g = self._chain([("a", 5), ("b", 5)]).graph
        self.assertNotIn("s0_ctx", g)
        self.assertEqual(g["s0_trim"]["inputs"]["trim_frames"], 0)
        self.assertEqual(g["s0_guider"]["inputs"]["conditioning"], ["s0_cond", 0])

    def test_added_segment_pins_the_previous_latent_not_decoded_frames(self) -> None:
        g = self._chain([("a", 5), ("b", 5)]).graph
        ctx = g["s1_ctx"]["inputs"]
        self.assertEqual(ctx["context_latent"], ["s0_sample", 0])
        self.assertNotIn("context_frames", ctx)
        self.assertNotIn("context_audio", ctx)
        self.assertEqual(ctx["context_length"], "22")
        self.assertEqual(ctx["audio_context_length"], 24)
        self.assertEqual(ctx["conditioning"], ["s1_cond", 0])
        self.assertEqual(ctx["latent"], ["s1_cond", 1])
        # sampled from the context-pinned conditioning and the segment's own empty latent
        self.assertEqual(g["s1_guider"]["inputs"]["conditioning"], ["s1_ctx", 0])
        self.assertEqual(g["s1_sample"]["inputs"]["latent_image"], ["s1_cond", 1])
        # the pinned head comes off picture and sound together
        self.assertEqual(g["s1_trim"]["inputs"]["trim_frames"], ["s1_ctx", 1])
        self.assertIs(g["s1_trim"]["inputs"]["match_tail"], True)

    def test_sampled_length_is_context_plus_new_frames(self) -> None:
        segs = _plan([("a", 5), ("b", 5)])
        g = h3.build_chain_graph(_spec(segs)).graph
        self.assertEqual(_sampled_length(g, 0), segs[0].frames)
        self.assertEqual(_sampled_length(g, 1), 22 + segs[1].frames)

    def test_each_segment_has_its_own_prompt_and_seed(self) -> None:
        g = self._chain([("harbor", 4), ("fisherman", 4), ("boat", 4)]).graph
        self.assertEqual([g[f"s{i}_cond"]["inputs"]["prompt"] for i in range(3)], ["harbor", "fisherman", "boat"])
        self.assertEqual([g[f"s{i}_noise"]["inputs"]["noise_seed"] for i in range(3)], [10, 11, 12])

    def test_total_frames_and_audio_match(self) -> None:
        segs = _plan([("a", 5), ("b", 5), ("c", 5)])
        built = h3.build_chain_graph(_spec(segs))
        self.assertEqual(built.total_frames, sum(s.frames for s in segs))
        g = built.graph
        # one audio part per segment, joined in order; each part is cut to its frames by the Trim node
        self.assertEqual(g["audio_cat0"]["inputs"]["audio1"], ["s0_trim", 1])
        self.assertEqual(g["audio_cat0"]["inputs"]["audio2"], ["s1_trim", 1])
        self.assertEqual(g["audio_cat1"]["inputs"]["audio2"], ["s2_trim", 1])
        self.assertEqual(g["create"]["inputs"]["audio"], ["audio_cat1", 0])
        self.assertEqual(g["create"]["inputs"]["fps"], 24.0)
        self.assertEqual(g["s1_cat"]["inputs"]["image1"], ["s0_trim", 0])

    def test_context_length_follows_the_context_window(self) -> None:
        g = h3.build_chain_graph(_spec(_plan([("a", 5), ("b", 5)]), context=39)).graph
        self.assertEqual(g["s1_ctx"]["inputs"]["context_length"], "39")
        self.assertEqual(_sampled_length(g, 1), 39 + _plan([("a", 5), ("b", 5)])[1].frames)
        with self.assertRaises(ve.PlanError):
            h3.build_chain_graph(_spec(_plan([("a", 5), ("b", 5)]), context=25))

    def test_first_frame_image_pins_the_first_segment_only(self) -> None:
        g = self._chain([("a", 10), ("b", 5)], first_frame_image="x.png").graph
        self.assertEqual(g["s0_image"]["inputs"]["image"], "x.png")
        self.assertEqual(g["s0_cond"]["inputs"]["first_frame"], ["s0_fit", 0])
        self.assertEqual(g["s0_fit"]["inputs"]["width"], 960)
        self.assertNotIn("first_frame", g["s1_cond"]["inputs"])

    def test_segment_of_node_maps_progress_nodes(self) -> None:
        built = self._chain([("a", 5), ("b", 5), ("c", 5)])
        self.assertEqual(ve.segment_of_node(built.units, "s1_sample"), 1)
        self.assertEqual(ve.segment_of_node(built.units, "s2_ctx"), 2)
        self.assertEqual(ve.segment_of_node(built.units, "s2_trim"), 2)
        self.assertIsNone(ve.segment_of_node(built.units, "unet"))


class ProgressOrderTests(unittest.IsolatedAsyncioTestCase):
    """ComfyUI runs a later segment's text encode before an earlier segment's
    sampler. Progress and the finished-segment count must not jump for it."""

    async def test_a_later_segments_encode_does_not_move_progress(self) -> None:
        built = h3.build_chain_graph(_spec(_plan([("a", 5), ("b", 5)])))
        self.assertIsNone(ve.segment_of_node(built.units, "s1_cond", progress_only=True))
        self.assertEqual(ve.segment_of_node(built.units, "s1_cond"), 1)  # errors still name the segment
        pid = "pppppppp-0000-0000-0000-000000000001"
        graph = built.graph
        with patch.dict(proxy._video_chain_units, {pid: built.units}), \
                patch.dict(proxy._video_workflows, {pid: graph}), \
                patch.dict(proxy._video_job_records, {pid: {"chain": {"segments": 2, "units": built.units}}}):
            proxy._video_progress.pop(pid, None)
            await proxy._record_event({"type": "executing", "data": {"prompt_id": pid, "node": "s0_cond"}})
            await proxy._record_event({"type": "executing", "data": {"prompt_id": pid, "node": "s1_cond"}})
            entry = proxy._video_progress[pid]
            self.assertLessEqual(entry["percent"], 5)
            await proxy._record_event({"type": "progress", "data": {"prompt_id": pid, "node": "s0_sample", "value": 4, "max": 8}})
            half = proxy._video_progress[pid]["percent"]
            self.assertGreater(half, 5)
            self.assertLess(half, 48)
            payload = proxy._openai_video_payload(video_id="video_" + pid, prompt_id=pid, status="running", progress=half)
            self.assertEqual(payload["segments_completed"], 0)
            await proxy._record_event({"type": "progress", "data": {"prompt_id": pid, "node": "s1_sample", "value": 1, "max": 8}})
            payload = proxy._openai_video_payload(video_id="video_" + pid, prompt_id=pid, status="running", progress=50)
            self.assertEqual(payload["segments_completed"], 1)
            proxy._video_progress.pop(pid, None)


class ExtensionGraphTests(unittest.TestCase):
    SOURCE = ve.SourceVideo("s.mp4", 244, FPS, 960, 544, True)

    def _graph(self, shots, source=SOURCE):
        return h3.build_chain_graph(_spec(_plan(shots, has_source=True), source=source))

    def test_first_added_segment_pins_decoded_tail_and_audio_of_the_source(self) -> None:
        built = self._graph([("more", 10)])
        g = built.graph
        self.assertEqual(g["src_file"]["inputs"]["file"], "s.mp4")
        self.assertEqual(g["s0_tail"]["inputs"]["length"], 22)
        self.assertEqual(g["s0_tail"]["inputs"]["batch_index"], 244 - 22)
        ctx = g["s0_ctx"]["inputs"]
        self.assertEqual(ctx["context_frames"], ["s0_tail", 0])
        self.assertEqual(ctx["context_audio"], ["src_audio", 0])
        self.assertEqual(ctx["audio_vae"], ["avae", 0])
        self.assertNotIn("context_latent", ctx)
        self.assertEqual(g["src_audio"]["inputs"]["duration"], 244 / FPS)
        self.assertEqual(g["audio_cat0"]["inputs"]["audio1"], ["src_audio", 0])
        self.assertEqual(g["s0_cat"]["inputs"]["image1"], ["src_parts", 0])
        self.assertEqual(_sampled_length(g, 0), 22 + _plan([("more", 10)], has_source=True)[0].frames)
        self.assertEqual(built.total_frames, 244 + _plan([("more", 10)], has_source=True)[0].frames)

    def test_later_segments_of_an_extension_use_the_lossless_latent_path(self) -> None:
        g = self._graph([("more", 10), ("again", 10)]).graph
        self.assertEqual(g["s1_ctx"]["inputs"]["context_latent"], ["s0_sample", 0])
        self.assertNotIn("s1_tail", g)

    def test_source_of_another_size_is_scaled_to_the_output_size(self) -> None:
        source = ve.SourceVideo("s.mp4", 244, FPS, 832, 480, True)
        g = self._graph([("more", 5)], source=source).graph
        self.assertEqual((g["src_scale"]["inputs"]["width"], g["src_scale"]["inputs"]["height"]), (960, 544))
        self.assertEqual(g["s0_tail"]["inputs"]["image"], ["src_scale", 0])
        self.assertEqual(g["s0_cat"]["inputs"]["image1"], ["src_scale", 0])

    def test_silent_source_gives_a_silent_result(self) -> None:
        source = ve.SourceVideo("s.mp4", 244, FPS, 960, 544, False)
        g = self._graph([("more", 5), ("again", 5)], source=source).graph
        self.assertNotIn("audio", g["create"]["inputs"])
        self.assertFalse([n for n in g if n.startswith("audio_cat") or n.endswith("_adecode") or n == "src_audio"])
        self.assertNotIn("context_audio", g["s0_ctx"]["inputs"])
        self.assertNotIn("audio", g["s0_trim"]["inputs"])


class H3RouteBase(RemixReplaceBase):
    """Fake ComfyUI plus a source record that was made by H3."""

    probe = video_inputs.VideoProbe(fps=24.0, duration=244 / 24, frames=244, width=960, height=544, has_audio=True)

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stack.enter_context(patch.object(media_jobs, "DB_PATH", Path(self._tmp.name) / "m.db"))
        proxy._video_job_records[SOURCE_PROMPT_ID].update(
            {"model": "minimax-h3", "resolved_model": "video-minimax-h3-t2v", "size": "960x544"})

    def _extend(self, **body):
        body.setdefault("video", {"id": SOURCE_ID})
        body.setdefault("prompt", "the dog reaches the water")
        return self.client.post("/v1/videos/extensions", json=body)

    def _create(self, **body):
        return self.client.post("/v1/videos", json=body)


class ExtensionRouteTests(H3RouteBase):
    def test_an_h3_video_is_extended_by_h3_without_naming_a_model(self) -> None:
        r = self._extend(seconds=8, seed=4)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], NEW_ID)
        self.assertEqual(body["object"], "video")
        self.assertEqual((body["status"], body["progress"]), ("queued", 0))
        self.assertEqual(body["vivid_fish_resolved_model"], "video-minimax-h3-long")
        self.assertEqual(body["model"], "minimax-h3")
        self.assertEqual(body["remixed_from_video_id"], SOURCE_ID)
        self.assertEqual(body["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(body["size"], "960x544")
        # 244 source frames + 8 s on the 17-frame grid (11 blocks = 187 frames), stitched
        self.assertAlmostEqual(body["total_seconds"], (244 + 187) / 24, places=3)
        self.assertEqual(float(body["seconds"]), body["total_seconds"])
        g = self.graph
        self.assertEqual(g["src_file"]["inputs"]["file"], "up1_source_00001_.mp4")
        self.assertEqual(g["s0_noise"]["inputs"]["noise_seed"], 4)
        self.assertEqual(g["s0_tail"]["inputs"]["length"], 22)
        self.assertEqual(self.comfy.uploads[0][1], SOURCE_BYTES)

    def test_a_ltx_video_is_still_extended_by_ltx(self) -> None:
        proxy._video_job_records[SOURCE_PROMPT_ID].update({"model": "ltx-2.5", "size": "960x512"})
        r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-ltx-2.5-long")

    def test_a_model_without_a_chain_falls_back_to_the_default_extender(self) -> None:
        proxy._video_job_records[SOURCE_PROMPT_ID].update({"model": "video-remix"})
        r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-ltx-2.5-long")

    def test_model_can_be_named_and_aliases_work(self) -> None:
        proxy._video_job_records[SOURCE_PROMPT_ID].update({"model": "ltx-2.5"})
        for alias in ("minimax-h3", "h3", "h3-long", "minimax-h3-long", "video-minimax-h3-long"):
            self.comfy.submitted.clear()
            r = self._extend(model=alias, seconds=4)
            self.assertEqual(r.status_code, 200, (alias, r.text))
            self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-minimax-h3-long", alias)
            self.assertEqual(r.json()["model"], alias)

    def test_context_seconds_sets_the_window_in_whole_latent_steps(self) -> None:
        self._extend(seconds=4, context_seconds=2.0)
        self.assertEqual(self.graph["s0_tail"]["inputs"]["length"], 39)
        self.assertEqual(self.graph["s0_ctx"]["inputs"]["context_length"], "39")

    def test_long_extension_becomes_several_segments_with_one_prompt(self) -> None:
        r = self._extend(seconds=22)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["segments"], 3)
        self.assertEqual(self.graph["s2_cond"]["inputs"]["prompt"], self.graph["s0_cond"]["inputs"]["prompt"])
        self.assertIn("context_latent", self.graph["s2_ctx"]["inputs"])

    def test_total_length_cap_returns_the_same_clear_400(self) -> None:
        r = self._extend(seconds=40)  # 10 s source + 40 s > 40 s cap
        self.assertEqual(r.status_code, 400, r.text)
        err = r.json()["error"]
        self.assertEqual((err["code"], err["param"]), ("video_total_length_exceeded", "seconds"))
        self.assertIn("max_total_seconds", err["message"])
        self.assertEqual((self.comfy.submitted, self.comfy.uploads), ([], []))

    def test_source_shorter_than_the_context_window_is_rejected(self) -> None:
        short = video_inputs.VideoProbe(fps=24.0, duration=0.5, frames=12, width=960, height=544, has_audio=True)
        with patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: short):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_too_short")
        self.assertEqual(self.comfy.uploads, [])

    def test_source_that_is_not_24_fps_is_rejected_before_any_upload(self) -> None:
        odd = video_inputs.VideoProbe(fps=30.0, duration=8.0, frames=240, width=960, height=544, has_audio=True)
        with patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: odd):
            r = self._extend(seconds=4)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_fps_unsupported")
        self.assertEqual((self.comfy.submitted, self.comfy.uploads), ([], []))

    def test_options_depend_on_the_engine(self) -> None:
        r = self._extend(seconds=4, extensions={NS: {"context_strength": 0.5}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")
        self.assertIn("state_ledger", r.json()["error"]["message"])
        self.assertEqual(self.comfy.submitted, [])
        proxy._video_job_records[SOURCE_PROMPT_ID].update({"model": "ltx-2.5"})
        self.assertEqual(self._extend(seconds=4, model="ltx-2.5", extensions={NS: {"context_strength": 0.5}}).status_code, 200)
        r = self._extend(seconds=4, model="h3", extensions={NS: {"state_ledger": "Brown dog."}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.comfy.submitted[-1]["s0_cond"]["inputs"]["prompt"].startswith("Brown dog.\n\n"))

    def test_record_is_tracked_and_persisted_with_chain_info(self) -> None:
        self._extend(seconds=8)
        record = proxy._video_job_records[NEW_PROMPT_ID]
        self.assertEqual(record["extended_from_video_id"], SOURCE_ID)
        self.assertEqual(record["chain"]["segments"], 1)
        self.assertEqual(record["chain"]["segment_frames"], [187])
        self.assertEqual(record["chain"]["source_frames"], 244)
        from gateway import video_job_records
        self.assertEqual(video_job_records.load_all(path=media_jobs.DB_PATH)[NEW_PROMPT_ID]["chain"]["segments"], 1)

    def test_duration_adjustment_reason_names_the_h3_grid(self) -> None:
        r = self._extend(seconds=8)  # 192 frames asked for, 187 on the grid
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("reason=h3_17np5_frame_grid", r.headers["X-Duration-Adjusted"])


class CreateLongVideoTests(H3RouteBase):
    probe = None

    def test_shots_render_each_prompt_in_order_on_h3(self) -> None:
        for model in ("minimax-h3", "h3", "h3-long", "minimax-h3-long"):
            self.comfy.submitted.clear()
            r = self._create(model=model, seed=3, shots=[
                {"prompt": "wide shot of a harbor", "seconds": 5},
                {"prompt": "close on a fisherman", "seconds": 4},
                {"prompt": "the boat leaves", "seconds": 4}])
            self.assertEqual(r.status_code, 200, (model, r.text))
            body = r.json()
            self.assertEqual(body["vivid_fish_resolved_model"], "video-minimax-h3-long", model)
            self.assertEqual(body["model"], model)
            self.assertEqual(body["segments"], 3)
            self.assertAlmostEqual(body["total_seconds"], 13, delta=0.9)
            self.assertEqual(body["prompt"], "wide shot of a harbor\nclose on a fisherman\nthe boat leaves")
            self.assertEqual(body["size"], "960x544")
            g = self.graph
            self.assertEqual([g[f"s{i}_cond"]["inputs"]["prompt"] for i in range(3)],
                             ["wide shot of a harbor", "close on a fisherman", "the boat leaves"])
            self.assertEqual(g["s0_noise"]["inputs"]["noise_seed"], 3)

    def test_plain_seconds_past_the_native_length_is_the_same_chain(self) -> None:
        r = self._create(model="minimax-h3", prompt="a long take", seconds=25)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-minimax-h3-long")
        self.assertAlmostEqual(r.json()["total_seconds"], 25, delta=0.9)
        self.assertEqual(r.json()["segments"], 3)  # 10 s first segment, then two added ones

    def test_native_length_still_uses_the_template_graph(self) -> None:
        r = self._create(model="minimax-h3", prompt="a dog", seconds=5)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-minimax-h3-t2v")
        self.assertEqual(self.graph["9"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertNotIn("s0_cond", self.graph)

    def test_i2v_first_frame_on_a_chain(self) -> None:
        r = self._create(model="minimax-h3", prompt="x", seconds=14,
                         input_reference="data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["vivid_fish_resolved_model"], "video-minimax-h3-long")
        self.assertEqual(self.graph["s0_image"]["inputs"]["image"], "up1_" + self.comfy.uploads[0][0])

    def test_state_ledger_prefixes_every_segment(self) -> None:
        r = self._create(model="h3-long", prompt="walk", seconds=22,
                         extensions={NS: {"state_ledger": "Woman in red coat."}})
        self.assertEqual(r.status_code, 200, r.text)
        g = self.graph
        self.assertTrue(all(g[f"s{i}_cond"]["inputs"]["prompt"].startswith("Woman in red coat.\n\nwalk") for i in range(3)))

    def test_total_length_cap_applies(self) -> None:
        r = self._create(model="h3-long", prompt="x", seconds=48)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "video_total_length_exceeded")
        self.assertEqual(self.comfy.submitted, [])

    def test_invalid_shots_use_the_h3_minimum_shot_length(self) -> None:
        r = self._create(model="h3-long", shots=[{"prompt": "a", "seconds": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "shots[0].seconds")
        r = self._create(model="h3-long", shots=[{"prompt": "a", "seconds": 2}, {"prompt": "b", "seconds": 2}])
        self.assertEqual(r.status_code, 200, r.text)
        r = self._create(model="ltx-2.5", shots=[{"prompt": "a", "seconds": 1}, {"prompt": "b", "seconds": 1}])
        self.assertEqual(r.status_code, 200, r.text)

    def test_size_follows_the_h3_limits(self) -> None:
        r = self._create(model="h3-long", prompt="x", seconds=12, size="1920x1080")
        self.assertEqual(r.json()["size"], "1344x768")
        g = self.graph
        self.assertEqual((g["s0_cond"]["inputs"]["width"], g["s0_cond"]["inputs"]["height"]), (1344, 768))

    def test_ltx_options_are_refused_on_h3_and_h3_ignores_negative_prompt(self) -> None:
        r = self._create(model="h3-long", prompt="x", seconds=12, extensions={NS: {"context_strength": 0.9}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_workflow_extension")
        r = self._create(model="h3-long", prompt="x", seconds=12, negative_prompt="blurry")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse([n for n in self.graph.values() if n["class_type"] == "CLIPTextEncode"])

    def test_progress_units_are_registered_and_progress_spans_all_segments(self) -> None:
        self._create(model="h3-long", prompt="x", seconds=22)
        units = proxy._video_chain_units[NEW_PROMPT_ID]
        self.assertEqual(len(units), 3)
        with patch.dict(proxy._video_chain_units, {"p": units}):
            first = proxy._video_percent("p", "s0_sample", "sampling", 1.0)
            second_start = proxy._video_percent("p", "s1_sample", "sampling", 0.0)
            last = proxy._video_percent("p", "s2_decode", "decoding", 1.0)
        self.assertLessEqual(first, second_start)
        self.assertLessEqual(last, 92)

    def test_failure_names_the_segment_and_what_finished(self) -> None:
        self._create(model="h3-long", prompt="x", seconds=22)
        record = proxy._video_job_records[NEW_PROMPT_ID]
        job = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_id": "s1_ctx", "node_type": "MiniMaxH3MotionContext",
                                 "exception_message": "h3_motion_context: layout changed"}]]}}
        error = proxy._chain_failure_error(record, job, "ComfyUI node s1_ctx: layout changed")
        self.assertEqual(error["code"], "video_generation_failed")
        self.assertEqual((error["failed_segment"], error["segments_completed"], error["segments_total"]), (2, 1, 3))
        self.assertIn("no partial video", error["message"])


if __name__ == "__main__":
    unittest.main()
