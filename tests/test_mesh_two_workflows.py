"""Two image-to-3D records on one card (docs/design/MESH_WORKFLOW_SELECTION.md).

A request names a record with ``model``; a request that names none runs the first choice, which the records' ``rank:N``
tags declare. The tests use the stand-in engines of the mesh harness: nothing here calls a real service.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses

import pytest

import proxy
from gateway import exclusive_pool, mesh_api, workflow_policy
from gateway.discovery import WorkflowRegistry
from gateway.frontend_shapes import bundled_rows, _wire, mesh as frontend_mesh_shapes, shapes as frontend_wire_shapes
from gateway.manifests import Provenance
from tests.support.gateway import gateway_client
from tests.support.mesh_harness import GLB, PIXAL3D_GLB, Harness
from tests.test_adapter_differential_s7_mesh_rows import RAW, SYNC, ASYNC, answer, form, job_id_of, rig, run, settle  # noqa: F401

TRELLIS = "mesh.generate.trellis-2"
PIXAL = "mesh.generate.pixal3d"


@pytest.fixture
def ranked(rig, monkeypatch):
    """The rig stands in for TRELLIS.2 by default; these tests want the deployment's own first choice back."""
    for shape in (frontend_mesh_shapes.LocalAI3D, frontend_mesh_shapes.RawGlb):
        monkeypatch.setitem(frontend_wire_shapes.DEFAULT_WORKFLOWS, shape.shape_id, proxy._mesh_default_workflow)
    saved = proxy.GATEWAY_WORKFLOW_REGISTRY.contents()
    yield rig
    proxy.GATEWAY_WORKFLOW_REGISTRY.restore(saved)


def _retag(tags_by_id: dict[str, tuple[str, ...]]) -> None:
    """Adopt a registry in which the named mesh records carry other tags (an owner's edit of the rank)."""
    changed = WorkflowRegistry()
    for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records():
        facts = record.facts
        if record.id in tags_by_id:
            facts = dataclasses.replace(facts, tags=tags_by_id[record.id])
        changed.add_manifest(copy.deepcopy(record.manifest), source_path=record.source_path,
                             provenance=Provenance(trusted=record.identity.trusted, source=record.identity.source), facts=facts)
    proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(changed)


def test_a_request_names_a_record_with_model_and_reaches_its_own_service(ranked):
    async def go(client):
        pixal = await client.post(RAW, data={"model": PIXAL, "seed": "1"}, files=form()["files"])
        trellis = await client.post(RAW, data={"model": TRELLIS, "seed": "1"}, files=form()["files"])
        return pixal, trellis

    async def scenario():
        async with gateway_client() as client:
            return await go(client)

    pixal, trellis = asyncio.run(scenario())
    assert (pixal.status_code, pixal.content) == (200, PIXAL3D_GLB)
    assert (trellis.status_code, trellis.content) == (200, GLB)
    assert (len(ranked.pixal_downstream), len(ranked.downstream)) == (1, 1)


def test_a_request_that_names_no_model_runs_the_first_choice_the_rank_data_declares(ranked):
    async def scenario():
        async with gateway_client() as client:
            first = await client.post(RAW, **form())
            _retag({PIXAL: ("rank:3",)})
            second = await client.post(RAW, **form())
            return first, second

    first, second = asyncio.run(scenario())
    assert first.content == PIXAL3D_GLB      # rank:1 ships on Pixal3D
    assert second.content == GLB             # the owner's edit moved the first choice
    assert (len(ranked.pixal_downstream), len(ranked.downstream)) == (1, 1)


def test_the_default_follows_the_rank_tags_and_ties_go_to_the_lower_id(ranked):
    assert proxy._mesh_default_workflow() == PIXAL
    _retag({PIXAL: ("rank:5",), TRELLIS: ("rank:2",)})
    assert proxy._mesh_default_workflow() == TRELLIS
    _retag({PIXAL: ("rank:2",), TRELLIS: ("rank:2",)})
    assert proxy._mesh_default_workflow() == PIXAL     # "mesh.generate.pixal3d" < "mesh.generate.trellis-2"
    _retag({PIXAL: (), TRELLIS: ()})
    assert [w.id for w in proxy.current_mesh_workflows()] == [PIXAL, TRELLIS]
    assert workflow_policy.rank_of(proxy.GATEWAY_WORKFLOW_REGISTRY.get(PIXAL).facts) == workflow_policy.UNRANKED


def test_an_async_job_runs_on_the_record_named_and_the_catalog_lists_both(ranked):
    async def go(client):
        created = await client.post(SYNC, headers=ASYNC, data={"model": TRELLIS}, files=form()["files"])
        return await settle(client, job_id_of(created))

    async def scenario():
        async with gateway_client() as client:
            return await go(client)

    job = asyncio.run(scenario())
    assert job["status"] == "succeeded"
    assert (len(ranked.downstream), len(ranked.pixal_downstream)) == (1, 0)
    assert {c.workflow for c in proxy._mesh_facts({})} == {TRELLIS, PIXAL}
    assert {c.backend for c in proxy._mesh_facts({})} == {"trellis", "pixal3d"}


def test_each_record_has_its_own_availability_and_the_two_share_one_pool():
    harness = Harness()
    workflows = proxy.current_mesh_workflows()
    present = lambda _id: True  # noqa: E731
    both = asyncio.run(mesh_api.records_evidence(harness.deps(), workflows, present))
    assert {service: item["available"] for service, item in both.items()} == {"trellis": True, "pixal3d": True}
    pools = {exclusive_pool.parse_resource_capacity(name, harness.services[name]["config"]["resource_capacity"]).pool_id for name in ("trellis", "pixal3d")}
    assert len(pools) == 1

    harness = Harness()
    harness.pixal_health_status = 503
    one = asyncio.run(mesh_api.records_evidence(harness.deps(), workflows, present))
    assert (one["trellis"]["available"], one["pixal3d"]["available"]) == (True, False)


def test_a_record_the_registry_lacks_is_unavailable_and_the_other_stays_available():
    harness = Harness()
    out = asyncio.run(mesh_api.records_evidence(harness.deps(), proxy.current_mesh_workflows(), lambda workflow_id: workflow_id != PIXAL))
    assert (out["trellis"]["available"], out["pixal3d"]["available"]) == (True, False)


def test_a_preferred_workflow_settles_an_ambiguous_choice_only_when_it_fits(monkeypatch):
    snapshot = proxy.GATEWAY_REGISTRY_PROVIDER.current()
    row = next(r for r in bundled_rows() if r.id == "mesh-generations")
    chosen = _wire.choose_workflow(snapshot, row, target=None, supplied=["image"], prefer=TRELLIS)
    assert chosen.id == TRELLIS
    chosen = _wire.choose_workflow(snapshot, row, target=None, supplied=["image"], prefer="mesh.generate.nothing")
    assert getattr(chosen, "status_code", None) == 400
    named = _wire.choose_workflow(snapshot, row, target=PIXAL, supplied=["image"], prefer=TRELLIS)
    assert named.id == PIXAL      # a named model always wins over the preference
