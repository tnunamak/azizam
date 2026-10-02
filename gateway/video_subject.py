"""Subject selection for character replace (SCAIL-2 + SAM 3.1).

SCAIL-2 replaces the people that SAM 3 tracks in the driving video. With the
text prompt `person` SAM 3 tracks every person it finds, background people
included, so a clip with two men and a crowd gave a reference with one identity
and four tracked people. Targeting now works in three ways:

* default: replace the largest tracked person (index 0, tracks sorted by area at
  their first frame), and warn when more than one person was tracked;
* `subject_index`: pick another tracked person by that order;
* `source_point_x` / `source_point_y`: a point (0 to 1, from the top left) on the
  person in the first frame. SAM 3 segments that point and tracks only that person.

Picking the person is not enough. The reference character must also look like it
belongs where that person is, so `add_reference_fit` rescales and places it (see
there). Measured on a two-man clip: without it the model drew a large extra
woman next to the man and left him in place; with it she replaced him.

Nothing here talks to ComfyUI. The route applies these helpers to the graph.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# Node ids in workflows/videos/video-scail-2-replace.json.
FIRST_FRAME_NODE = "70"
TRACK_NODE = "18"
COLOR_MASK_NODE = "23"
PROBE_NODE = "71"
POINT_DETECT_NODE = "72"
SAM_MODEL_NODE = "15"
REF_RAW_NODE = "2"  # LoadImage of the reference character
REF_TEXT_NODE = "17"  # SAM 3 prompt for the character in the reference image
REF_DETECT_NODE = "19"  # SAM 3 mask of the character (used by SCAIL2ColoredMask)
FIT_CANVAS_NODE = "108"  # the fitted reference canvas

_SCORES = re.compile(r"'scores':\s*\[([^\]]*)\]")


class SubjectError(ValueError):
    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


def explicit_selection(block: Mapping[str, Any]) -> bool:
    """True when the caller chose the person (an index other than the default, or a point)."""
    index = str(block.get("subject_index", "")).strip()
    return ("source_point_x" in block) or (index not in ("", "0"))


def apply_point(workflow: dict[str, Any], block: Mapping[str, Any], width: int, height: int) -> bool:
    """Rewire the graph to track only the person under a point. Return True if applied."""
    has_x, has_y = "source_point_x" in block, "source_point_y" in block
    if has_x != has_y:
        raise SubjectError("source_point_x and source_point_y go together", "source_point_x")
    if not has_x:
        return False
    for name in ("source_point_x", "source_point_y"):
        value = block[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise SubjectError(f"{name} must be a number from 0 to 1", name)
    x = int(round(float(block["source_point_x"]) * (width - 1)))
    y = int(round(float(block["source_point_y"]) * (height - 1)))
    workflow[POINT_DETECT_NODE] = {
        "class_type": "SAM3_Detect",
        "inputs": {
            "model": [SAM_MODEL_NODE, 0],
            "image": [FIRST_FRAME_NODE, 0],
            "threshold": 0.5,
            "refine_iterations": 2,
            "individual_masks": False,
            "positive_coords": json.dumps([{"x": x, "y": y}]),
        },
    }
    track = workflow[TRACK_NODE]["inputs"]
    track.pop("conditioning", None)
    track["initial_mask"] = [POINT_DETECT_NODE, 0]
    track["max_objects"] = 1
    workflow[COLOR_MASK_NODE]["inputs"]["object_indices"] = "0"
    return True


def add_reference_fit(workflow: dict[str, Any]) -> None:
    """Fit the reference character to the size and place of the person it replaces.

    SCAIL-2 reads the reference image as the character's look AND layout. The old
    graph centre-cropped the reference to the output frame, which cut the head and
    feet off a square portrait, and it left the character at whatever scale the
    image had. With other people in the clip the model then drew a large extra
    figure next to the person it should have replaced. This adds nodes that
    (1) cut the character out of the full reference with its SAM 3 mask, (2) scale it to the
    height of the selected person in the first frame, and (3) paste it at that
    person's position on a grey canvas of the output size. The fitted canvas then
    replaces the cropped reference for the CLIP vision features, the reference
    image and the reference mask.
    """
    g = workflow
    nodes: dict[str, Any] = {
        # character cut-out from the full reference image
        "90": {"class_type": "SAM3_Detect", "inputs": {
            "model": [SAM_MODEL_NODE, 0], "image": [REF_RAW_NODE, 0], "threshold": 0.5,
            "refine_iterations": 2, "individual_masks": False, "conditioning": [REF_TEXT_NODE, 0]}},
        "91": {"class_type": "MaskToImage", "inputs": {"mask": ["90", 0]}},
        "92": {"class_type": "ImageCropByMask", "inputs": {"image": [REF_RAW_NODE, 0], "mask": ["90", 0]}},
        "93": {"class_type": "ImageCropByMask", "inputs": {"image": ["91", 0], "mask": ["90", 0]}},
        # selected driving person in the first frame (same selection as node 23)
        "95": {"class_type": "SCAIL2ColoredMask", "inputs": {
            "driving_track_data": [TRACK_NODE, 0],
            "object_indices": g[COLOR_MASK_NODE]["inputs"]["object_indices"],
            "sort_by": g[COLOR_MASK_NODE]["inputs"]["sort_by"], "replacement_mode": True}},
        "96": {"class_type": "ImageFromBatch", "inputs": {"image": ["95", 0], "batch_index": 0, "length": 1}},
        "97": {"class_type": "ImageToMask", "inputs": {"image": ["96", 0], "channel": "red"}},
        "98": {"class_type": "InvertMask", "inputs": {"mask": ["97", 0]}},
        # exact bounding box of that person's mask: x, y, width, height
        "99": {"class_type": "INPAINT_MaskBoundingBox", "inputs": {"mask": ["98", 0]}},
        # scale the cut-out to the person's height, keeping its aspect
        "101": {"class_type": "ImageScale", "inputs": {
            "image": ["92", 0], "upscale_method": "lanczos", "width": 0, "height": ["99", 3], "crop": "disabled"}},
        "102": {"class_type": "ImageScale", "inputs": {
            "image": ["93", 0], "upscale_method": "lanczos", "width": 0, "height": ["99", 3], "crop": "disabled"}},
        "104": {"class_type": "ImageToMask", "inputs": {"image": ["102", 0], "channel": "red"}},
        "105": {"class_type": "GetImageSize", "inputs": {"image": ["101", 0]}},
        "106": {"class_type": "ComfyMathExpression", "inputs": {
            "expression": "a + b / 2 - c / 2", "values.a": ["99", 0], "values.b": ["99", 2],
            "values.c": ["105", 0]}},
        "107": {"class_type": "EmptyImage", "inputs": {
            "width": ["20", 0], "height": ["21", 0], "batch_size": 1, "color": 8421504}},
        FIT_CANVAS_NODE: {"class_type": "ImageCompositeMasked", "inputs": {
            "destination": ["107", 0], "source": ["101", 0], "x": ["106", 1], "y": ["99", 1],
            "resize_source": False, "mask": ["104", 0]}},
    }
    g.update(nodes)
    # Use the fitted canvas wherever the cropped reference was used.
    canvas = [FIT_CANVAS_NODE, 0]
    for node in g.values():
        for key, value in node.get("inputs", {}).items():
            if value == ["6", 0]:
                node["inputs"][key] = canvas
    g[REF_DETECT_NODE]["inputs"]["image"] = canvas


def tracked_count(history_outputs: Mapping[str, Any] | None) -> int | None:
    """Number of tracked people, read from the probe node's text (a repr of the
    SAM 3 track data that ends with one score per tracked object)."""
    node = (history_outputs or {}).get(PROBE_NODE)
    texts = node.get("text") if isinstance(node, Mapping) else None
    if not texts:
        return None
    match = _SCORES.search(str(texts[0]))
    if not match:
        return None
    return len([part for part in match.group(1).split(",") if part.strip()])


def warnings_for(count: int | None, *, explicit: bool, subject: str) -> list[str]:
    if count is None:
        return []
    if count == 0:
        return [f"No person matching '{subject}' was found in the source video; the result may show no replacement. "
                "Try source_subject 'person', or source_point_x/source_point_y."]
    if count > 1 and not explicit:
        return [f"{count} people matched '{subject}' in the source video (background people count). "
                "Only the largest one (subject_index 0) was replaced. To pick another, set "
                "extensions.com.vividfish.workflow.subject_index (0 = largest at first appearance, 1 = next, ...) or "
                "source_point_x/source_point_y (0 to 1, a point on the person in the first frame)."]
    return []
