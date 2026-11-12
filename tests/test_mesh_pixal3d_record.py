"""The Pixal3D workflow is a bundled mesh record beside TRELLIS.2: data only, on the same GPU pool."""

from __future__ import annotations

import dataclasses
import json

import pytest
import yaml

from gateway import exclusive_pool, mesh_api, workflow_policy
from gateway.discovery import WorkflowRegistry
from tests.mesh_workflows import BUNDLED_ID, ROOT, bundled_document, workflow_of

RECORDS = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
PIXAL3D_ID = "mesh.generate.pixal3d"
SERVICES = yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8"))["backends"]


def test_the_record_passes_the_mesh_profile_and_binds_its_own_service() -> None:
    record = RECORDS.get(PIXAL3D_ID)
    assert mesh_api.mesh_profile_errors(record.contract) == []
    workflow = workflow_of(record.contract)
    assert (workflow.image_input, workflow.service_id, workflow.transport_path) == ("image", "pixal3d", "/generate")
    assert set(record.contract["inputs"]) <= set(bundled_document()["inputs"])


def test_both_records_declare_the_same_exclusive_pool() -> None:
    pools = {name: exclusive_pool.parse_resource_capacity(name, SERVICES[name]["resource_capacity"]) for name in ("trellis", "pixal3d")}
    assert pools["pixal3d"].pool_id == pools["trellis"].pool_id == "gpu0"
    assert pools["pixal3d"].observer_service == pools["trellis"].observer_service


def test_the_first_choice_is_stated_by_rank_data_not_by_name() -> None:
    ranks = {record_id: workflow_policy.rank_of(RECORDS.get(record_id).facts) for record_id in (PIXAL3D_ID, BUNDLED_ID)}
    assert ranks[PIXAL3D_ID] < ranks[BUNDLED_ID]
    assert workflow_policy.rank_of(None) == workflow_policy.UNRANKED


def test_the_engine_service_holds_no_local_paths_or_addresses() -> None:
    for path in [ROOT / "engines" / "pixal3d_comfy_service.py", ROOT / "docs/examples/mesh-pixal3d/README.md"]:
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "192.168." not in text, path.name


@pytest.mark.parametrize("rank,expected", [(None, workflow_policy.UNRANKED), (0, 0), (3, 3), (workflow_policy.MAX_SELECTION_RANK, workflow_policy.MAX_SELECTION_RANK)])
def test_selection_rank_orders_records_and_an_unranked_record_comes_last(rank, expected) -> None:
    facts = dataclasses.replace(RECORDS.get(PIXAL3D_ID).facts, selection_rank=rank)
    assert workflow_policy.rank_of(facts) == expected
    assert workflow_policy.rank_of(dataclasses.replace(facts, selection_rank=None)) > workflow_policy.MAX_SELECTION_RANK


@pytest.mark.parametrize("bad", [True, False, -1, workflow_policy.MAX_SELECTION_RANK + 1, 10**30, 1.5, "1", [1]])
def test_a_selection_rank_that_is_not_a_bounded_integer_states_no_facts(bad) -> None:
    stated = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "media", "tags": []}
    assert workflow_policy.facts_from_mapping({**stated, "selection_rank": 2}).selection_rank == 2
    assert workflow_policy.facts_from_mapping({**stated, "selection_rank": bad}) is None
    text = "schema_version: 1\nworkflows:\n  w: {exposure: default, trust_tier: stable, resource_tier: batch, resource_class: media, tags: [], selection_rank: %s}\n"
    with pytest.raises(workflow_policy.PolicyFileError):
        workflow_policy.parse_policy_text(text % json.dumps(bad))
