"""Exact decisions of gateway/category_policy.py, one cause at a time (mutation sweep round 2)."""

from __future__ import annotations

import pytest

from gateway import category_policy as cp
from gateway.category_policy import (
    Assertion, Category, Grant, Plan, PolicyVersion, Row, Service, Subject, Waiver,
    decision_for, evaluate, legacy_exact, legacy_gated,
)

CAT = Category("aud", "Audience", values=("kids", "adult"))
CATS = {"aud": CAT}


def fact(value="kids", **kw):
    base = dict(category="aud", value=value, source="owner", accepted=True, observed_at="t1",
                accepted_at="t2", accepting_owner="o", mapping_version="v1")
    base.update(kw)
    return Assertion(**base)


def svc(*facts, id="s@1", kind="release", root="r@1"):
    return Service(id, (Subject(root, kind, tuple(facts)),))


GRANT = {"capability": "image", "service": "s@1"}


def plan(id="p", grants=(GRANT,), **kw):
    return Plan(id, tuple(grants), **kw)


def run(service, *plans, app_plans=None, **kw):
    return evaluate(CATS, list(plans), list(plans) if app_plans is None else app_plans, service, "image", **kw)


def shape(d):
    return (d.outcome, d.reason, d.rule, d.category, d.member)


def test_an_allowed_request_has_the_allow_reason_and_no_rule():
    assert shape(run(svc(fact()), plan())) == ("allow", "ALLOW", "", "", "")


@pytest.mark.parametrize("kwargs", [
    dict(phase="nowhere"), dict(capability=""), dict(capability=5),
])
def test_a_bad_boundary_or_capability_fails_closed(kwargs):
    capability = kwargs.pop("capability", "image")
    d = evaluate(CATS, [plan()], [plan()], svc(fact()), capability, **kwargs)
    assert shape(d) == ("deny", "POLICY_UNAVAILABLE", "evaluation", "", "")


@pytest.mark.parametrize("kwargs", [dict(person=None), dict(person=""), dict(person=3), dict(app=None), dict(app=""), dict(app=3)])
def test_a_missing_identity_is_unavailable_not_unplanned(kwargs):
    assert shape(run(svc(fact()), plan(), **kwargs)) == ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")


def test_missing_or_empty_plan_lists_are_told_apart():
    s = svc(fact())
    assert shape(evaluate(CATS, None, [plan()], s, "image")) == ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")
    assert shape(evaluate(CATS, [plan()], None, s, "image")) == ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")
    assert shape(evaluate(CATS, [], [plan()], s, "image")) == ("deny", "NO_PLANS", "identity", "", "")
    assert shape(evaluate(CATS, [plan()], [], s, "image")) == ("deny", "NO_PLANS", "identity", "", "")


def test_completeness_flags_are_checked_before_anything_is_decided():
    s = svc(fact())
    assert shape(run(s, plan(), metadata_complete=False)) == ("deny", "METADATA_INCOMPLETE", "completeness", "", "")
    assert run(s, plan(), metadata_complete=1).reason == "POLICY_UNAVAILABLE"
    assert run(s, plan(), completeness_gate=1).reason == "POLICY_UNAVAILABLE"
    assert run(s, plan(), completeness_gate=False).reason == "POLICY_UNAVAILABLE"
    assert run(s, plan(), metadata_complete=False, completeness_gate=False).reason == "METADATA_INCOMPLETE"


def test_an_invalid_plan_or_service_fails_closed():
    assert run(svc(fact()), Plan("p", (GRANT,), require=(Row("r", "ghost", "exists"),))).reason == "POLICY_UNAVAILABLE"
    assert run(Service("plain", (Subject("r@1", "release"),)), plan()).reason == "POLICY_UNAVAILABLE"


def test_a_deny_row_names_plan_row_category_and_the_subject_that_matched():
    d = run(svc(fact("adult")), plan(deny=(Row("d1", "aud", "in", ("adult",)),)))
    assert shape(d) == ("deny", "EXPLICIT_DENY", "p:d1", "aud", "r@1")


