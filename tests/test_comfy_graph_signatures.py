"""Check the graphs the gateway builds in code against ComfyUI node signatures.

A graph that ComfyUI rejects only shows up after a render is queued, and the GPU
is shared. This test catches the common mistakes offline: a missing required
input, an unknown input, a link from the wrong output type, a literal of the
wrong type, a combo value that does not exist.

`fixtures/comfy_node_signatures.json` is a snapshot of `/object_info` for the
node classes these graphs use (ComfyUI with the LTX, KJ, SAM 3, SCAIL and H3 nodes
installed, 2026-10-02; the two H3 Motion Context entries were read from the
pinned pack's own `INPUT_TYPES`, because the pack is not loaded until ComfyUI restarts). Regenerate it when a node changes.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import video_extend as ve, video_extend_h3 as h3, video_subject as vs  # noqa: E402

SIGNATURES = json.loads((ROOT / "tests/fixtures/comfy_node_signatures.json").read_text())
# Combos that list files on the host: the value is checked at run time, not here.
HOST_FILE_INPUTS = {"unet_name", "vae_name", "clip_name", "ckpt_name", "lora_name", "file", "image", "model_name"}
PY_TYPES = {"INT": (int,), "FLOAT": (int, float), "STRING": (str,), "BOOLEAN": (bool,)}


def _type_matches(output_type: str, input_type: str) -> bool:
    if "*" in (output_type, input_type) or output_type.startswith("COMFY_") or input_type.startswith("COMFY_"):
        return True  # wildcard and "match type" generics resolve at run time
    return bool(set(output_type.split(",")) & set(input_type.split(",")))


def check_graph(graph: dict) -> list[str]:
    problems: list[str] = []
    for node_id, node in graph.items():
        sig = SIGNATURES.get(node["class_type"])
        if sig is None:
            problems.append(f"{node_id}: class {node['class_type']} not in the signature snapshot")
            continue
        required, optional = sig["input"]["required"], sig["input"]["optional"]
        known = {**required, **optional}
        for name in required:
            if name not in node["inputs"] and not any(k.startswith(name + ".") for k in node["inputs"]):
                problems.append(f"{node_id} {node['class_type']}: missing required input {name}")
        for name, value in node["inputs"].items():
            spec = known.get(name)
            if spec is None and "." in name and name.split(".")[0] in known:
                continue  # dynamic (autogrow / combo-nested) input
            if spec is None:
                problems.append(f"{node_id} {node['class_type']}: unknown input {name}")
                continue
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                src_id, index = value
                if src_id not in graph:
                    problems.append(f"{node_id}.{name}: links to missing node {src_id}")
                    continue
                outputs = SIGNATURES.get(graph[src_id]["class_type"], {}).get("output", [])
                if index >= len(outputs):
                    problems.append(f"{node_id}.{name}: node {src_id} has no output {index}")
                elif isinstance(spec, str) and not _type_matches(outputs[index], spec):
                    problems.append(f"{node_id}.{name}: {outputs[index]} output linked to {spec} input")
                continue
            if isinstance(spec, list):
                if name in HOST_FILE_INPUTS:
                    continue
                if value not in spec:
                    problems.append(f"{node_id}.{name}: {value!r} is not one of {spec}")
            elif "DYNAMICCOMBO" in spec or spec == "COMBO":
                continue  # options depend on the host or on another input
            elif all(t in PY_TYPES for t in spec.split(",")):
                allowed = tuple(p for t in spec.split(",") for p in PY_TYPES[t])
                if isinstance(value, bool) != ("BOOLEAN" in spec.split(",")) or not isinstance(value, allowed):
                    problems.append(f"{node_id}.{name}: {value!r} is not {spec}")
            else:
                problems.append(f"{node_id}.{name}: literal {value!r} given for a {spec} input")
    return problems


class ChainGraphSignatureTests(unittest.TestCase):
    def _specs(self):
        source = ve.SourceVideo("s.mp4", 193, 24.0, 832, 480, True)
        silent = ve.SourceVideo("s.mp4", 193, 24.0, 960, 512, False)
        plan = lambda shots, has_source: ve.plan_segments(shots, fps=24.0, has_source=has_source)  # noqa: E731
        yield "text to video, 3 segments", ve.ChainSpec(plan([("a", 20)], False), 960, 512, 24.0, 1)
        yield "first frame image", ve.ChainSpec(plan([("a", 12)], False), 960, 512, 24.0, 1, first_frame_image="x.png")
        yield "extend, scaled source", ve.ChainSpec(plan([("b", 12)], True), 960, 512, 24.0, 1, source=source)
        yield "extend, silent source", ve.ChainSpec(plan([("b", 4)], True), 960, 512, 24.0, 1, source=silent)
        yield "storyboard", ve.ChainSpec(plan([("a", 5), ("b", 9), ("c", 3)], False), 960, 512, 24.0, 1)

    def test_every_chain_graph_passes_the_signature_check(self) -> None:
        for label, spec in self._specs():
            with self.subTest(label):
                self.assertEqual(check_graph(ve.build_chain_graph(spec).graph), [])

    def test_manifest_reference_graph_passes(self) -> None:
        graph = json.loads((ROOT / "workflows/videos/video-ltx-2.5-long.json").read_text())
        graph.pop("_meta")
        self.assertEqual(check_graph(graph), [])


class H3ChainGraphSignatureTests(unittest.TestCase):
    """The H3 chain uses core H3 nodes and the H3 Motion Context pack (pinned in
    docs/long-video-design.md); its two signatures come from that pack's code."""

    def _specs(self):
        source = ve.SourceVideo("s.mp4", 244, 24.0, 832, 480, True)
        silent = ve.SourceVideo("s.mp4", 244, 24.0, 960, 544, False)

        def spec(shots, has_source=False, **kw):
            segments = h3.plan_segments(shots, fps=24.0, has_source=has_source, max_segment_seconds=10.0)
            return ve.ChainSpec(segments, 960, 544, 24.0, 1, context_frames=kw.pop("context", 22), **kw)

        yield "text to video, 3 segments", spec([("a", 25)])
        yield "first frame image", spec([("a", 12)], first_frame_image="x.png")
        yield "storyboard", spec([("a", 5), ("b", 9), ("c", 3)])
        yield "extend, scaled source", spec([("b", 12)], True, source=source)
        yield "extend, silent source", spec([("b", 4)], True, source=silent)
        for context in (5, 39, 56):
            yield f"context {context}", spec([("a", 5), ("b", 5)], context=context)

    def test_every_h3_chain_graph_passes_the_signature_check(self) -> None:
        for label, spec in self._specs():
            with self.subTest(label):
                self.assertEqual(check_graph(h3.build_chain_graph(spec).graph), [])

    def test_manifest_reference_graph_passes(self) -> None:
        graph = json.loads((ROOT / "workflows/videos/video-minimax-h3-long.json").read_text())
        graph.pop("_meta")
        self.assertEqual(check_graph(graph), [])


