"""Binding-map targets, including every bundled non-UI image workflow."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway import image_map_targets  # noqa: E402

BINDING = "comfyui"  # every shipped image graph's binding key


def targets(contract):
    """The reader over the shipped image graphs' binding."""
    return image_map_targets.targets(contract, BINDING)

FIELDS = ("prompt", "negative_prompt", "seed", "width", "height", "batch_size", "lora_strength")


class ImageMapTargetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        cls.entries = {}
        for record in cls.registry.records():
            binding = record.contract.get("bindings", {}).get("comfyui", {})
            workflow = binding.get("workflow")
            if not isinstance(workflow, str):
                continue
            path = Path(workflow)
            if path.parent == Path("workflows/images") and ".ui" not in path.name:
                cls.entries[path.name] = (
                    record.contract,
                    json.loads((ROOT / path).read_text(encoding="utf-8")),
                )

    def result(self, filename: str):
        return targets(self.entries[filename][0])

    def test_registry_covers_all_18_non_ui_image_graphs(self) -> None:
        self.assertEqual(self.registry.errors, [])
        graphs = {path.name for path in (ROOT / "workflows/images").glob("*.json")
                  if ".ui" not in path.name}
        self.assertEqual(len(graphs), 18)
        self.assertEqual(set(self.entries), graphs)

    def test_fibo_and_qwen_prompt_inputs(self) -> None:
        self.assertEqual(self.result("fibo-2-edit-turbo.json").prompt, (("6", "prompt"),))
        self.assertEqual(self.result("qwen-image-2.1-edit.json").prompt, (("6", "text"),))

    def test_edit_and_inpaint_size_fanout_in_map_order(self) -> None:
        edit = self.result("flux2-klein-4b-edit.json")
        inpaint = self.result("flux2-klein-4b-inpaint.json")
        for field in ("width", "height"):
            with self.subTest(field=field):
                self.assertEqual(getattr(edit, field), (("48", field), ("47", field)))
                self.assertEqual(getattr(inpaint, field),
                                 (("11", field), ("14", field), ("48", field)))

    def test_multiref_assets_and_unmapped_mask(self) -> None:
        multi = self.result("qwen-image-2.1-edit-multiref.json")
        self.assertEqual(tuple(multi.assets), ("source_image", "reference_image_2"))
        self.assertEqual(multi.assets["source_image"], (("4", "image"),))
        self.assertEqual(multi.assets["reference_image_2"], (("11", "image"),))
        self.assertEqual(self.result("flux2-klein-4b-edit.json").assets["mask"], ())
        self.assertEqual(self.result("flux2-klein-4b-inpaint.json").assets["mask"],
                         (("12", "image"),))

    def test_lora_and_batch_are_read_from_the_map(self) -> None:
        result = self.result("krea2-turbo-snofs.json")
        self.assertEqual(result.lora_strength, (("11", "strength_model"),))
        self.assertEqual(result.batch_size, (("6", "batch_size"),))
        # Graph metadata must not manufacture an absent binding.
        self.assertEqual(self.result("flux2-klein-4b-edit.json").batch_size, ())

    def test_every_pair_names_an_existing_graph_input(self) -> None:
        for filename, (contract, graph) in self.entries.items():
            result = targets(contract)
            groups = {name: getattr(result, name) for name in FIELDS}
            groups.update(result.assets)
            mapping = contract["bindings"]["comfyui"].get("map", {})
            for name, pairs in groups.items():
                with self.subTest(graph=filename, input=name):
                    self.assertIsInstance(pairs, tuple)
                    expected = tuple(
                        (str(item["node"]), str(item["input"]))
                        for item in mapping.get(name, [])
                        if "node" in item and "input" in item
                    )
                    self.assertEqual(pairs, expected)
                    for node, input_name in pairs:
                        self.assertIn(node, graph)
                        self.assertIn(input_name, graph[node]["inputs"])

    def test_all_contracts_are_unchanged_and_deep_copies_compare_equal(self) -> None:
        for filename, (contract, _) in self.entries.items():
            with self.subTest(graph=filename):
                before = copy.deepcopy(contract)
                result = targets(contract)
                self.assertEqual(contract, before)
                self.assertEqual(result, targets(copy.deepcopy(contract)))
                self.assertEqual(contract, before)

    def test_table_of_mapped_meta_roles(self) -> None:
        roles = (
            ("prompt_node", ("prompt",)),
            ("seed_node", ("seed",)),
            ("size_node", ("width", "height")),
            ("batch_size_node", ("batch_size",)),
            ("lora_node", ("lora_strength",)),
        )
        for filename, (contract, graph) in self.entries.items():
            result = targets(contract)
            mapping = contract["bindings"]["comfyui"].get("map", {})
            meta = graph.get("_meta", {})
            for role, fields in roles:
                if role not in meta:
                    continue
                for field in fields:
                    if field not in mapping:
                        continue  # Only roles that the binding map carries.
                    with self.subTest(graph=filename, role=role, field=field):
                        self.assertIn(str(meta[role]),
                                      {node for node, _ in getattr(result, field)})
            asset_nodes = {node for name, pairs in result.assets.items()
                           if name in mapping for node, _ in pairs}
            if any(name in mapping for name in result.assets):
                for role in ("image_node", "image_nodes"):
                    if role not in meta:
                        continue
                    nodes = meta[role] if isinstance(meta[role], list) else [meta[role]]
                    for node in nodes:
                        with self.subTest(graph=filename, role=role, node=node):
                            self.assertIn(str(node), asset_nodes)

    def test_order_duplicates_and_node_input_stringification(self) -> None:
        result = targets({"bindings": {"comfyui": {"map": {
            "prompt": [{"node": 12, "input": "text"}, {"node": "2", "input": "prompt"},
                       {"node": 12, "input": "text"}],
            "seed": [{"node": 3, "input": 4}],
        }}}})
        self.assertEqual(result.prompt, (("12", "text"), ("2", "prompt"), ("12", "text")))
        self.assertEqual(result.seed, (("3", "4"),))

    def test_missing_targets_are_empty_and_only_asset_inputs_are_included(self) -> None:
        result = targets({"inputs": {"source": {"type": "asset"}, "legacy": {"type": "image"},
                                     "prompt": {"type": "string"}}})
        for name in FIELDS:
            with self.subTest(input=name):
                self.assertEqual(getattr(result, name), ())
        self.assertEqual(dict(result.assets), {"source": ()})
        self.assertEqual(dict(result.path_mappings), {})

    def test_paths_stay_separate_from_node_input_pairs(self) -> None:
        result = targets({"inputs": {"source": {"type": "asset"}}, "bindings": {"comfyui": {"map": {
            "prompt": [{"path": "request.prompt"}, {"node": "6", "input": "text"},
                       {"path": "fallback.prompt"}],
            "source": [{"path": "request.image"}],
            "steps": [{"path": "request.steps"}],
            # Match _apply_mapping: node/input wins when both forms are present.
            "seed": [{"node": "7", "input": "seed", "path": "unused.seed"}],
        }}}})
        self.assertEqual(result.prompt, (("6", "text"),))
        self.assertEqual(result.seed, (("7", "seed"),))
        self.assertEqual(result.assets["source"], ())
        self.assertEqual(tuple(result.path_mappings), ("prompt", "source", "steps"))
        self.assertEqual(dict(result.path_mappings), {
            "prompt": ("request.prompt", "fallback.prompt"),
            "source": ("request.image",), "steps": ("request.steps",),
        })

    def test_result_is_frozen_including_nested_mappings_and_detached(self) -> None:
        contract = {"inputs": {"source": {"type": "asset"}}, "bindings": {"comfyui": {"map": {
            "prompt": [{"node": "6", "input": "text"}],
            "source": [{"node": "4", "input": "image"}],
            "seed": [{"path": "request.seed"}],
        }}}}
        result = targets(contract)
        with self.assertRaises(FrozenInstanceError):
            result.prompt = ()
        with self.assertRaises(TypeError):
            result.assets["source"] = ()
        with self.assertRaises(TypeError):
            result.path_mappings["seed"] = ()
        with self.assertRaises(TypeError):
            result.assets["source"][0] = ("5", "image")
        contract["bindings"]["comfyui"]["map"]["prompt"][0]["node"] = "changed"
        contract["bindings"]["comfyui"]["map"]["source"][0]["input"] = "changed"
        contract["bindings"]["comfyui"]["map"]["seed"][0]["path"] = "changed"
        self.assertEqual(result.prompt, (("6", "text"),))
        self.assertEqual(result.assets["source"], (("4", "image"),))
        self.assertEqual(result.path_mappings["seed"], ("request.seed",))

    def test_only_the_named_binding_map_supplies_targets(self) -> None:
        contract = {
            "_meta": {"prompt_node": "wrong"},
            "inputs": {"prompt": {"type": "string", "node": "wrong"}},
            "bindings": {
                "other": {"map": {"prompt": [{"node": "wrong", "input": "wrong"}]}},
                "comfyui": {"selectors": {"prompt": {"node": "wrong"}},
                            "map": {"prompt": [{"node": "6", "input": "prompt"}]}},
            },
        }
        self.assertEqual(targets(contract).prompt, (("6", "prompt"),))
        del contract["bindings"]["comfyui"]
        self.assertEqual(targets(contract).prompt, ())
        # The caller names the binding; another key reads that binding's map, with no engine name in the reader.
        self.assertEqual(image_map_targets.targets(contract, "other").prompt, (("wrong", "wrong"),))


if __name__ == "__main__":
    unittest.main()
