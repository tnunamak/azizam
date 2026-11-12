"""The Pixal3D workflow is a bundled mesh record beside TRELLIS.2: data only, on the same GPU pool."""

from __future__ import annotations

import dataclasses

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


@pytest.mark.parametrize("tags,expected", [
    (("rank:²",), workflow_policy.UNRANKED),
    (("rank:", "rank:-1", "rank:no"), workflow_policy.UNRANKED),
    (("rank:99999999999999999999",), 99999999999999999999),
    (("rank:3", "rank:1"), 1),
])
def test_rank_tags_ignore_invalid_numbers_and_leave_unranked_records_last(tags, expected) -> None:
    facts = dataclasses.replace(RECORDS.get(PIXAL3D_ID).facts, tags=tags)
    assert workflow_policy.rank_of(facts) == expected
    assert workflow_policy.rank_of(dataclasses.replace(facts, tags=())) > 99999999999999999999