class ReplaceGraphSignatureTests(unittest.TestCase):
    def _base(self) -> dict:
        graph = json.loads((ROOT / "workflows/videos/video-scail-2-replace.json").read_text())
        graph.pop("_meta")
        return graph

    def test_base_graph(self) -> None:
        self.assertEqual(check_graph(self._base()), [])

    def test_with_reference_fit(self) -> None:
        graph = self._base()
        vs.add_reference_fit(graph)
        self.assertEqual(check_graph(graph), [])

    def test_with_point_and_fit(self) -> None:
        graph = self._base()
        vs.apply_point(graph, {"source_point_x": 0.4, "source_point_y": 0.6}, 832, 448)
        vs.add_reference_fit(graph)
        self.assertEqual(check_graph(graph), [])


class CheckerTests(unittest.TestCase):
    """The checker has to catch the mistakes it exists for."""

    def test_it_catches_common_errors(self) -> None:
        g = {"a": {"class_type": "UNETLoader", "inputs": {"unet_name": "x.safetensors", "weight_dtype": "bogus"}},
             "b": {"class_type": "VAEEncode", "inputs": {"pixels": ["a", 0]}},
             "c": {"class_type": "EmptyLTXVLatentVideo", "inputs": {
                 "width": "960", "height": 512, "length": 9, "batch_size": 1, "extra": 1}}}
        text = "\n".join(check_graph(g))
        self.assertIn("bogus", text)                        # bad combo value
        self.assertIn("MODEL output linked to IMAGE", text)  # wrong link type
        self.assertIn("missing required input vae", text)
        self.assertIn("is not INT", text)                   # string for an INT
        self.assertIn("unknown input extra", text)


if __name__ == "__main__":
    unittest.main()
