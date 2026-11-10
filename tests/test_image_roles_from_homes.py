"""The image routes take every node role of a shipped image workflow from its data homes, not from the graph's _meta.

Homes: the manifest's binding map (image_map_targets) and the images registry section's graph_roles
(image_graph_roles). ``proxy._image_graph_roles`` merges them into the shape the route code reads.
For each shipped graph the roles the routes read must not depend on the graph file's ``_meta`` block.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import proxy
import yaml
from gateway.discovery import WorkflowRegistry
from tests.test_image_graph_golden import release_graph_roles

ROOT = Path(__file__).resolve().parents[1]
# The keys the image routes read from a graph's roles.
READ = ("name", "prompt_node", "size_node", "seed_node", "batch_size_node", "image_node", "image_nodes",
        "background_removal")


def _shipped():
    found = []
    for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records():
        binding = record.manifest.get("bindings", {}).get(proxy.DERIVED._GRAPH_BINDING) or {}
        path = binding.get("workflow")
        if isinstance(path, str) and path.startswith("workflows/images/") and ".ui" not in path:
            found.append(pytest.param(record.manifest, path, id=Path(path).stem))
    return found


SHIPPED = _shipped()


def _roles(manifest, path, graph_meta):
    graph = json.loads((ROOT / path).read_text())
    graph.pop("_meta")
    return proxy._image_graph_roles(copy.deepcopy(manifest), graph_meta, graph, Path(path).stem)


@pytest.fixture(autouse=True)
def release_table(monkeypatch):
    """The test deployment boots with an empty table (see release_graph_roles); the release's own is put in place."""
    monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", release_graph_roles())


def _release_section():
    return yaml.safe_load((ROOT / "registry" / "defaults" / "images.yml").read_text())


def test_the_release_table_covers_all_eighteen_shipped_graphs():
    assert len(SHIPPED) == 18
    assert {param.values[1] for param in SHIPPED} == set(proxy._IMAGE_GRAPH_ROLES)


def test_the_loader_the_boot_and_each_reload_use_checks_the_table_against_the_registry():
    registry = proxy.GATEWAY_WORKFLOW_REGISTRY
    binding = proxy.DERIVED._GRAPH_BINDING
    assert len(proxy._load_image_graph_roles(_release_section(), registry, binding)) == 18
    section = _release_section()
    section["graph_roles"]["workflows/images/not-a-graph.json"] = {}
    with pytest.raises(ValueError, match="not-a-graph"):
        proxy._load_image_graph_roles(section, registry, binding)
    # A registry that could not be read has no graphs to check against; an images section that is not a mapping has no table.
    assert proxy._load_image_graph_roles(section, WorkflowRegistry(), binding) == {}
    assert proxy._load_image_graph_roles(None, registry, binding) == {}


@pytest.mark.parametrize(("manifest", "path"), SHIPPED)
def test_the_roles_the_routes_read_do_not_depend_on_the_graphs_meta_block(manifest, path):
    meta = json.loads((ROOT / path).read_text())["_meta"]
    with_meta = _roles(manifest, path, meta)
    without_meta = _roles(manifest, path, {})
    assert {key: with_meta.get(key) for key in READ} == {key: without_meta.get(key) for key in READ}


def test_partial_role_entry_preserves_metadata_reference_order(monkeypatch):
    path = "workflows/images/qwen-image-2.1-edit-multiref.json"
    manifest = next(p.values[0] for p in SHIPPED if p.values[1] == path)
    meta = json.loads((ROOT / path).read_text())["_meta"]
    table = release_graph_roles()
    monkeypatch.setattr(proxy, "_IMAGE_GRAPH_ROLES", {path: {"background": table[path]["background"]}})
    assert _roles(manifest, path, meta)["image_nodes"] == ["4", "11"]


@pytest.mark.parametrize("family", ["name", "prompt", "size", "seed", "batch", "references", "background"])
def test_data_wins_when_graph_metadata_disagrees(family):
    path = ("workflows/images/qwen-image-2.1-edit-multiref.json" if family == "references"
            else "workflows/images/flux2-klein-edit.json" if family == "batch"
            else "workflows/images/krea2-turbo.json")
    manifest = next(p.values[0] for p in SHIPPED if p.values[1] == path)
    meta = json.loads((ROOT / path).read_text())["_meta"]
    keys = {"name": ["name"], "prompt": ["prompt_node"], "size": ["size_node"],
            "seed": ["seed_node"], "batch": ["batch_size_node"],
            "references": ["image_node", "image_nodes"], "background": ["background_removal"]}[family]
    expected = _roles(manifest, path, meta)
    for key in keys:
        meta[key] = ["wrong-b", "wrong-a"] if key == "image_nodes" else "wrong"
    actual = _roles(manifest, path, meta)
    assert {k: actual.get(k) for k in keys} == {k: expected.get(k) for k in keys}
