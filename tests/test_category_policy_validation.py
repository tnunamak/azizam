"""Every rejection of gateway/category_policy.py, one invalid fault at a time, with its exact message.

A snapshot that should be refused and is accepted lets a rule be skipped. Each case breaks exactly
one condition, so a validator that checks `and` where it must check `or` fails.
"""

from __future__ import annotations

import pytest

from gateway import category_policy as cp
from gateway.category_policy import (
    Assertion, Category, Grant, Plan, PolicyError, Row, Service, Subject, accept_evidence, pinned, resolve,
)

CAT = Category("aud", "Audience", values=("kids", "adult"))
CATS = {"aud": CAT}


def fact(**kw):
    base = dict(category="aud", value="kids", source="owner", accepted=True, observed_at="t1",
                accepted_at="t2", accepting_owner="o", mapping_version="v1")
    base.update(kw)
    return Assertion(**base)


def rel(id="r@1", kind="release", *assertions):
    return Subject(id, kind, tuple(assertions))


@pytest.mark.parametrize("identifier,expected", [
    ("a@1", True), ("a@b@1", True), ("@x@1", True), ("x@1@", False), ("@1", False), ("a@", False), ("plain", False), ("sha256:abcdefg", True),
    ("sha256:a", True), ("sha256:", False), ("sha256", False), (None, False), (7, False),
])
def test_what_counts_as_a_pinned_revision(identifier, expected):
    assert pinned(identifier) is expected


@pytest.mark.parametrize("category,message", [
    (Category("a", "A", kind="range"), "A14: only set categories are supported"),
    (Category("", "A"), "incomplete category"),
    (Category(5, "A"), "incomplete category"),
    (Category("a", ""), "incomplete category"),
    (Category("a", 5), "incomplete category"),
    (Category("a", "A", applies_to=()), "incomplete category"),
    (Category("a", "A", applies_to=("release", "ghost")), "unsupported subject kind"),
    (Category("a", "A", values=("x", "")), "category needs unique value IDs"),
    (Category("a", "A", values=("x", 3)), "category needs unique value IDs"),
    (Category("a", "A", values=("x", "x")), "category needs unique value IDs"),
])
def test_a_malformed_category_is_refused(category, message):
    with pytest.raises(PolicyError) as raised:
        category.validate()
    assert str(raised.value) == message


def test_a_category_accepts_only_its_listed_string_values_and_applies_to_its_kinds():
    narrow = Category("a", "A", values=("x",), applies_to=("release",))
    narrow.validate()
    assert narrow.accepts("x") and not narrow.accepts("y") and not narrow.accepts(1)
    assert narrow.applies(Subject("r@1", "release")) and not narrow.applies(Subject("w@1", "workflow"))
    assert Category("a", "A").applies_to == ("release", "route", "workflow")
    assert isinstance(Category("a", "A", values=["x"]).values, tuple)
    assert isinstance(Category("a", "A", applies_to=["route"]).applies_to, tuple)


@pytest.mark.parametrize("kwargs,message", [
    (dict(actor="", owner=""), "owner action required"),
    (dict(actor="x", owner="o"), "owner action required"),
    (dict(actor=7, owner=7), "owner action required"),
    (dict(observed_at=""), "complete provenance required"),
    (dict(accepted_at=""), "complete provenance required"),
    (dict(mapping_version=""), "complete provenance required"),
])
def test_accepting_evidence_needs_the_owner_and_complete_provenance(kwargs, message):
    args = dict(actor="o", owner="o", observed_at="t1", accepted_at="t2", mapping_version="v1")
    args.update(kwargs)
    with pytest.raises(PolicyError) as raised:
        accept_evidence(Assertion("aud", "kids", "owner"), **args)
    assert str(raised.value) == message
    with pytest.raises(PolicyError, match=r"\Acomplete provenance required\Z"):
        accept_evidence(Assertion("aud", "kids", ""), actor="o", owner="o", observed_at="t1",
                        accepted_at="t2", mapping_version="v1")


def test_outside_evidence_needs_an_explicit_owner_mapping_and_owner_evidence_does_not():
    with pytest.raises(PolicyError) as raised:
        accept_evidence(Assertion("aud", "x", "scanner"), actor="o", owner="o", observed_at="t1",
                        accepted_at="t2", mapping_version="v1")
    assert str(raised.value) == "outside evidence requires explicit owner mapping"
    mapped = accept_evidence(Assertion("aud", "x", "scanner"), actor="o", owner="o", observed_at="t1",
                             accepted_at="t2", mapping_version="v1", mapped_value="kids")
    assert (mapped.value, mapped.accepted, mapped.accepting_owner, mapped.observed_at, mapped.accepted_at,
            mapped.mapping_version) == ("kids", True, "o", "t1", "t2", "v1")
    own = accept_evidence(Assertion("aud", "kids", "owner"), actor="o", owner="o", observed_at="t1",
                          accepted_at="t2", mapping_version="v1")
    assert own.value == "kids" and own.accepted


