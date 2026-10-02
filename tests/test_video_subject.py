"""Character replace: which person is replaced, and the warning when several are tracked."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import video_inputs, video_subject as vs  # noqa: E402
from test_video_remix_replace import NEW_ID, NEW_PROMPT_ID, NS, PNG, SOURCE_ID, RemixReplaceBase  # noqa: E402

TRACK_REPR = "{'packed_masks': tensor([[0, 0]], dtype=torch.uint8), 'n_frames': 81, 'scores': %s, 'orig_size': (448, 832)}"


def _graph() -> dict:
    g = json.loads((ROOT / "workflows/videos/video-scail-2-replace.json").read_text())
    g.pop("_meta")
    return g


class SubjectHelperTests(unittest.TestCase):
    def test_workflow_selects_the_largest_person_and_masks_the_reference(self) -> None:
        g = _graph()
        mask = g[vs.COLOR_MASK_NODE]["inputs"]
        self.assertEqual((mask["object_indices"], mask["sort_by"]), ("0", "area"))
        # The reference is a plain mask, so an index above 0 never empties it.
        self.assertEqual(g["19"]["class_type"], "SAM3_Detect")
        self.assertEqual(mask["ref_track_data"], ["19", 0])
        self.assertEqual(g[vs.PROBE_NODE]["class_type"], "PreviewAny")
        self.assertEqual(g[vs.PROBE_NODE]["inputs"]["source"], [vs.TRACK_NODE, 0])

    def test_point_rewires_tracking_to_one_person(self) -> None:
        g = _graph()
        self.assertTrue(vs.apply_point(g, {"source_point_x": 0.5, "source_point_y": 0.25}, 832, 448))
        coords = json.loads(g[vs.POINT_DETECT_NODE]["inputs"]["positive_coords"])
        self.assertEqual(coords, [{"x": 416, "y": 112}])
        track = g[vs.TRACK_NODE]["inputs"]
        self.assertNotIn("conditioning", track)
        self.assertEqual(track["initial_mask"], [vs.POINT_DETECT_NODE, 0])
        self.assertEqual(track["max_objects"], 1)
        self.assertEqual(g[vs.COLOR_MASK_NODE]["inputs"]["object_indices"], "0")

    def test_no_point_leaves_the_graph_alone(self) -> None:
        g = _graph()
        before = json.dumps(g, sort_keys=True)
        self.assertFalse(vs.apply_point(g, {"subject_index": "1"}, 832, 448))
        self.assertEqual(json.dumps(g, sort_keys=True), before)

    def test_bad_points(self) -> None:
        for block in ({"source_point_x": 0.5}, {"source_point_x": 2, "source_point_y": 0.5},
                      {"source_point_x": "a", "source_point_y": 0.5}, {"source_point_x": True, "source_point_y": 0.5}):
            with self.assertRaises(vs.SubjectError, msg=block):
                vs.apply_point(_graph(), block, 832, 448)

    def test_tracked_count_reads_the_scores(self) -> None:
        out = {vs.PROBE_NODE: {"text": [TRACK_REPR % "[0.97, 0.96, 0.92, 0.91]"]}}
        self.assertEqual(vs.tracked_count(out), 4)
        self.assertEqual(vs.tracked_count({vs.PROBE_NODE: {"text": [TRACK_REPR % "[]"]}}), 0)
        self.assertIsNone(vs.tracked_count({}))
        self.assertIsNone(vs.tracked_count({vs.PROBE_NODE: {"text": ["garbage"]}}))

    def test_warning_rules(self) -> None:
        self.assertEqual(vs.warnings_for(1, explicit=False, subject="person"), [])
        self.assertEqual(vs.warnings_for(None, explicit=False, subject="person"), [])
        self.assertEqual(vs.warnings_for(4, explicit=True, subject="person"), [])
        many = vs.warnings_for(4, explicit=False, subject="man in gray hoodie")
        self.assertEqual(len(many), 1)
        self.assertIn("4 people", many[0])
        self.assertIn("subject_index", many[0])
        self.assertIn("source_point_x", many[0])
        self.assertIn("No person", vs.warnings_for(0, explicit=False, subject="dog")[0])

    def test_explicit_selection(self) -> None:
        self.assertFalse(vs.explicit_selection({}))
        self.assertFalse(vs.explicit_selection({"subject_index": "0"}))
        self.assertTrue(vs.explicit_selection({"subject_index": "1"}))
        self.assertTrue(vs.explicit_selection({"source_point_x": 0.1, "source_point_y": 0.1}))


class ReplaceSubjectRouteTests(RemixReplaceBase):
    probe = video_inputs.VideoProbe(fps=24.0, duration=5.0, frames=120, width=832, height=448)

    def _create(self, **extra):
        ext = {"source_video": {"video_id": SOURCE_ID},
               "reference_image": "data:image/png;base64," + base64.b64encode(PNG).decode()}
        ext.update(extra)
        return self.client.post("/v1/videos", json={"model": "character-replace", "prompt": "a woman", "extensions": {NS: ext}})

    def test_default_replaces_the_largest_person(self) -> None:
        self.assertEqual(self._create().status_code, 200)
        self.assertEqual(self.graph[vs.COLOR_MASK_NODE]["inputs"]["object_indices"], "0")
        self.assertFalse(proxy._video_job_records[NEW_PROMPT_ID]["subject"]["explicit"])

    def test_subject_index_is_bound(self) -> None:
        self.assertEqual(self._create(subject_index="1").status_code, 200)
        self.assertEqual(self.graph[vs.COLOR_MASK_NODE]["inputs"]["object_indices"], "1")
        self.assertTrue(proxy._video_job_records[NEW_PROMPT_ID]["subject"]["explicit"])

    def test_point_uses_the_output_size(self) -> None:
        r = self._create(source_point_x=0.5, source_point_y=0.5)
        self.assertEqual(r.status_code, 200, r.text)
        coords = json.loads(self.graph[vs.POINT_DETECT_NODE]["inputs"]["positive_coords"])
        self.assertEqual(coords, [{"x": 416, "y": 224}])
        self.assertEqual(self.graph[vs.TRACK_NODE]["inputs"]["max_objects"], 1)

    def test_form_strings_are_converted(self) -> None:
        r = self.client.post("/v1/videos", data={
            "model": "character-replace", "prompt": "a woman",
            f"extensions[{NS}][source_video][video_id]": SOURCE_ID,
            f"extensions[{NS}][reference_image]": "data:image/png;base64," + base64.b64encode(PNG).decode(),
            f"extensions[{NS}][source_point_x]": "0.25", f"extensions[{NS}][source_point_y]": "0.75"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(json.loads(self.graph[vs.POINT_DETECT_NODE]["inputs"]["positive_coords"]),
                         [{"x": 208, "y": 335}])

    def test_invalid_selection_is_a_400_with_the_param(self) -> None:
        for extra, param in (({"subject_index": "left"}, "subject_index"),
                             ({"source_point_x": 0.5}, "source_point_x"),
                             ({"source_point_x": 3, "source_point_y": 0.5}, "source_point_x")):
            r = self._create(**extra)
            self.assertEqual(r.status_code, 400, (extra, r.text))
            self.assertTrue(r.json()["error"]["param"].endswith(param), r.json())
        self.assertEqual(self.comfy.submitted, [])

    def test_finished_job_carries_the_warning(self) -> None:
        self._create()
        self.comfy.history[NEW_PROMPT_ID] = {
            "status": {"completed": True, "status_str": "success", "messages": []},
            "outputs": {"60": {"videos": [{"filename": "x.mp4", "subfolder": "video", "type": "output"}]},
                        vs.PROBE_NODE: {"text": [TRACK_REPR % "[0.9, 0.9, 0.8]"]}}}
        status = self.client.get(f"/v1/videos/{NEW_ID}").json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(status["vivid_fish_warnings"]), 1)
        self.assertIn("3 people", status["vivid_fish_warnings"][0])

    def test_no_warning_when_the_person_was_chosen(self) -> None:
        self._create(subject_index="1")
        self.comfy.history[NEW_PROMPT_ID] = {
            "status": {"completed": True, "status_str": "success", "messages": []},
            "outputs": {vs.PROBE_NODE: {"text": [TRACK_REPR % "[0.9, 0.9, 0.8]"]}}}
        self.assertNotIn("vivid_fish_warnings", self.client.get(f"/v1/videos/{NEW_ID}").json())


if __name__ == "__main__":
    unittest.main()