def test_a_deny_row_with_unknown_input_denies_and_a_non_matching_one_does_not():
    unknown = run(svc(), plan(deny=(Row("d1", "aud", "in", ("adult",)),)))
    assert shape(unknown) == ("deny", "DENY_INPUT_UNKNOWN", "p:d1", "aud", "r@1")
    assert run(svc(fact("kids")), plan(deny=(Row("d1", "aud", "in", ("adult",)),))).outcome == "allow"
    assert shape(run(svc(), plan(deny=(Row("d1", "aud", "missing"),)))) == ("deny", "EXPLICIT_DENY", "p:d1", "aud", "r@1")


def test_a_deny_row_skips_subjects_its_category_does_not_apply_to():
    narrow = Category("aud", "Audience", values=("kids", "adult"), applies_to=("workflow",))
    d = evaluate({"aud": narrow}, [plan(deny=(Row("d1", "aud", "missing"),))], [plan()], svc(), "image")
    assert d.outcome == "allow"


def test_a_deny_on_a_later_subject_is_found_after_an_earlier_inapplicable_one():
    narrow = Category("aud", "Audience", values=("kids", "adult"), applies_to=("workflow",))
    service = Service("s@1", (Subject("r@1", "release"), Subject("w@1", "workflow", (fact("adult"),))))
    d = evaluate({"aud": narrow}, [plan(deny=(Row("d1", "aud", "in", ("adult",)),))], [plan()], service, "image")
    assert shape(d) == ("deny", "EXPLICIT_DENY", "p:d1", "aud", "w@1")


def test_every_plan_must_grant_the_exact_capability_service_and_root():
    s = svc(fact())
    assert shape(run(s, plan(grants=({"capability": "image", "service": "other@1"},)))) == ("deny", "NO_GRANT", "p:grant", "", "")
    assert run(s, plan(grants=({"capability": "video", "service": "s@1"},))).reason == "NO_GRANT"
    assert run(s, plan(grants=())).reason == "NO_GRANT"
    assert run(s, plan(grants=({**GRANT, "release": "r@1"},))).outcome == "allow"
    assert run(s, plan(grants=({**GRANT, "release": "r@2"},))).reason == "NO_GRANT"
    assert run(s, plan(grants=({**GRANT, "workflow": "r@1"},))).reason == "NO_GRANT"
    assert run(s, plan(grants=(Grant("image", "s@1", release="r@1"),))).outcome == "allow"
    assert run(s, plan(grants=({"capability": "video", "service": "s@1"}, GRANT))).outcome == "allow"
    workflow_root = svc(fact(), kind="workflow", root="w@1")
    assert run(workflow_root, plan(grants=({**GRANT, "workflow": "w@1"},))).outcome == "allow"
    assert run(workflow_root, plan(grants=({**GRANT, "release": "w@1"},))).reason == "NO_GRANT"
    second = plan("q", grants=({"capability": "x", "service": "s@1"},))
    assert shape(run(s, plan(), second)) == ("deny", "NO_GRANT", "q:grant", "", "")


def test_a_legacy_plan_follows_the_gated_tag_rules_and_prefixes_the_plan_id():
    s = svc(fact())
    legacy = Plan("L", (GRANT,), mode="legacy", legacy_tags=("a",))
    assert shape(run(s, legacy, tags=("b",))) == ("deny", "LEGACY_TAG_MISMATCH", "L:legacy:tags", "", "")
    assert run(s, legacy, tags=("a",)).outcome == "allow"
    assert shape(run(s, legacy, tags=())) == ("deny", "METADATA_INCOMPLETE", "L:completeness", "", "")
    assert shape(run(s, legacy, tags=None)) == ("deny", "METADATA_INCOMPLETE", "L:completeness", "", "")


