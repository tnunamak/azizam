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