def test_the_defaults_of_an_assertion_and_a_service_and_a_plan_are_inert_and_strict():
    assert Assertion("c", "v", "s") == Assertion("c", "v", "s", False, "", "", "", "")
    assert Service("s@1", (rel(),)).checkable is True and Service("s@1", (rel(),)).dependencies == ()
    plan = Plan("p")
    assert (plan.mode, plan.grants, plan.require, plan.deny, dict(plan.missing), plan.legacy_tags) == ("v3", (), (), (), {}, ())
    assert isinstance(Plan("p", grants=[{"capability": "c", "service": "s@1"}]).grants[0], cp._FrozenMap)
    assert isinstance(Plan("p", require=[Row("r", "aud", "exists")]).require, tuple)
    assert isinstance(Plan("p", deny=[Row("r", "aud", "missing")]).deny, tuple)
    assert isinstance(Plan("p", legacy_tags=["t"]).legacy_tags, tuple)
    assert isinstance(Subject("r@1", "release", [fact()]).assertions, tuple)
    assert isinstance(Service("s@1", [rel()], [Service("d@1", (rel("d@1"),))]).members, tuple)
    assert isinstance(Service("s@1", (rel(),), [Service("d@1", (rel("d@1"),))]).dependencies, tuple)
    assert isinstance(Row("r", "aud", "in", ["kids"]).values, tuple)


def _svc(id="s@1", members=None, deps=(), checkable=True):
    return Service(id, members if members is not None else (rel(),), tuple(deps), checkable)


@pytest.mark.parametrize("service,message", [
    (_svc(id="plain"), "unresolved or unpinned service"),
    (_svc(members=()), "missing root release/workflow"),
    (_svc(members=("x",)), "missing root release/workflow"),
    (_svc(members=(rel("r@1", "route"),)), "missing root release/workflow"),
    (_svc(checkable=False), "cannot enforce before effect"),
    (_svc(checkable=1), "cannot enforce before effect"),
    (_svc(deps=("x",)), "unresolved dependency"),
    (_svc(members=(rel("plain"),)), "unsupported or unpinned subject"),
    (_svc(members=(rel("r@1"), rel("x@1", "ghost"))), "unsupported or unpinned subject"),
    (_svc(members=(rel("r@1"), Subject("r@1", "release", (fact(),)))), "conflicting subject revision in closure"),
])
def test_resolving_a_service_refuses_what_it_cannot_enforce(service, message):
    with pytest.raises(PolicyError) as raised:
        resolve(service)
    assert str(raised.value) == message


def test_a_dependency_cycle_and_a_conflicting_revision_are_refused_and_a_clean_closure_resolves():
    inner = Service("a@1", (rel("ia@1"),))
    cyc = Service("a@1", (rel("r@1"),), (inner,))
    with pytest.raises(PolicyError) as raised:
        resolve(cyc)
    assert str(raised.value) == "conflicting service revision in closure" or str(raised.value) == "dependency cycle"
    same_id = Service("a@1", (rel("r@1"),), (Service("a@1", (rel("r@1"),)),))
    with pytest.raises(PolicyError) as cycle:
        resolve(same_id)
    assert str(cycle.value) == "dependency cycle"
    left = Service("l@1", (rel("l@1"),), (Service("shared@1", (rel("s@1"),)),))
    right = Service("shared@1", (rel("s@1"), rel("extra@1")))
    with pytest.raises(PolicyError) as conflict:
        resolve(Service("top@1", (rel("top@1"),), (left, right)))
    assert str(conflict.value) == "conflicting service revision in closure"
    ok = Service("top@1", (rel("top@1"),), (Service("d@1", (rel("d@1"), rel("x@1", "route"))),
                                             Service("d@1", (rel("d@1"), rel("x@1", "route")))))
    assert [s.id for s in resolve(ok)] == ["top@1", "d@1", "x@1"]


@pytest.mark.parametrize("row,denying,message", [
    (Row("", "aud", "in", ("kids",)), False, "unknown category or unnamed rule"),
    (Row(5, "aud", "in", ("kids",)), False, "unknown category or unnamed rule"),
    (Row("r", "ghost", "in", ("kids",)), False, "unknown category or unnamed rule"),
    (Row("r", "aud", "like", ()), False, "unsupported operator"),
    (Row("r", "aud", "exists", ()), True, "unsupported operator"),
    (Row("r", "aud", "exists", ("kids",)), False, "presence operator cannot have candidates"),
    (Row("r", "aud", "missing", ("kids",)), True, "presence operator cannot have candidates"),
    (Row("r", "aud", "in", ("ghost",)), False, "unknown rule value"),
])
def test_a_malformed_rule_row_is_refused(row, denying, message):
    with pytest.raises(PolicyError) as raised:
        cp._validate_row(row, CATS, denying)
    assert str(raised.value) == message


