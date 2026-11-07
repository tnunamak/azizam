"""The mesh route reads its workflow record on every request and listing, not once at import.

The owner can change workflow records while the gateway runs (``GATEWAY_WORKFLOW_REGISTRY.adopt``). The receipt, the policy
request and the discovery facts of the mesh route must then show the record now in the registry, with no reimport of proxy.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import proxy
from gateway.discovery import WorkflowRegistry
from gateway.manifests import Provenance

NEW_VERSION = "9.9.9-live-record"


def _adopt_changed_record() -> str:
    """Adopt a registry whose mesh record has another version. Returns the new version."""
    old = proxy.GATEWAY_WORKFLOW_REGISTRY.get(proxy._MESH_WORKFLOW_ID) or proxy._BUNDLED_MESH_RECORD
    manifest = copy.deepcopy(old.manifest)
    assert manifest["version"] != NEW_VERSION
    manifest["version"] = NEW_VERSION
    changed = WorkflowRegistry()
    changed.add_manifest(manifest, source_path=old.source_path, provenance=Provenance(trusted=old.identity.trusted, source=old.identity.source), facts=old.facts)
    proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(changed)
    return NEW_VERSION


@pytest.fixture
def restored_registry():
    saved = proxy.GATEWAY_WORKFLOW_REGISTRY.contents()
    yield
    proxy.GATEWAY_WORKFLOW_REGISTRY.restore(saved)


def test_an_adopted_mesh_record_reaches_the_receipt_the_policy_and_the_discovery_facts(restored_registry, monkeypatch):
    before = proxy.current_mesh_workflow()
    assert before.version != NEW_VERSION
    version = _adopt_changed_record()

    auth = SimpleNamespace(identity=None)
    receipt = proxy._mesh_open_receipt(auth, "req-live", "/v1/meshes/generate", proxy.current_mesh_workflow()).receipt
    assert (receipt.manifest_version, receipt.manifest_hash) == (version, proxy.current_mesh_workflow().manifest_hash)
    assert receipt.manifest_hash != before.manifest_hash

    seen = {}
    monkeypatch.setattr(proxy, "evaluate_and_apply", lambda identity, request: seen.setdefault("request", request))
    monkeypatch.setattr(proxy, "make_policy_request", lambda **kwargs: SimpleNamespace(**kwargs))
    proxy._mesh_route_policy(auth, {"runtime_policy": {
        "capability_exposure": "default", "capability_trust_tier": "stable", "capability_resource_tier": "batch"}},
        proxy.current_mesh_workflow())
    assert (seen["request"].workflow, seen["request"].backend) == (before.id, before.service_id)

    assert proxy.MESH_DEPS.current_manifest().version == version
    assert proxy._mesh_facts({}).workflow == before.id
    assert proxy.current_mesh_workflow() is proxy.current_mesh_workflow()  # converted once per record, not per call


def test_an_adopted_mesh_record_with_another_service_reaches_the_facts_and_the_policy(restored_registry, monkeypatch):
    before = proxy.current_mesh_workflow()
    old = proxy.GATEWAY_WORKFLOW_REGISTRY.get(proxy._MESH_WORKFLOW_ID) or proxy._BUNDLED_MESH_RECORD
    manifest = copy.deepcopy(old.manifest)
    text = repr(manifest)
    assert before.service_id in text
    manifest = _replace_service(manifest, before.service_id, "swapped-engine")
    changed = WorkflowRegistry()
    changed.add_manifest(manifest, source_path=old.source_path, provenance=Provenance(trusted=old.identity.trusted, source=old.identity.source), facts=old.facts)
    proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(changed)

    assert proxy.current_mesh_workflow().service_id == "swapped-engine"
    assert proxy._mesh_facts({}).backend == "swapped-engine"
    receipt = proxy._mesh_open_receipt(
        SimpleNamespace(identity=None), "req-live-2", "/v1/meshes/generate", proxy.current_mesh_workflow()).receipt
    assert receipt.backend == "swapped-engine"


def _replace_service(node, old, new):
    if isinstance(node, dict):
        return {key: _replace_service(value, old, new) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace_service(value, old, new) for value in node]
    return new if node == old else node