def test_a_requirement_reports_no_subject_missing_or_mismatching_values():
    narrow = Category("aud", "Audience", values=("kids", "adult"), applies_to=("workflow",))
    none = evaluate({"aud": narrow}, [plan(require=(Row("q1", "aud", "exists"),))], [plan()], svc(), "image")
    assert shape(none) == ("deny", "NO_APPLICABLE_SUBJECT", "p:q1", "aud", "")
    need = plan(require=(Row("q1", "aud", "in", ("kids",)),))
    assert shape(run(svc(), need)) == ("deny", "REQUIRE_MISSING", "p:q1", "aud", "r@1")
    assert shape(run(svc(fact("adult")), need)) == ("deny", "REQUIRE_MISMATCH", "p:q1", "aud", "r@1")
    assert run(svc(fact("kids")), need).outcome == "allow"
    assert run(svc(fact("kids")), plan(require=(Row("q1", "aud", "exists"),))).outcome == "allow"
    assert shape(run(svc(fact("kids")), plan(require=(Row("q1", "aud", "missing"),)))) == (
        "deny", "REQUIRE_MISMATCH", "p:q1", "aud", "r@1")
    assert run(svc(), plan(require=(Row("q1", "aud", "missing"),))).outcome == "allow"


def test_every_subject_must_satisfy_a_requirement_not_just_the_first():
    both = Service("s@1", (Subject("r@1", "release", (fact("kids"),)), Subject("x@1", "route", (fact("adult"),))))
    d = run(both, plan(require=(Row("q1", "aud", "in", ("kids",)),)))
    assert shape(d) == ("deny", "REQUIRE_MISMATCH", "p:q1", "aud", "x@1")
    mixed = Service("s@1", (Subject("r@1", "release", (fact("kids"), fact("adult"))),))
    assert run(mixed, plan(require=(Row("q1", "aud", "in", ("kids",)),))).outcome == "deny"
    assert run(mixed, plan(require=(Row("q1", "aud", "in", ("kids", "adult")),))).outcome == "allow"


def test_a_waiver_allows_a_missing_value_once_per_subject_and_is_reported():
    waiving = plan(require=(Row("q1", "aud", "in", ("kids",)),), missing={"q1": "allow"})
    allowed = run(svc(), waiving, app_plans=[plan()])
    assert allowed.outcome == "allow" and allowed.waivers == (("p", "q1", "r@1"),)
    assert shape(run(svc(fact("adult")), waiving, app_plans=[plan()])) == ("deny", "REQUIRE_MISMATCH", "p:q1", "aud", "r@1")
    two = Service("s@1", (Subject("r@1", "release"), Subject("x@1", "route")))
    assert run(two, waiving, app_plans=[plan()]).waivers == (("p", "q1", "r@1"), ("p", "q1", "x@1"))
    later = plan("p2", require=(Row("q2", "aud", "in", ("kids",)),))
    refused = run(svc(), waiving, later, app_plans=[plan()])
    assert refused.reason == "REQUIRE_MISSING" and refused.waivers == (("p", "q1", "r@1"),)
    ungranted = plan("p3", grants=({"capability": "x", "service": "s@1"},))
    assert run(svc(), waiving, ungranted, app_plans=[plan()]).waivers == (("p", "q1", "r@1"),)


