"""What the store keeps when the owner changes a bundled workflow without replacing its contract.

r6 removes contract replacement under a bundled id. Its migration and its "turn the bundled one off" advice depend on
one shape the store must hold without a copy of the bundled contract: a run-control patch (state, publication). These
tests read the stored row and the served entry through the production snapshot builder, and pin the two refusals the
store gives a contract write under a bundled id.
"""
from __future__ import annotations

import copy

import pytest

from gateway import admin_store, registry_layers, service_registry
from gateway.definition_store import DefinitionStore
from gateway.workflow_registry_store import WorkflowIdIsBundled, WorkflowRegistryStore
from gateway.workflow_runtime_types import Actor, RegistryRefused
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_workflow_owner_api import _real_snapshot

ME = Actor("owner", "owner-1", "Owner")
BUNDLED_ID = "image.generate.krea2-turbo"


@pytest.fixture
def parts(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    store = WorkflowRegistryStore(path=db, definitions=DefinitionStore(tmp_path / "jobs.db"),
                                  build_snapshot=_real_snapshot, config=dict, on_changed=lambda: None)
    return store, db


def _generation(db) -> int:
    return int(admin_store.get_service_registry_json_with_generation(path=db)["operator_settings_generation"])


def _stored(db) -> dict:
    return copy.deepcopy(admin_store.get_service_registry_with_generation(path=db)["value"]["workflows"][BUNDLED_ID])


def test_turning_a_bundled_workflow_off_stores_run_control_only_and_the_bundled_contract_still_serves(parts):
    store, db = parts
    before = store.snapshot().get(BUNDLED_ID)

    store.set_state(BUNDLED_ID, "disabled", if_match=_generation(db), actor=ME)

    row = _stored(db)
    inner = row.get(registry_layers.REPLACE_KEY, row)
    assert inner.get(registry_layers.RUN_CONTROL_PATCH_KEY) == BUNDLED_ID, row
    assert "manifest" not in inner and "overrides" not in inner and "policy" not in inner, row
    after = store.snapshot().get(BUNDLED_ID)
    assert after.state == "disabled"
    assert after.contract_digest == before.contract_digest
    assert after.facts == before.facts


def test_a_first_save_under_a_bundled_id_is_refused_with_the_save_as_new_advice(parts):
    store, _db = parts
    contract = copy.deepcopy(service_registry.bundled_workflow_manifests()[BUNDLED_ID])
    with pytest.raises(WorkflowIdIsBundled) as refused:
        store.put_workflow(contract, if_match=None, actor=ME, facts=store.snapshot().get(BUNDLED_ID).facts)
    assert "save it under a new id" in str(refused.value)
    assert refused.value.bundled == service_registry.bundled_workflow_manifests()[BUNDLED_ID]


def test_an_update_under_a_bundled_id_is_refused_even_after_the_owner_turned_it_off(parts):
    """A stored run-control patch does not open the id to a contract: the update is refused and the patch stays."""
    store, db = parts
    store.set_state(BUNDLED_ID, "disabled", if_match=_generation(db), actor=ME)
    stored_before = _stored(db)
    contract = copy.deepcopy(service_registry.bundled_workflow_manifests()[BUNDLED_ID])
    with pytest.raises(WorkflowIdIsBundled):
        store.put_workflow(contract, if_match=_generation(db), actor=ME, facts=store.snapshot().get(BUNDLED_ID).facts)
    assert _stored(db) == stored_before
