"""The production registry store over a real ``admin.db`` (CODE_TO_DATA_S4_S8, I2; acceptance rows 2, 4, 5, 7, 8, 9, 11,
16, 17, 21 and 22 on the store side). The snapshot builder is B's ``registry_snapshot.build``; until it lands the store is
built over a small adapter onto the fakes' snapshot, which holds the same fields."""
from __future__ import annotations

import copy
import threading
from types import MappingProxyType

import pytest

from gateway import admin_store, discovery, service_registry
from gateway.definition_store import DefinitionStore
from gateway.job_envelope import canonical_bytes
from gateway.workflow_registry_store import (
    Bounds, WorkflowIdIsBundled, WorkflowRegistryStore, bound_problems, bounds_from_config, history_depth_from_config,
    template_depth,
)
from gateway.workflow_runtime_types import (
    BOUND_EXCEEDED, ID_CLASH, WORKFLOW_EXISTS, Actor, RegistryConflict, RegistryRefused, WorkflowFacts,
)
from tests.registry_test_helpers import write_service_registry_for_test
from tests.workflow_runtime_fakes import FakeSnapshot, entry

ME = Actor("owner", "owner-1", "Owner")
BUNDLED_ID = "image.generate.flux2-klein-4b"
FACTS = WorkflowFacts(exposure="default", trust_tier="stable", resource_tier="batch", resource_class="image", tags=("a",))
MODEL_KINDS = frozenset({"model", "model_alias", "frontend", "route"})


def build_snapshot(document, bundled, *, generation):
    """The adapter: every workflow of the effective document as a fakes entry, and the non-workflow names as extras."""
    workflows = {}
    for workflow_id, record in (document.get("workflows") or {}).items():
        policy = record.get("policy")
        facts = WorkflowFacts(policy["exposure"], policy["trust_tier"], policy["resource_tier"],
                              policy.get("resource_class", ""), tuple(policy["tags"])) if policy else None
        workflows[workflow_id] = entry(
            record["manifest"], revision=record.get("record_revision", 1), state=record.get("state", "active"),
            published=record.get("published", True), facts=facts, incarnation=record.get("incarnation", "unstepped"),
            overrides=record.get("overrides"))
    extras = tuple(pair for pair in service_registry.registry_name_pairs(document, ()) if pair[1].kind in MODEL_KINDS)
    return FakeSnapshot(generation, MappingProxyType(workflows), MappingProxyType({}), extras)


def owner_contract(workflow_id: str = "owner.test.demo", **changes) -> dict:
    contract = copy.deepcopy(discovery.bundled_workflow_manifests()[BUNDLED_ID])
    contract["id"] = workflow_id
    contract.update(changes)
    return contract