def test_legacy_stage_helpers_decide_exactly():
    assert shape(legacy_exact(["a"], ["a"], grants=1)) == ("deny", "POLICY_UNAVAILABLE", "evaluation", "", "")
    assert shape(legacy_exact(["a"], ["a"], grants=False)) == ("deny", "NO_GRANT", "legacy:grant", "", "")
    assert legacy_exact([], ["z"]).outcome == "allow"
    assert legacy_exact(["a"], []).outcome == "allow"
    assert legacy_exact(["a"], None).outcome == "allow"
    assert legacy_exact(["a"], ["a", "b"]).outcome == "allow"
    assert shape(legacy_exact(["a"], ["b"])) == ("deny", "LEGACY_TAG_MISMATCH", "legacy:tags", "", "")
    assert legacy_gated(["a"], ["a"], metadata_complete=1).reason == "POLICY_UNAVAILABLE"
    assert legacy_gated(["a"], ["a"], grants=1).reason == "POLICY_UNAVAILABLE"
    assert shape(legacy_gated(["a"], ["a"], metadata_complete=False)) == ("deny", "METADATA_INCOMPLETE", "completeness", "", "")
    assert shape(legacy_gated(["a"], [])) == ("deny", "METADATA_INCOMPLETE", "completeness", "", "")
    assert legacy_gated(["a"], ["a"]).outcome == "allow"
    assert legacy_gated(["a"], ["a"], grants=False).rule == "legacy:grant"


# --- versions, boundaries and previews -------------------------------------------------------

def version(*facts, retained=None, gate=True, vid="rules"):
    s = svc(*facts)
    return PolicyVersion(vid, CATS, {"mia": (plan(),)}, {"app": (plan(),)}, {"s@1": s},
                         gate, retained if retained is not None else {})


def req(**kw):
    base = {"person": "mia", "app": "app", "capability": "image", "service": "s@1"}
    base.update(kw)
    return base


def test_a_request_is_decided_against_the_named_version():
    v = version(fact())
    assert decision_for(v, req()).outcome == "allow"
    assert shape(decision_for(None, req())) == ("deny", "POLICY_UNAVAILABLE", "snapshot", "", "")
    for bad in (dict(person=None), dict(person=""), dict(app=None), dict(app=""), dict(person="ghost"), dict(app="ghost")):
        assert shape(decision_for(v, req(**bad))) == ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")
    assert decision_for(v, req(service="ghost")).reason == "POLICY_UNAVAILABLE"
    assert decision_for(version(fact(), vid=""), req()).reason == "POLICY_UNAVAILABLE"
    assert decision_for(version(fact(), vid=3), req()).reason == "POLICY_UNAVAILABLE"
    mismatched = PolicyVersion("rules", CATS, {"mia": (plan(),)}, {"app": (plan(),)}, {"other@1": svc(fact())})
    assert decision_for(mismatched, req(service="other@1")).reason == "POLICY_UNAVAILABLE"


def test_the_completeness_gate_of_the_version_and_the_metadata_flag_of_the_request_both_apply():
    assert decision_for(version(fact(), gate=False), req()).reason == "POLICY_UNAVAILABLE"
    assert decision_for(version(fact()), req(metadata_complete=False)).reason == "METADATA_INCOMPLETE"
    legacy = Plan("L", (GRANT,), mode="legacy", legacy_tags=("a",))
    v = PolicyVersion("rules", CATS, {"mia": (legacy,)}, {"app": (legacy,)}, {"s@1": svc(fact())})
    assert decision_for(v, req(tags=["a"])).outcome == "allow"
    assert decision_for(v, req(tags=["b"])).reason == "LEGACY_TAG_MISMATCH"


@pytest.mark.parametrize("phase", ["release", "retrieval", "download", "share"])
def test_an_output_boundary_decides_against_the_recorded_bundle_not_the_live_service(phase):
    kept = Service("s@1", (Subject("r@1", "release", (fact("adult"),)),))
    v = version(fact("kids"), retained={"out1": kept})
    deny_adult = Plan("p", (GRANT,), deny=(Row("d", "aud", "in", ("adult",)),))
    v = PolicyVersion("rules", CATS, {"mia": (deny_adult,)}, {"app": (deny_adult,)}, {"s@1": svc(fact("kids"))}, True, {"out1": kept})
    out = decision_for(v, {"person": "mia", "app": "app", "capability": "image", "output": "out1", "phase": phase})
    assert shape(out) == ("deny", "EXPLICIT_DENY", "p:d", "aud", "r@1")
    live = decision_for(v, req(phase="dispatch"))
    assert live.outcome == "allow"
    assert decision_for(v, {"person": "mia", "app": "app", "capability": "image", "output": "ghost", "phase": phase}).reason == "POLICY_UNAVAILABLE"


