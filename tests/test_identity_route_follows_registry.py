"""The identity route reads its workflow from the registry serving now, so a workflow added after boot works without a restart."""
from __future__ import annotations

import copy

import pytest

import proxy
from gateway.discovery import WorkflowRegistry
from tests.test_binding_descriptors import _TILE_MANIFEST

NONE_BOUND = "identity route must have exactly one workflow gateway binding"
MISSING = "identity workflow manifest is missing"


def _graph(marker: str) -> dict:
    return {"1": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "marker": marker}}}


def _manifest(workflow_id: str, model_id: str, marker: str) -> dict:
    manifest = copy.deepcopy(_TILE_MANIFEST)
    manifest["id"] = workflow_id
    manifest["bindings"]["gateway"].update(model_id=model_id, workflow={"inline": _graph(marker)})
    return manifest


def _registry(*manifests: dict) -> WorkflowRegistry:
    registry = WorkflowRegistry()
    for manifest in manifests:
        registry.add_manifest(manifest)
    return registry


@pytest.fixture
def live(monkeypatch) -> WorkflowRegistry:
    """The registry object the gateway holds from import; a reload swaps records into it with ``adopt``."""
    registry = WorkflowRegistry()
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry)
    monkeypatch.setattr(proxy, "_identity_template_cache", None)
    return registry


def test_a_workflow_added_after_boot_serves_without_a_restart(live):
    with pytest.raises(RuntimeError, match=MISSING):
        proxy._identity_template()
    assert proxy._identity_job_envelope() is None
    live.adopt(_registry(_manifest("image.edit.owner-a", "owner-a", "first")))
    assert proxy._identity_template()[0] == _graph("first")
    built = proxy._identity_job_envelope()
    assert built is not None and built.envelope.source_id == "image.edit.owner-a"
    assert proxy._identity_ids() == ("image.edit.owner-a", "owner-a")


def test_an_edited_workflow_replaces_the_cached_template(live):
    live.adopt(_registry(_manifest("image.edit.owner-a", "owner-a", "first")))
    assert proxy._identity_template()[0] == _graph("first")
    live.adopt(_registry(_manifest("image.edit.owner-a", "owner-a", "second")))
    assert proxy._identity_template()[0] == _graph("second")
    live.adopt(_registry(_manifest("image.edit.owner-b", "owner-b", "third")))
    assert proxy._identity_ids() == ("image.edit.owner-b", "owner-b")
    assert proxy._identity_template()[0] == _graph("third")


def test_removing_the_workflow_closes_the_route_again(live):
    live.adopt(_registry(_manifest("image.edit.owner-a", "owner-a", "first")))
    assert proxy._identity_template()[0] == _graph("first")
    live.adopt(_registry())
    assert proxy._identity_ids() == (None, None)
    with pytest.raises(RuntimeError, match=MISSING):
        proxy._identity_template()


def test_no_binding_and_two_bindings_keep_their_messages(live):
    with pytest.raises(ValueError, match=NONE_BOUND):
        proxy._identity_workflow_ids()
    assert proxy._identity_ids() == (None, None)
    live.adopt(_registry(_manifest("image.edit.owner-a", "owner-a", "a"), _manifest("image.edit.owner-b", "owner-b", "b")))
    with pytest.raises(ValueError, match=NONE_BOUND):
        proxy._identity_workflow_ids()
    assert proxy._identity_ids() == (None, None)
    with pytest.raises(RuntimeError, match=MISSING):
        proxy._identity_template()