@pytest.fixture
def parts(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    definitions = DefinitionStore(tmp_path / "jobs.db")
    changes = []
    config: dict = {}
    store = WorkflowRegistryStore(path=db, definitions=definitions, build_snapshot=build_snapshot,
                                  config=lambda: config, on_changed=lambda: changes.append(1))
    return store, db, definitions, config, changes


@pytest.fixture
def store(parts):
    return parts[0]


def generation(db) -> int:
    return int(admin_store.get_service_registry_json_with_generation(path=db)["operator_settings_generation"])


def test_a_create_is_unpublished_active_revision_one_with_a_random_incarnation(parts):
    store, db, *_ = parts
    commit = store.put_workflow(owner_contract(), if_match=None, actor=ME, facts=FACTS)
    entry_ = store.snapshot().get("owner.test.demo")
    assert (entry_.published, entry_.state, entry_.record_revision) == (False, "active", 1)
    assert entry_.incarnation and entry_.facts == FACTS
    assert (commit.revision, commit.record_revision) == (generation(db), 1)
    assert (commit.contract_digest, commit.record_digest) == (entry_.contract_digest, entry_.record_digest)


def test_a_create_of_an_existing_id_is_workflow_exists(store):
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    with pytest.raises(RegistryConflict) as raised:
        store.put_workflow(owner_contract(), if_match=None, actor=ME)
    assert raised.value.code == WORKFLOW_EXISTS


def test_two_edits_with_one_if_match_the_second_is_a_conflict_with_the_current_revision(parts):  # row 7
    store, db, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    seen = generation(db)
    store.put_workflow(owner_contract(display_name="One"), if_match=seen, actor=ME)
    with pytest.raises(RegistryConflict) as raised:
        store.put_workflow(owner_contract(display_name="Two"), if_match=seen, actor=ME)
    assert raised.value.current_revision == seen + 1 and raised.value.code == "generation_mismatch"
    assert store.snapshot().get("owner.test.demo").contract["display_name"] == "One"


def test_record_revision_rises_by_one_for_each_change_of_that_workflow_only(parts):
    store, db, *_ = parts
    store.put_workflow(owner_contract("owner.other.demo"), if_match=None, actor=ME)
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    store.set_published("owner.test.demo", True, if_match=generation(db), actor=ME)
    commit = store.set_state("owner.test.demo", "disabled", if_match=generation(db), actor=ME)
    assert commit.record_revision == 3
    assert store.snapshot().get("owner.other.demo").record_revision == 1


def test_publish_and_disable_are_run_control_not_the_contract(parts):  # rows 2, 4, 17
    store, db, *_ = parts
    first = store.put_workflow(owner_contract(), if_match=None, actor=ME)
    published = store.set_published("owner.test.demo", True, if_match=generation(db), actor=ME)
    disabled = store.set_state("owner.test.demo", "disabled", if_match=generation(db), actor=ME)
    enabled = store.set_state("owner.test.demo", "active", if_match=generation(db), actor=ME)
    snap = store.snapshot().get("owner.test.demo")
    assert (snap.published, snap.state) == (True, "active")
    assert {first.contract_digest, published.contract_digest, disabled.contract_digest} == {first.contract_digest}
    assert disabled.record_digest not in {published.record_digest, enabled.record_digest}
    assert enabled.record_digest == published.record_digest      # the state returned, the record is the same record
    assert snap.incarnation == store.snapshot().get("owner.test.demo").incarnation


def test_facts_are_kept_cleared_or_set(parts):
    store, db, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME, facts=FACTS)
    store.put_workflow(owner_contract(display_name="x"), if_match=generation(db), actor=ME)            # KEEP
    assert store.snapshot().get("owner.test.demo").facts == FACTS
    store.put_workflow(owner_contract(display_name="y"), if_match=generation(db), actor=ME, facts=None)
    assert store.snapshot().get("owner.test.demo").needs_configuration
    other = WorkflowFacts("default", "stable", "batch", "video")
    store.put_workflow(owner_contract(display_name="z"), if_match=generation(db), actor=ME, facts=other)
    assert store.snapshot().get("owner.test.demo").facts == other


def test_delete_then_create_gives_a_new_incarnation_and_the_history_holder_is_released(parts):  # rows 5, 16
    store, db, definitions, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    before = store.snapshot().get("owner.test.demo").incarnation
    assert definitions.holder_shas("registry:owner.test.demo")
    store.delete_workflow("owner.test.demo", if_match=generation(db), actor=ME)
    assert store.snapshot().get("owner.test.demo") is None
    assert definitions.holder_shas("registry:owner.test.demo") == ()
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    again = store.snapshot().get("owner.test.demo")
    assert again.incarnation != before and again.record_revision == 1


def test_a_bundled_id_is_refused_unless_it_says_it_overrides_and_cannot_be_deleted(parts):
    store, db, *_ = parts
    with pytest.raises(WorkflowIdIsBundled):
        store.put_workflow(owner_contract(BUNDLED_ID), if_match=None, actor=ME)
    with pytest.raises(WorkflowIdIsBundled):
        store.delete_workflow(BUNDLED_ID, if_match=generation(db), actor=ME)
    store.put_workflow(owner_contract(BUNDLED_ID, display_name="Mine"), if_match=None, actor=ME, overrides=BUNDLED_ID)
    assert store.snapshot().get(BUNDLED_ID).contract["display_name"] == "Mine"