@pytest.mark.parametrize("phase", ["action", "helper", "retry"])
def test_the_other_boundaries_decide_against_the_live_service(phase):
    v = version(fact())
    assert decision_for(v, req(phase=phase)).outcome == "allow"


def test_the_preview_inventory_covers_every_identity_service_and_boundary_once():
    kept = svc(fact(), id="s@1")
    a = PolicyVersion("rules", CATS, {"mia": (plan(),)}, {"app": (plan(),)}, {"s@1": svc(fact())}, True, {"out1": kept})
    explicit = [req(phase="dispatch", tags=["t"])]
    inventory = cp.preview_inventory(a, None, explicit)
    assert inventory[0] == explicit[0]
    live = [r for r in inventory[1:] if "service" in r]
    assert sorted(r.get("phase", "dispatch") for r in live) == ["action", "cache", "dispatch", "helper", "retry"]
    assert req() in inventory and all("phase" not in r for r in inventory if r == req())
    out = [r for r in inventory if "output" in r]
    assert sorted(r["phase"] for r in out) == ["download", "release", "retrieval", "share"]
    assert len(inventory) == 1 + 5 + 4 and cp.preview_inventory(None, None) == []
    assert cp.preview_inventory(a, a).count(req()) == 1
    only_grants = PolicyVersion("r", CATS, {"mia": (plan(grants=({"capability": "", "service": "s@1"}, {"service": "s@1"}, "x")),)}, {"app": ()}, {"s@1": svc()})
    assert cp.preview_inventory(only_grants, None) == []


def test_a_change_preview_lists_gainers_losers_and_changed_reasons_for_its_scope():
    allow = version(fact("kids"))
    denied = PolicyVersion("rules", CATS, {"mia": (plan(deny=(Row("d", "aud", "in", ("kids",)),)),)},
                           {"app": (plan(),)}, {"s@1": svc(fact("kids"))})
    losing = cp.change_preview(allow, denied)
    assert [r["service"] for r in losing["losses"]] == ["s@1"] * 5 and losing["gains"] == []
    assert len(losing["scope"]) == 5
    assert losing["reason_changes"][0]["before"].reason == "ALLOW" and losing["reason_changes"][0]["after"].reason == "EXPLICIT_DENY"
    gaining = cp.change_preview(denied, allow)
    assert len(gaining["gains"]) == 5 and gaining["losses"] == []
    same = cp.change_preview(allow, allow)
    assert same["gains"] == same["losses"] == same["reason_changes"] == []
    first = losing["losses"][0]
    first["service"] = "mutated"
    assert losing["scope"][0]["service"] == "s@1"          # results are copies, not the scope's own dicts
    other_member = PolicyVersion("rules", CATS, {"mia": (plan(deny=(Row("d", "aud", "in", ("kids",)),)),)},
                                 {"app": (plan(),)}, {"s@1": svc(fact("kids"), root="r@2")})
    changed = cp.change_preview(denied, other_member)
    assert changed["gains"] == changed["losses"] == [] and len(changed["reason_changes"]) == 5


def test_migration_comparison_runs_both_legacy_stages_and_the_new_rules():
    v = version(fact())
    result = cp.migration_comparison(["a"], req(tags=["b"], metadata_complete=True), v)
    assert set(result) == {"legacy_exact", "legacy_gated", "v3"}
    assert result["legacy_exact"].reason == "LEGACY_TAG_MISMATCH" and result["v3"].outcome == "allow"
    incomplete = cp.migration_comparison(["a"], req(tags=["a"], metadata_complete=False), v)
    assert incomplete["legacy_gated"].reason == "METADATA_INCOMPLETE" and incomplete["legacy_exact"].outcome == "allow"
    nogrant = cp.migration_comparison(["a"], req(tags=["a"]), v, legacy_grants=False)
    assert nogrant["legacy_exact"].rule == "legacy:grant" and nogrant["legacy_gated"].rule == "legacy:grant"
    assert cp.migration_comparison([], req(), None)["v3"].rule == "snapshot"


