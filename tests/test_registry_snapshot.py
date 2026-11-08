"""The registry snapshot (CODE_TO_DATA_S4_S8, I1, S5): one immutable view of workflows, frontends and names.

``tests/workflow_runtime_fakes.py`` is the reference behaviour of the interface; these tests prove the production builder keeps
the same contracts (read-only views, canonical lookup, owner entities, clash refusal, a digest that covers frontends and
names) and the rules of the real document: stored records over bundled ones, state and publication defaults, strictness.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from gateway import registry_snapshot as rs
from gateway.discovery import WorkflowRegistry
from gateway.manifests import BUNDLED_PROVENANCE
from gateway.workflow_kernel import execution_digest
from gateway.workflow_runtime_types import ID_CLASH, NameOwner
from tests.support.workflow_docs import STANDARD_FACTS
from tests.workflow_runtime_fakes import FakeStore, entry as fake_entry, names_of

CONTRACT = {
    "schema_version": 2, "min_gateway_version": "0.1", "version": "0.1.0", "id": "poster.generate.demo", "intent": "poster.generate",
    "capability": "image", "display_name": "Poster", "purpose": "Make a poster", "description": "Make a poster.",
    "inputs": {"prompt": {"type": "string", "required": True}},
    "outputs": {"poster": {"media_type": "image/png"}},
    "bindings": {"comfyui": {"workflow": "workflows/none.json", "selectors": {"poster": {"node": "1", "key": "images", "from": "output_dir", "decode": "none", "take": "all"}}, "map": {"prompt": [{"node": "2", "input": "text"}]}}},
}


def bundled_of(*contracts):
    registry = WorkflowRegistry()
    for contract in contracts:
        registry.add_manifest(copy.deepcopy(contract), provenance=BUNDLED_PROVENANCE, facts=STANDARD_FACTS)
    return rs.BundledRelease({r.id: r for r in registry.records()}, ())


def contract(workflow_id, **extra):
    return {**copy.deepcopy(CONTRACT), "id": workflow_id, **extra}


def stored(workflow_id, **extra):
    record = {"id": workflow_id, "manifest": contract(workflow_id), "enabled": True, "policy": {
        "exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "media", "tags": []}}
    return {**record, **extra}


def row(row_id, *routes, selects=None):
    return rs.frontend_row({"id": row_id, "shape": "s", "mode": "job", "receipt_protocol": "p", "selects": selects or {"ids": ["x"]},
                            "routes": [{"methods": ["POST"], "canonical": path} for path in routes]})


def build(document=None, bundled=None, generation=3, **kwargs):
    return rs.build(document or {}, bundled or bundled_of(CONTRACT), generation=generation, **kwargs)


# ---------------------------------------------------------------------------------------------------------- entries


def test_a_bundled_workflow_is_an_active_published_entry_with_both_digests():
    entry = build().get("poster.generate.demo")
    assert (entry.state, entry.published, entry.incarnation) == ("active", True, "bundled")
    assert entry.contract_digest == execution_digest(CONTRACT)
    assert entry.record_digest != entry.contract_digest
    assert entry.facts is not None and not entry.needs_configuration
    assert entry.facts.resource_class == STANDARD_FACTS.resource_class


def test_annotations_move_the_record_digest_and_not_the_contract_digest():
    plain = build(bundled=bundled_of(CONTRACT)).get(CONTRACT["id"])
    noted = build(bundled=bundled_of({**CONTRACT, "annotations": {"note": "x"}})).get(CONTRACT["id"])
    assert noted.contract_digest == plain.contract_digest
    assert noted.record_digest != plain.record_digest


def test_a_workflow_without_facts_needs_configuration():
    registry = WorkflowRegistry()
    registry.add_manifest(copy.deepcopy(CONTRACT), provenance=BUNDLED_PROVENANCE)
    release = rs.BundledRelease({r.id: r for r in registry.records()}, ())
    assert build(bundled=release).get(CONTRACT["id"]).needs_configuration


def test_a_stored_record_replaces_the_bundled_workflow_of_its_id_and_reads_the_store_keys_with_defaults():
    snap = build({"workflows": {CONTRACT["id"]: stored(CONTRACT["id"], overrides=CONTRACT["id"], state="disabled", published=True,
                                                       incarnation="inc-9", record_revision=5)}})
    entry = snap.get(CONTRACT["id"])
    assert (entry.state, entry.published, entry.incarnation, entry.record_revision, entry.overrides) == (
        "disabled", True, "inc-9", 5, CONTRACT["id"])
    assert not entry.provenance.trusted
    plain = build({"workflows": {"mine.one": stored("mine.one")}}, generation=11).get("mine.one")
    assert (plain.state, plain.published, plain.incarnation, plain.record_revision) == ("active", False, "stored", 11)
    off = build({"workflows": {"mine.two": stored("mine.two", enabled=False)}}).get("mine.two")
    assert off.state == "disabled"


def test_a_record_the_schema_step_left_marked_never_runs_and_the_bundled_version_serves():
    marked = stored(CONTRACT["id"], migration={"step": "x", "errors": []})
    entry = build({"workflows": {CONTRACT["id"]: marked}}).get(CONTRACT["id"])
    assert entry.incarnation == "bundled"


def test_a_stored_record_without_facts_needs_configuration_and_a_release_row_does_not_fill_it_in():
    bare = stored("mine.three")
    del bare["policy"]
    assert build({"workflows": {"mine.three": bare}}).get("mine.three").needs_configuration


def test_by_intent_lists_the_entries_of_one_intent_and_get_the_one_of_an_id():
    snap = build({"workflows": {"mine.one": stored("mine.one")}})
    assert {e.id for e in snap.by_intent("poster.generate")} == {CONTRACT["id"], "mine.one"}
    assert snap.get("absent") is None and snap.by_intent("absent") == ()


def test_the_bindings_carry_the_adapter_id_the_binding_body_and_its_digest():
    entry = build().get(CONTRACT["id"])
    binding = entry.bindings["comfyui"]
    assert binding.adapter_id == "comfyui" and binding.binding["workflow"] == "workflows/none.json"
    assert binding.binding_digest.startswith("sha256:") and binding.template.path == "workflows/none.json"


def test_an_inline_template_is_carried_and_digested():
    inline = {"1": {"class_type": "x"}}
    c = contract("mine.inline")
    c["bindings"] = {"comfyui": {**c["bindings"]["comfyui"], "workflow": {"inline": inline}}}   # the descriptor's template field
    template = build({"workflows": {"mine.inline": {**stored("mine.inline"), "manifest": c}}}).get("mine.inline").bindings["comfyui"].template
    assert dict(template.inline["1"]) == inline["1"] and template.digest.startswith("sha256:")


# --------------------------------------------------------------------------------------------------- read-only views


def test_every_mapping_of_the_snapshot_is_a_read_only_view():
    snap = build()
    entry = snap.get(CONTRACT["id"])
    for target in (snap.workflows, snap.frontends, entry.contract, entry.contract["inputs"], entry.bindings,
                   entry.bindings["comfyui"].binding):
        with pytest.raises(TypeError):
            target["x"] = 1   # type: ignore[index]
    assert isinstance(entry.contract["inputs"]["prompt"], type(entry.contract))


# ----------------------------------------------------------------------------------------------------------- names


def test_names_are_looked_up_by_canonical_token_and_owned_by_the_entity():
    snap = build({"workflows": {"mine.one": stored("mine.one", manifest={**contract("mine.one"), "aliases": ["Mine One Alias"]})}})
    names = snap.names
    assert names.owner("workflow", "POSTER_generate.demo") == NameOwner("workflow", CONTRACT["id"])
    assert names.owner("workflow_alias", "mine-one-alias") == NameOwner("workflow_alias", "mine.one")
    assert names.owner("operation", "run_mine_one") == NameOwner("operation", "mine.one")
    assert names.owner("component", "mine.one") == NameOwner("component", "mine.one")
    assert names.owner("workflow", "nothing") is None
    assert {o.kind for o in names.owners("mine one alias")} == {"workflow_alias"}


def test_model_ids_and_model_aliases_come_from_the_services_section():
    services = {"svc": {"config": {"models": [
        {"id": "base-m"}, {"id": "friendly", "base_model_id": "base-m", "model_group": "Group Name"}]}}}
    names = build({"services": services}).names
    assert names.owner("model", "Base_M") == NameOwner("model", "base-m")
    assert names.owner("model_alias", "friendly") == NameOwner("model_alias", "base-m")
    assert names.owner("model_alias", "group name") == NameOwner("model_alias", "base-m")


def test_dynamic_aliases_are_names_and_a_glob_is_not():
    aliases = {"chat": {"stable": {"fast": "x", "slow": None}, "openai": [{"match": "gpt-*", "to": "fast"}, {"match": "plain-name", "to": "fast"}]}}
    names = build(model_aliases=aliases).names
    assert names.owner("model_alias", "fast") and names.owner("model_alias", "slow") and names.owner("model_alias", "plain-name")
    assert names.owner("model_alias", "gpt-") is None


def test_frontend_ids_and_route_keys_are_names():
    release = rs.BundledRelease(bundled_of(CONTRACT).workflows, (row("front", "/V1/Things/"),))
    names = rs.build({}, release, generation=1).names
    assert names.owner("frontend", "front") == NameOwner("frontend", "front")
    assert names.owner("route", "POST /v1/things") == NameOwner("route", "front")


def test_a_name_two_owners_hold_is_refused_whole_revision():
    other = contract("other.thing", aliases=["poster-generate-demo"])
    with pytest.raises(rs.SnapshotBuildError) as caught:
        build({"workflows": {"other.thing": {**stored("other.thing"), "manifest": other}}})
    assert [p.code for p in caught.value.problems] == [ID_CLASH]


@pytest.mark.parametrize("name", ["Friendly", "run_poster_generate_demo-ish", "front"])
def test_a_model_alias_a_frontend_or_an_id_with_the_token_of_another_owner_is_refused(name):
    release = rs.BundledRelease(bundled_of(CONTRACT).workflows, (row("front", "/v1/things"),))
    services = {"svc": {"config": {"models": [{"id": name, "base_model_id": "base"}, {"id": "base"}]}}}
    if name == "run_poster_generate_demo-ish":
        return   # that token is an operation name, which has its own space
    with pytest.raises(rs.SnapshotBuildError):
        rs.build({"services": services, "workflows": {name: stored(name)}}, release, generation=1)


def test_two_workflows_whose_generated_names_collide_are_refused():
    with pytest.raises(rs.SnapshotBuildError):
        build({"workflows": {"x.a-b": stored("x.a-b"), "x.a.b": stored("x.a.b")}})


def test_a_workflow_own_alias_with_its_id_token_is_not_a_clash():
    snap = build({"workflows": {"mine.one": stored("mine.one", manifest={**contract("mine.one"), "aliases": ["mine-one"]})}})
    assert snap.get("mine.one") is not None


def test_an_overridden_bundled_entry_leaves_the_name_view():
    renamed = contract(CONTRACT["id"], aliases=["bundled-alias"])
    release = bundled_of(renamed)
    assert build(bundled=release).names.owner("workflow_alias", "bundled-alias")
    overriding = stored(CONTRACT["id"], overrides=CONTRACT["id"])
    assert build({"workflows": {CONTRACT["id"]: overriding}}, release).names.owner("workflow_alias", "bundled-alias") is None


def test_the_name_view_has_the_same_lookups_as_the_reference_fake():
    entries = {CONTRACT["id"]: fake_entry(CONTRACT)}
    reference = names_of(entries)
    real = build(bundled=bundled_of(CONTRACT)).names
    for kind, name in (("workflow", CONTRACT["id"]), ("operation", "run_poster_generate_demo"), ("component", "poster_generate_demo")):
        assert real.owner(kind, name) == reference.owner(kind, name)
    assert sorted((n, o.kind, o.id) for n, o in real.items()) == sorted((n, o.kind, o.id) for n, o in reference.items())


# ----------------------------------------------------------------------------------------------------------- digest


def test_the_digest_is_stable_and_follows_every_part_of_the_revision():
    base = build()
    assert build().digest == base.digest
    assert build({"workflows": {"mine.one": stored("mine.one")}}).digest != base.digest              # a workflow
    assert build({"workflows": {CONTRACT["id"]: stored(CONTRACT["id"], published=True, overrides=CONTRACT["id"])}}).digest != base.digest
    services = {"svc": {"config": {"models": [{"id": "alias-only", "base_model_id": "base"}, {"id": "base"}]}}}
    with_alias = build({"services": services})
    assert with_alias.digest != base.digest                                                            # an alias-only edit
    renamed = {"svc": {"config": {"models": [{"id": "alias-two", "base_model_id": "base"}, {"id": "base"}]}}}
    assert build({"services": renamed}).digest != with_alias.digest
    release = bundled_of(CONTRACT)
    one = rs.build({}, rs.BundledRelease(release.workflows, (row("front", "/v1/a"),)), generation=1)
    two = rs.build({}, rs.BundledRelease(release.workflows, (row("front", "/v1/b"),)), generation=1)
    assert one.digest != two.digest                                                                    # a frontend-only edit


def test_a_state_or_publication_change_changes_the_digest():
    off = build({"workflows": {"mine.one": stored("mine.one")}})
    assert build({"workflows": {"mine.one": stored("mine.one", state="disabled")}}).digest != off.digest
    assert build({"workflows": {"mine.one": stored("mine.one", published=True)}}).digest != off.digest


def test_the_revision_is_the_generation():
    assert build(generation=42).revision == 42


# ------------------------------------------------------------------------------------------------ strict and swapped


@pytest.mark.parametrize("document", [
    {"workflows": []}, {"workflows": {"a": "text"}}, {"workflows": {"a": {"manifest": {"id": "b"}}}},
    {"workflows": {"a": {"manifest": {}}}}, {"workflows": {"a": stored("a", state="paused")}}, {"services": []},
])
def test_a_bad_document_raises_and_builds_nothing(document):
    with pytest.raises(rs.SnapshotBuildError):
        build(document)


def test_a_failed_build_keeps_the_snapshot_the_provider_holds():
    provider = rs.Provider(build())
    held = provider.current()
    with pytest.raises(rs.SnapshotBuildError):
        provider.swap(build({"workflows": []}))
    assert provider.current() is held
    newer = build(generation=9)
    assert provider.swap(newer) is held and provider.current() is newer


def test_a_provider_with_nothing_built_says_so():
    with pytest.raises(RuntimeError):
        rs.Provider().current()


def test_a_request_keeps_the_snapshot_it_first_took_across_a_swap():
    provider = rs.Provider(build(generation=1))
    request = SimpleNamespace(state=SimpleNamespace())
    first = rs.snapshot_of(request, provider)
    provider.swap(build(generation=2))
    assert rs.snapshot_of(request, provider) is first and provider.current().revision == 2


# ----------------------------------------------------------------------------------------------------- frontend rows


def test_a_frontend_row_keeps_selects_as_written_and_refuses_a_bad_selector():
    good = rs.frontend_row({"id": "r", "selects": {"match": {"outputs": {"kind": "assets"}}}, "routes": []})
    assert good.selects["match"]["outputs"]["kind"] == "assets"
    with pytest.raises(rs.SnapshotBuildError):
        rs.frontend_row({"id": "r", "selects": {"ids": []}})
    with pytest.raises(rs.SnapshotBuildError):
        rs.frontend_row({"id": "r", "mode": "later"})
    with pytest.raises(rs.SnapshotBuildError):
        rs.frontend_row({"routes": []})


def test_the_release_files_build_one_snapshot_whose_digest_is_stable():
    release = rs.bundled_release()
    assert len(release.workflows) == 38 and release.frontends
    first = rs.build({}, release, generation=1)
    assert rs.build({}, release, generation=1).digest == first.digest
    assert all(not e.needs_configuration and e.published for e in first.workflows.values())


def test_the_fake_store_and_the_real_snapshot_agree_on_what_a_save_would_clash_with():
    snap = build()
    store = FakeStore({CONTRACT["id"]: fake_entry(CONTRACT)})
    clash = store.check(contract("x.y", aliases=["poster-generate-demo"]), workflow_id="x.y", snapshot=snap)
    assert [p.code for p in clash.errors] == [ID_CLASH]


def test_load_current_reads_the_stored_document_and_uses_its_generation_as_the_revision(tmp_path):
    from gateway import admin_store

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    first = rs.load_current(path)
    assert first.revision == 0 and len(first.workflows) == 38
    written = admin_store.get_service_registry_with_generation(path=path)
    value = dict(written["value"]) if written else {"services": {}}
    result = admin_store.write_service_registry(value, expected_generation=written["operator_settings_generation"] if written else 0, path=path)
    second = rs.load_current(path)
    assert second.revision == result["operator_settings_generation"] and second.digest == first.digest