def test_the_check_returns_every_problem_with_its_path_and_writes_nothing(parts):
    store, db, *_ = parts
    bad = owner_contract(intent=7)
    del bad["outputs"]
    seen = generation(db)
    report = store.check(bad, workflow_id=None, snapshot=store.snapshot())
    assert len(report.errors) >= 2 and all(isinstance(e.path, tuple) for e in report.errors)
    assert generation(db) == seen
    with pytest.raises(RegistryRefused) as raised:
        store.put_workflow(bad, if_match=None, actor=ME)
    assert raised.value.problems and generation(db) == seen


def test_a_workflow_whose_token_equals_another_workflows_is_refused(parts):  # rows 8, 22
    store, db, *_ = parts
    store.put_workflow(owner_contract("x.a-b"), if_match=None, actor=ME)
    with pytest.raises(RegistryRefused) as raised:
        store.put_workflow(owner_contract("x.a.b"), if_match=None, actor=ME)
    assert {p.code for p in raised.value.problems} == {ID_CLASH}


def test_a_workflow_whose_token_equals_a_model_alias_is_refused_and_its_own_names_are_not_a_clash(parts):  # row 8
    store, db, *_ = parts
    registry = service_registry.normalize_service_registry({})
    registry["services"] = {"svc": {"id": "svc", "name": "svc", "kind": "engine", "config": {"models": [
        {"id": "svc-model"}, {"id": "Owner-Test-Demo", "base_model_id": "svc-model"}]}}}
    try:
        write_service_registry_for_test(registry, path=db)
    except ValueError:
        pytest.skip("the minimal service record is not valid in this build")
    with pytest.raises(RegistryRefused) as raised:
        store.put_workflow(owner_contract(), if_match=None, actor=ME)
    assert ID_CLASH in {p.code for p in raised.value.problems}


def test_an_update_of_a_workflow_is_not_a_clash_with_its_own_names(parts):
    store, db, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    store.put_workflow(owner_contract(display_name="again"), if_match=generation(db), actor=ME)


def _template(nodes: int = 3, depth: int = 1, padding: int = 0) -> dict:
    inner: object = "x" * padding
    for _ in range(depth):
        inner = {"k": inner}
    return {str(i): {"inputs": inner} for i in range(nodes)}


def _with_inline(contract: dict, template: dict) -> dict:
    """The template carried in the binding's own template field (the description's ``kind: template``), not by file."""
    key = next(iter(contract["bindings"]))
    contract["bindings"][key] = {**contract["bindings"][key], "workflow": {"inline": template}}
    return contract


def bundled_template(workflow_id: str = BUNDLED_ID) -> dict:
    """The graph the bundled workflow names by file, to carry inline."""
    import json
    from gateway.discovery import ROOT
    block = next(iter(discovery.bundled_workflow_manifests()[workflow_id]["bindings"].values()))
    return json.loads((ROOT / block["workflow"]).read_text(encoding="utf-8"))


def test_each_bound_is_checked_with_its_path_and_code():  # row 9
    limits = Bounds(max_template_bytes=200, max_nodes=4, max_depth=5)
    contract = _with_inline(owner_contract(), _template(nodes=9, depth=9, padding=500))
    problems = bound_problems(contract, limits)
    assert [p.code for p in problems] == [BOUND_EXCEEDED] * 3
    assert all(p.path[-2:] == ("workflow", "inline") for p in problems)
    assert bound_problems(_with_inline(owner_contract(), _template(2, 1)), limits) == []
    assert template_depth({}) == 1 and template_depth(5) == 0 and template_depth({"a": [1, {"b": 2}]}) == 3


