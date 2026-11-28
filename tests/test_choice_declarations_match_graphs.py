"""Keep engine-derived choices aligned with the graphs that consume them."""

import json
from pathlib import Path

import yaml


EXPECTED = {
    ("image.edit.seedvr2-upscale", "color_correction_method"),
    ("audio.generate.ace-step-1.5-xl-turbo", "timesignature"),
    ("audio.generate.ace-step-1.5-xl-turbo", "language"),
    ("audio.generate.ace-step-1.5-xl-turbo", "keyscale"),
    ("image.generate.krea2-turbo", "sampler"),
    ("image.generate.krea2-turbo", "scheduler"),
    ("image.edit.qwen-image-2.1-edit", "sampler"),
    ("image.edit.qwen-image-2.1-edit", "scheduler"),
}
ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "manifests" / "workflows"


def test_choice_declarations_match_graphs():
    paths = sorted(MANIFESTS.glob("*.yml"))
    assert paths, "No workflow manifests found"
    declared = set()
    for path in paths:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict), path
        for name, field in manifest.get("inputs", {}).items():
            if "choices_from" not in field:
                continue
            context = f"{path.name}:{name}"
            pair = (manifest["id"], name)
            assert pair not in declared, f"{context}: duplicate declaration"
            declared.add(pair)
            assert field.get("type") == "string", context
            choices = field.get("enum")
            assert isinstance(choices, list) and choices, context
            assert all(isinstance(choice, str) for choice in choices), context
            assert len(choices) == len(set(choices)), context
            source = field["choices_from"]
            assert isinstance(source, dict), context
            assert isinstance(source.get("node_type"), str) and source["node_type"], context
            assert isinstance(source.get("input"), str) and source["input"], context
            mapped = False
            for binding in manifest.get("bindings", {}).values():
                mapping = binding.get("map", {})
                if name not in mapping:
                    continue
                mapped = True
                targets = mapping[name]
                assert isinstance(targets, list) and targets, context
                workflow = binding.get("workflow")
                assert isinstance(workflow, str) and workflow, context
                relative = Path(workflow)
                assert not relative.is_absolute(), context
                graph_path = (ROOT / relative).resolve()
                assert graph_path.is_relative_to(ROOT), context
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                assert isinstance(graph, dict), context
                for target in targets:
                    assert isinstance(target, dict), context
                    node_id = target.get("node")
                    assert isinstance(node_id, str) and node_id in graph, context
                    node = graph[node_id]
                    assert isinstance(node, dict), context
                    assert node.get("class_type") == source["node_type"], context
                    assert target.get("input") == source["input"], context
                    assert source["input"] in node.get("inputs", {}), context
            assert mapped, f"{context}: no binding maps this input"
    assert declared == EXPECTED