def test_preview_as_covers_each_known_service_or_recorded_output():
    kept = svc(fact(), id="s@1")
    v = PolicyVersion("rules", CATS, {"mia": (plan(),)}, {"app": (plan(),)}, {"s@1": svc(fact())}, True, {"out1": kept})
    assert {k: d.outcome for k, d in cp.preview_as(v, "mia", "app", "image").items()} == {"s@1": "allow"}
    assert {k: d.outcome for k, d in cp.preview_as(v, "mia", "app", "image", phase="share").items()} == {"out1": "allow"}
    assert cp.preview_as(None, "mia", "app", "image") == {}
    tagged = Plan("L", (GRANT,), mode="legacy", legacy_tags=("a",))
    lv = PolicyVersion("rules", CATS, {"mia": (tagged,)}, {"app": (tagged,)}, {"s@1": svc(fact())})
    assert cp.preview_as(lv, "mia", "app", "image", tags=["a"])["s@1"].outcome == "allow"
    assert cp.preview_as(lv, "mia", "app", "image", tags=["b"])["s@1"].reason == "LEGACY_TAG_MISMATCH"
    assert cp.preview_as(lv, "mia", "app", "image", metadata_complete=False)["s@1"].reason == "METADATA_INCOMPLETE"


def test_the_defaults_registry_composes_overrides_and_deletions_without_sharing_state():
    base = {"a": {"observed_at": "2026-01-01"}, "b": {"observed_at": "2026-06-01"}, "c": {"x": 1}}
    reg = cp.Registry(base, {"b": {"observed_at": "2026-02-01"}}, ["c"])
    assert reg.effective() == {"a": {"observed_at": "2026-01-01"}, "b": {"observed_at": "2026-02-01"}}
    reg.defaults["a"]["observed_at"] = "tampered"
    assert reg.defaults["a"]["observed_at"] == "2026-01-01"
    reg.reset("b")
    assert reg.effective()["b"] == {"observed_at": "2026-06-01"}
    reg.undelete("c")
    assert "c" in reg.effective()
    reg.delete("a")
    assert "a" not in reg.effective()
    reg.update_defaults({"z": {"observed_at": "2026-03-03"}})
    assert reg.effective() == {"z": {"observed_at": "2026-03-03"}}
    assert cp.Registry({"k": 1}).effective() == {"k": 1} and cp.Registry({"k": 1}).overrides == {}


@pytest.mark.parametrize("today,stale", [("2026-01-01", False), ("2026-01-02", True), ("2025-12-31", False)])
def test_a_record_is_stale_only_when_observed_strictly_before_today(today, stale):
    assert cp.Registry({"k": {"observed_at": "2026-01-01"}, "n": {"observed_at": ""}, "s": "text"}).tick(today) == {
        "k": stale, "n": False, "s": False}


def test_a_service_catalog_lists_every_revision_and_refuses_two_closures_of_one_revision():
    dep = Service("d@1", (Subject("d@1", "release"),))
    top = Service("s@1", (Subject("r@1", "release"),), (dep,))
    v = PolicyVersion("rules", CATS, {}, {}, {"s@1": top}, True, {"o@1": Service("o@1", (Subject("o@1", "release"),))})
    assert sorted(cp.service_catalog(v)) == ["d@1", "o@1", "s@1"]
    clash = Service("d@1", (Subject("d@1", "release"), Subject("e@1", "route")))
    bad = PolicyVersion("rules", CATS, {}, {}, {"s@1": top}, True, {"d@1": clash})
    with pytest.raises(cp.PolicyError) as raised:
        cp.service_catalog(bad)
    assert str(raised.value) == "one service revision has conflicting closures"
    chain = Service("a@1", (Subject("a@1", "release"),), (Service("b@1", (Subject("b@1", "release"),), (Service("c@1", (Subject("c@1", "release"),)),)),))
    assert sorted(cp.service_catalog(PolicyVersion("r", CATS, {}, {}, {"a@1": chain}))) == ["a@1", "b@1", "c@1"]


