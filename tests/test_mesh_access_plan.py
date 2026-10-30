"""The mesh route's intent is granted like the other image workflows: a plan lists the workflow under images."""

from __future__ import annotations

from gateway.access_plans import capability_for_intent
from gateway.mesh_manifest import MESH_INTENT
from tests.support.access import plan_for


def test_the_mesh_intent_maps_to_a_capability():
    assert capability_for_intent(MESH_INTENT) == "image"


def test_a_plan_that_lists_the_trellis_workflow_under_images_grants_it():
    grant = plan_for(caps=("image",), workflows=["mesh.generate.trellis-2"]).can_use["image"]

    assert grant.allows(model=None, workflow="mesh.generate.trellis-2")
    assert not grant.allows(model=None, workflow="image.generate.other")
