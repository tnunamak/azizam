"""The mesh route's intent is its own capability, "3D models". A plan grants it on its own, never through images."""

from __future__ import annotations

from gateway.access_plans import CAPABILITIES, capability_for_intent
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.mesh_manifest import MESH_INTENT
from gateway.policy import PolicyRequest, evaluate
from tests.support.access import access, everything, plan_for

MESH_WORKFLOW = "mesh.generate.trellis-2"


def test_the_mesh_intent_maps_to_the_mesh_capability():
    assert capability_for_intent(MESH_INTENT) == "mesh"
    assert "mesh" in CAPABILITIES
    assert CAPABILITY_LABELS["mesh"] == "3D models"


def test_a_plan_that_allows_only_images_is_refused_the_mesh_route():
    images_only = plan_for(caps=("image",), workflows=[MESH_WORKFLOW])

    decision = evaluate(access(images_only, images_only),
                        PolicyRequest(intent=MESH_INTENT, workflow=MESH_WORKFLOW))

    assert decision.denied


def test_a_plan_that_allows_3d_models_is_admitted_to_the_mesh_route():
    mesh_only = plan_for(caps=("mesh",), workflows=[MESH_WORKFLOW])

    decision = evaluate(access(mesh_only, mesh_only),
                        PolicyRequest(intent=MESH_INTENT, workflow=MESH_WORKFLOW))

    assert decision.allowed


def test_a_plan_lists_the_trellis_workflow_under_3d_models_and_grants_it():
    grant = plan_for(caps=("mesh",), workflows=[MESH_WORKFLOW]).can_use["mesh"]

    assert grant.allows(model=None, workflow=MESH_WORKFLOW)
    assert not grant.allows(model=None, workflow="image.generate.other")


def test_a_plan_that_grants_every_capability_grants_the_mesh_route():
    decision = evaluate(access(everything(), everything()),
                        PolicyRequest(intent=MESH_INTENT, workflow=MESH_WORKFLOW))

    assert decision.allowed