def test_validate_snapshot_refuses_what_a_runtime_could_not_enforce():
    ok = version(fact())
    cp.validate_snapshot(ok)
    for bad, message in (
        (PolicyVersion("", CATS, {}, {}, {}), "unsafe snapshot"),
        (PolicyVersion(3, CATS, {}, {}, {}), "unsafe snapshot"),
        (PolicyVersion("r", CATS, {}, {}, {}, completeness_gate=False), "unsafe snapshot"),
        (PolicyVersion("r", CATS, {}, {}, {"a@1": svc(id="a@1"), "a@2": svc(id="a@2")}), "one active revision per service; keep older revisions in retained"),
        (PolicyVersion("r", CATS, {}, {}, {"wrong@1": svc(id="s@1")}), "service key mismatch"),
        (PolicyVersion("r", CATS, {}, {}, {}, retained={"x@1": Service("x@1", (Subject("plain", "release"),))}), "unsupported or unpinned subject"),
    ):
        with pytest.raises(cp.PolicyError) as raised:
            cp.validate_snapshot(bad)
        assert str(raised.value) == message
    two_stems = PolicyVersion("r", CATS, {}, {}, {"a@1": svc(id="a@1", root="r@1"), "b@1": svc(id="b@1", root="r@9")})
    cp.validate_snapshot(two_stems)
    sharing = PolicyVersion("r", CATS, {}, {}, {"a@1": svc(id="a@1")}, retained={"o@1": svc(id="o@1", root="r@9", kind="workflow")})
    cp.validate_snapshot(sharing)
    bad_retained_fact = PolicyVersion("r", CATS, {}, {}, {}, retained={"o@1": svc(fact("ghost"), id="o@1")})
    with pytest.raises(cp.PolicyError, match=r"\Ainvalid accepted value\Z"):
        cp.validate_snapshot(bad_retained_fact)
    bad_live_fact = PolicyVersion("r", CATS, {}, {}, {"a@1": svc(fact("ghost"), id="a@1")})
    with pytest.raises(cp.PolicyError, match=r"\Ainvalid accepted value\Z"):
        cp.validate_snapshot(bad_live_fact)


def test_frozen_maps_and_grants_behave_like_read_only_mappings():
    m = cp._FrozenMap({"a": 1, "b": 2})
    assert len(m) == 2 and list(m) == ["a", "b"] and m["b"] == 2 and dict(m) == {"a": 1, "b": 2}
    with pytest.raises(KeyError):
        m["c"]
    g = Grant("image", "s@1")
    assert list(g) == ["capability", "service"] and len(g) == 2 and g["service"] == "s@1"
    assert list(Grant("image", "s@1", "r@1", "w@1")) == ["capability", "service", "release", "workflow"]
    with pytest.raises(KeyError):
        g["release"]
    with pytest.raises(KeyError):
        g["other"]
    assert cp.deny("R").rule == "evaluation" and cp.deny("R", "x", "c", "m").member == "m"


def test_boundaries_kinds_and_decision_defaults_are_the_documented_vocabulary():
    assert cp.BOUNDARIES == {"dispatch", "action", "helper", "retry", "release", "retrieval", "download", "share", "cache"}
    assert cp.KINDS == {"release", "route", "workflow"}
    assert cp.Decision("allow", "ALLOW") == cp.Decision("allow", "ALLOW", "", "", "", ())
    assert cp.deny("R") == cp.Decision("deny", "R", "evaluation", "", "", ())
    assert cp.deny("R", waivers=[("p", "q", "s")]).waivers == (("p", "q", "s"),)