def test_the_bounds_and_history_depth_come_from_config_and_the_ceiling_wins():
    assert bounds_from_config({}) == Bounds()
    config = {"workflow_execution": {"bounds": {"max_nodes": 10, "max_depth": 10 ** 6, "max_template_bytes": "x"},
                                     "history_depth": 5}}
    assert bounds_from_config(config) == Bounds(max_nodes=10)
    assert history_depth_from_config(config) == 5
    assert history_depth_from_config({"workflow_execution": {"history_depth": 500}}) == 20


def test_two_saves_that_differ_only_in_annotations_are_two_history_rows_with_one_contract_digest(parts):  # row 21
    store, db, *_ = parts
    contract = owner_contract()
    key = next(iter(contract["inputs"]))
    first = copy.deepcopy(contract)
    first["inputs"][key]["annotations"] = {"example.org/note": "first"}
    second = copy.deepcopy(contract)
    second["inputs"][key]["annotations"] = {"example.org/note": "second"}
    a = store.put_workflow(first, if_match=None, actor=ME)
    b = store.put_workflow(second, if_match=generation(db), actor=ME)
    assert a.contract_digest == b.contract_digest and a.record_digest != b.record_digest
    rows = store.history("owner.test.demo")
    assert [r.record_revision for r in rows] == [2, 1] and rows[0].contract_digest == rows[1].contract_digest
    store.restore("owner.test.demo", 1, if_match=generation(db), actor=ME)
    assert store.snapshot().get("owner.test.demo").contract["inputs"][key]["annotations"] == {"example.org/note": "first"}


def test_restore_writes_a_new_revision_with_the_old_contract_facts_published_and_overrides(parts):  # row 11
    store, db, *_ = parts
    store.put_workflow(owner_contract(display_name="old"), if_match=None, actor=ME, facts=FACTS)
    store.set_published("owner.test.demo", True, if_match=generation(db), actor=ME)       # revision 2: published
    store.put_workflow(owner_contract(display_name="new"), if_match=generation(db), actor=ME, facts=None)
    store.set_published("owner.test.demo", False, if_match=generation(db), actor=ME)
    store.set_state("owner.test.demo", "disabled", if_match=generation(db), actor=ME)
    current = store.snapshot().get("owner.test.demo")
    commit = store.restore("owner.test.demo", 2, if_match=generation(db), actor=ME)
    restored = store.snapshot().get("owner.test.demo")
    assert commit.record_revision == current.record_revision + 1
    assert restored.contract["display_name"] == "old" and restored.facts == FACTS and restored.published is True
    assert (restored.state, restored.incarnation) == ("disabled", current.incarnation)
    with pytest.raises(RegistryRefused):
        store.restore("owner.test.demo", 99, if_match=generation(db), actor=ME)


def test_only_the_last_history_depth_changes_are_held(parts):
    store, db, definitions, config, _ = parts
    config["workflow_execution"] = {"history_depth": 3}
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    for n in range(5):
        store.put_workflow(owner_contract(display_name=f"v{n}"), if_match=generation(db), actor=ME)
    assert [r.record_revision for r in store.history("owner.test.demo", limit=50)] == [6, 5, 4]
    assert len(definitions.holder_shas("registry:owner.test.demo")) == 3
    assert len(store.history("owner.test.demo", limit=2)) == 2


def test_listeners_run_after_the_commit_with_old_and_new_and_one_that_raises_stops_nothing(parts):
    store, db, _, _, changes = parts
    seen = []
    store.subscribe(lambda old, new: (_ for _ in ()).throw(RuntimeError("boom")))
    off = store.subscribe(lambda old, new: seen.append((old.revision, new.revision)))
    before = generation(db)
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    assert seen == [(before, before + 1)] and changes == [1]
    off()
    store.put_workflow(owner_contract(display_name="n"), if_match=generation(db), actor=ME)
    assert len(seen) == 1


def test_a_failed_hot_reload_is_reported_with_the_stored_commit(parts):
    store, db, *_ = parts
    from gateway.workflow_registry_store import RegistryNotApplied

    def broken() -> None:
        raise RuntimeError("could not reload")

    failing = WorkflowRegistryStore(path=db, definitions=parts[2], build_snapshot=build_snapshot, on_changed=broken)
    with pytest.raises(RegistryNotApplied) as raised:
        failing.put_workflow(owner_contract(), if_match=None, actor=ME)
    assert raised.value.commit.workflow_id == "owner.test.demo"
    assert store.snapshot().get("owner.test.demo") is not None      # saved