def test_a_well_formed_row_passes_in_the_places_it_is_allowed():
    cp._validate_row(Row("r", "aud", "in", ("kids",)), CATS)
    cp._validate_row(Row("r", "aud", "exists"), CATS)
    cp._validate_row(Row("r", "aud", "missing"), CATS, True)


def _validate(categories=CATS, plans=(), subjects=()):
    cp._validate(categories, plans, subjects)


GOOD_GRANT = {"capability": "image", "service": "s@1"}


@pytest.mark.parametrize("categories,subjects,plans,message", [
    ({"other": CAT}, (), (), "category key mismatch"),
    (CATS, (rel("plain"),), (), "unsupported subject"),
    (CATS, (rel("r@1", "ghost"),), (), "unsupported subject"),
    (CATS, (rel("r@1", "release", fact(accepted=1)),), (), "invalid acceptance flag"),
    (CATS, (rel("r@1", "release", fact(category="ghost")),), (), "invalid accepted value"),
    (CATS, (rel("r@1", "release", fact(value="ghost")),), (), "invalid accepted value"),
    ({"aud": Category("aud", "A", values=("kids",), applies_to=("workflow",))}, (rel("r@1", "release", fact()),), (),
     "assertion applicability mismatch"),
    (CATS, (rel("r@1", "release", fact(source="")),), (), "incomplete accepted provenance"),
    (CATS, (rel("r@1", "release", fact(observed_at="")),), (), "incomplete accepted provenance"),
    (CATS, (rel("r@1", "release", fact(accepted_at="")),), (), "incomplete accepted provenance"),
    (CATS, (rel("r@1", "release", fact(accepting_owner="")),), (), "incomplete accepted provenance"),
    (CATS, (rel("r@1", "release", fact(mapping_version="")),), (), "incomplete accepted provenance"),
    (CATS, (), (Plan(""),), "invalid plan"),
    (CATS, (), (Plan(3),), "invalid plan"),
    (CATS, (), (Plan("p", mode="other"),), "invalid plan"),
    (CATS, (), (Plan("p", grants=("x",)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({"capability": "image"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({"service": "s@1"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({**GOOD_GRANT, "extra": "x"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({"capability": "", "service": "s@1"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({"capability": "image", "service": "plain"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({**GOOD_GRANT, "release": "plain"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", grants=({**GOOD_GRANT, "workflow": "plain"},)),), "A9: exact capability and root service required"),
    (CATS, (), (Plan("p", require=(Row("x", "aud", "exists"),), deny=(Row("x", "aud", "missing"),)),), "duplicate rule ID"),
    (CATS, (), (Plan("p", missing={"ghost": "allow"}),), "A4: waiver must name one requirement"),
    (CATS, (), (Plan("p", require=(Row("x", "aud", "exists"),), missing={"x": "deny"}),), "A4: waiver must name one requirement"),
    (CATS, (), (Plan("p", mode="legacy", require=(Row("x", "aud", "exists"),)),), "legacy conversion must be explicit"),
    (CATS, (), (Plan("p", mode="legacy", deny=(Row("x", "aud", "missing"),)),), "legacy conversion must be explicit"),
    (CATS, (), (Plan("p", mode="legacy", require=(Row("x", "aud", "exists"),), missing={"x": "allow"}),), "legacy conversion must be explicit"),
    (CATS, (), (Plan("p", mode="v3", legacy_tags=("t",)),), "legacy tags require legacy mode"),
    (CATS, (), (Plan("p", mode="legacy", legacy_tags=(7,)),), "invalid legacy tag"),
])
def test_a_snapshot_with_one_fault_is_refused_with_its_message(categories, subjects, plans, message):
    with pytest.raises(PolicyError) as raised:
        _validate(categories, plans, subjects)
    assert str(raised.value) == message


def test_a_clean_snapshot_with_inert_unaccepted_evidence_and_good_plans_passes():
    plan = Plan("p", grants=({**GOOD_GRANT, "release": "r@1", "workflow": "w@1"},),
                require=(Row("a", "aud", "in", ("kids",)),), deny=(Row("b", "aud", "missing"),), missing={"a": "allow"})
    legacy = Plan("l", mode="legacy", legacy_tags=("t",))
    _validate(CATS, (plan, legacy), (rel("r@1", "release", fact(), fact(accepted=False, value="ghost", source="")),))