def test_a_policy_version_copies_and_freezes_its_catalogs_so_later_edits_to_the_inputs_do_not_leak_in():
    categories, services, retained = dict(CATS), {"s@1": svc(fact())}, {"o@1": svc(fact(), id="o@1")}
    v = PolicyVersion("rules", categories, {"mia": [plan()]}, {"app": [plan()]}, services, True, retained)
    categories["extra"] = CAT
    services["t@1"] = svc(id="t@1")
    retained["p@1"] = svc(id="p@1")
    assert set(v.categories) == {"aud"} and set(v.services) == {"s@1"} and set(v.retained) == {"o@1"}
    assert all(isinstance(getattr(v, n), cp._FrozenMap) for n in ("categories", "services", "retained", "people", "apps"))
    assert isinstance(v.people["mia"], tuple)
    with pytest.raises(TypeError):
        v.services["t@1"] = svc(id="t@1")


def test_a_reason_change_carries_a_copy_of_the_request_that_changed():
    allow = version(fact("kids"))
    denied = PolicyVersion("rules", CATS, {"mia": (plan(deny=(Row("d", "aud", "in", ("kids",)),)),)},
                           {"app": (plan(),)}, {"s@1": svc(fact("kids"))})
    change = cp.change_preview(allow, denied)["reason_changes"][0]
    assert set(change) == {"request", "before", "after"} and change["request"]["person"] == "mia"


def test_two_services_whose_ids_share_a_prefix_up_to_an_inner_at_sign_are_different_services():
    v = PolicyVersion("r", CATS, {}, {}, {"team@a@1": svc(id="team@a@1"), "team@b@1": svc(id="team@b@1", root="r@2")})
    cp.validate_snapshot(v)


@pytest.mark.parametrize("phase", ["release", "retrieval", "download", "share"])
def test_preview_as_reads_the_retained_bundles_for_every_output_phase(phase):
    kept = svc(fact(), id="s@1")
    v = PolicyVersion("rules", CATS, {"mia": (plan(),)}, {"app": (plan(),)}, {"s@1": svc(fact())}, True, {"out1": kept})
    assert list(cp.preview_as(v, "mia", "app", "image", phase=phase)) == ["out1"]
    assert list(cp.preview_as(v, "mia", "app", "image", phase="action")) == ["s@1"]


def test_preview_as_passes_the_metadata_flag_to_the_decision():
    v = version(fact())
    assert cp.preview_as(v, "mia", "app", "image", metadata_complete=False)["s@1"].reason == "METADATA_INCOMPLETE"
    assert cp.preview_as(v, "mia", "app", "image")["s@1"].outcome == "allow"


@pytest.mark.parametrize("who", [dict(person=""), dict(person=3), dict(app=""), dict(app=3)])
def test_an_unusable_identity_is_unavailable_before_the_service_is_looked_up(who):
    """Kills category_policy.py:483 ``or`` -> ``and`` (all three).

    evaluate() repeats the identity guard, so a bad identity with a good service gives the same
    decision either way. With an unknown service the order shows: the guard at :483 answers
    IDENTITY_UNAVAILABLE, but without it the service lookup at :489 fails first (POLICY_UNAVAILABLE).
    PolicyVersion does not check its identity keys, so "" and 3 are given keys to get past :485.
    """
    v = PolicyVersion("rules", CATS, {"mia": (plan(),), "": (plan(),), 3: (plan(),)},
                      {"app": (plan(),), "": (plan(),), 3: (plan(),)}, {"s@1": svc(fact())})
    assert decision_for(v, req()).outcome == "allow"
    expected = ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")
    assert shape(decision_for(v, req(service="ghost", **who))) == expected
    assert shape(decision_for(v, req(**who))) == expected
    assert decision_for(v, req(service="ghost")).reason == "POLICY_UNAVAILABLE"