def test_concurrent_creates_of_one_id_make_one_workflow(parts):
    store, db, *_ = parts
    results = []

    def create() -> None:
        try:
            store.put_workflow(owner_contract(), if_match=None, actor=ME)
            results.append("ok")
        except RegistryConflict as error:
            results.append(error.code)

    threads = [threading.Thread(target=create) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", *[WORKFLOW_EXISTS] * 3]


def test_every_bundled_contract_passes_the_save_pipeline(store):
    snapshot = store.snapshot()
    failures = {}
    for workflow_id, contract in discovery.bundled_workflow_manifests().items():
        report = store.check(contract, workflow_id=workflow_id, overrides=workflow_id, snapshot=snapshot)
        if report.errors:
            failures[workflow_id] = [(e.pointer, e.code) for e in report.errors][:3]
    assert failures == {}


# -- W1: history is part of the accepted change -------------------------------------------------------------------------


def _state(store, db, definitions):
    return generation(db), [r.record_revision for r in store.history("owner.test.demo", limit=50)], definitions.holder_shas("registry:owner.test.demo")


def test_a_history_write_that_fails_leaves_neither_the_change_nor_a_history_row(parts, monkeypatch):  # W1
    store, db, definitions, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    before = _state(store, db, definitions)

    def broken(*_args, **_kwargs):
        raise OSError("history store down")

    monkeypatch.setattr(definitions, "replace_holder", broken)
    with pytest.raises(OSError):
        store.put_workflow(owner_contract(display_name="two"), if_match=generation(db), actor=ME)
    monkeypatch.undo()
    assert _state(store, db, definitions) == before
    assert store.snapshot().get("owner.test.demo").contract["display_name"] != "two"


def test_a_registry_commit_that_fails_after_the_history_write_removes_that_history_row(parts, monkeypatch):  # W1
    store, db, definitions, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    before = _state(store, db, definitions)
    real = admin_store.write_service_registry_with_audit

    def failing_commit(*args, also_in_transaction=None, **kwargs):
        # the history write has run inside the transaction; the commit then fails
        def then_fail(conn, now):
            also_in_transaction(conn, now)
            raise RuntimeError("commit failed")
        return real(*args, also_in_transaction=then_fail, **kwargs)

    monkeypatch.setattr(admin_store, "write_service_registry_with_audit", failing_commit)
    with pytest.raises(RuntimeError):
        store.put_workflow(owner_contract(display_name="two"), if_match=generation(db), actor=ME)
    monkeypatch.undo()
    assert _state(store, db, definitions) == before


def test_a_history_row_left_by_a_crashed_commit_is_replaced_by_the_next_real_change(parts):  # W1
    store, db, definitions, *_ = parts
    store.put_workflow(owner_contract(), if_match=None, actor=ME)
    # a crash after the history write and before the registry commit leaves a row for revision 2 that never happened
    body = {"record_revision": 2, "record_digest": "sha256:orphan", "contract_digest": "sha256:orphan", "saved_at_s": 1,
            "actor": {"kind": "owner", "id": "owner-1", "label": "Owner"}, "change": "update",
            "record": {"manifest": {"display_name": "never"}}}
    sha = definitions.put_blob(canonical_bytes(body))
    definitions.replace_holder("registry:owner.test.demo", [sha, *definitions.holder_shas("registry:owner.test.demo")])
    store.put_workflow(owner_contract(display_name="real"), if_match=generation(db), actor=ME)
    rows = store.history("owner.test.demo", limit=50)
    assert [r.record_revision for r in rows] == [2, 1]
    assert store.restore("owner.test.demo", 2, if_match=generation(db), actor=ME)
    assert store.snapshot().get("owner.test.demo").contract["display_name"] == "real"
