"""Property tests for the three authorization modules (test strategy 4.3, P1).

Each oracle is written from the modules' documented contract, not copied from
their code:

* ``category_policy``: a fault model. Every guard that fails is collected as a
  (precedence key, reason, rule, category, member) fact; the decision is the
  fact with the smallest key, or allow when there is none. Precedence follows
  the docstrings: all deny rows first, then per plan its grant and then its
  requirements, in plan, row and first-occurrence subject order.
* ``category_enforcement``: a truth table over gate state, governance,
  resolved target and the kernel model above.
* ``policy``: the written rules of one access plan, applied to both the
  person's and the app's plan (design v4 §2.6), with a hand-written table for
  direct-engine operation scopes.

Metamorphic relations need no model: adding a plan, a deny row or a
requirement never turns a denial into an allow, and a gain on one side of
the two plans never widens the other.

Hypothesis profiles live in tests/conftest.py (gate: 100 examples, nightly:
2,000; both derandomized). Boundary cases and every shrunk counterexample
stay in this file as explicit @example cases.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import example, given, strategies as st

from gateway import category_enforcement, category_policy as kernel
from gateway.access_plans import AccessPlan, CapabilityGrant, DirectEngineAccess
from gateway.policy import ALLOWED, DENIED, EffectiveAccess, PolicyDecision, PolicyRequest, evaluate, matches_operation


# --------------------------------------------------------------------------
# category_policy: generators
# --------------------------------------------------------------------------

KINDS = ("release", "route", "workflow")
VALUES = {"aud": ("g", "t", "a"), "loc": ("l", "c")}
CAPABILITY = "cap"
PROVENANCE = dict(source="owner", observed_at="2026-01-01", accepted_at="2026-01-02",
                  accepting_owner="owner-1", mapping_version="map@1")


def _accepted(category: str, value: str) -> kernel.Assertion:
    return kernel.Assertion(category, value, accepted=True, **PROVENANCE)


@st.composite
def categories(draw) -> dict[str, kernel.Category]:
    loc_kinds = draw(st.lists(st.sampled_from(KINDS), min_size=1, max_size=3, unique=True))
    return {
        "aud": kernel.Category("aud", "Audience", values=VALUES["aud"]),
        "loc": kernel.Category("loc", "Location", values=VALUES["loc"], applies_to=tuple(loc_kinds)),
    }


@st.composite
def subject(draw, cats, sid: str, kinds=KINDS) -> kernel.Subject:
    kind = draw(st.sampled_from(kinds))
    facts = []
    for cid, category in cats.items():
        if kind not in category.applies_to:
            continue  # An accepted fact on an inapplicable kind is invalid input.
        for value in draw(st.lists(st.sampled_from(category.values), max_size=2, unique=True)):
            facts.append(_accepted(cid, value))
    # Unaccepted evidence, even malformed, must be inert.
    if draw(st.booleans()):
        facts.append(kernel.Assertion(draw(st.sampled_from(["aud", "loc", "nope"])),
                                      draw(st.sampled_from(["g", "a", "zzz", 7])), "outside"))
    return kernel.Subject(sid, kind, tuple(draw(st.permutations(facts))))


@st.composite
def service(draw, cats, prefix="s", service_id="svc@r1", depth=1) -> kernel.Service:
    root = draw(subject(cats, f"{prefix}0@1", kinds=("release", "workflow")))
    extra = [draw(subject(cats, f"{prefix}{i}@1")) for i in range(1, 1 + draw(st.integers(0, 2)))]
    deps = ()
    if depth and draw(st.booleans()):
        deps = (draw(service(cats, prefix=prefix + "d", service_id=f"{prefix}dep@r1", depth=0)),)
    return kernel.Service(service_id, (root, *extra), deps)


@st.composite
def row(draw, rid: str, ops) -> kernel.Row:
    cid = draw(st.sampled_from(sorted(VALUES)))
    op = draw(st.sampled_from(ops))
    values = tuple(draw(st.lists(st.sampled_from(VALUES[cid]), max_size=2, unique=True))) if op == "in" else ()
    return kernel.Row(rid, cid, op, values)


@st.composite
def grant(draw, svc: kernel.Service) -> kernel.Grant:
    root = svc.members[0]
    # Weighted toward matching grants so that allows and requirement faults are common.
    binding = draw(st.sampled_from(["none", "none", "root", "root", "wrong-id", "wrong-kind"]))
    extra: dict[str, str] = {}
    if binding == "root":
        extra[root.kind] = root.id
    elif binding == "wrong-id":
        extra[root.kind] = "other@1"
    elif binding == "wrong-kind":
        extra["release" if root.kind == "workflow" else "workflow"] = root.id
    return kernel.Grant(draw(st.sampled_from([CAPABILITY] * 5 + ["other"])),
                        draw(st.sampled_from([svc.id] * 5 + ["elsewhere@r1"])), **extra)


@st.composite
def plan(draw, svc: kernel.Service, pid: str) -> kernel.Plan:
    grants = draw(st.lists(grant(svc), min_size=draw(st.sampled_from([0, 1, 1, 1])), max_size=2))
    require = [draw(row(f"r{i}", ("in", "exists", "missing"))) for i in range(draw(st.integers(0, 2)))]
    deny = [draw(row(f"d{i}", ("in", "missing"))) for i in range(draw(st.sampled_from([0, 0, 0, 1, 2, 2])))]
    waived = draw(st.lists(st.sampled_from([r.id for r in require]), unique=True)) if require else []
    return kernel.Plan(pid, tuple(grants), tuple(require), tuple(deny), {rid: "allow" for rid in waived})


@st.composite
def world(draw):
    """A valid closure plus person and app plans for one capability."""
    cats = draw(categories())
    svc = draw(service(cats))
    people = [draw(plan(svc, f"pp{i}")) for i in range(draw(st.integers(1, 2)))]
    apps = [draw(plan(svc, f"ap{i}")) for i in range(draw(st.integers(1, 2)))]
    return cats, tuple(people), tuple(apps), svc


# --------------------------------------------------------------------------
# category_policy: the fault model
# --------------------------------------------------------------------------

def closure(svc: kernel.Service) -> list[kernel.Subject]:
    """Members of the service, then each dependency's, first occurrence wins."""
    seen: dict[str, kernel.Subject] = {}

    def walk(node: kernel.Service) -> None:
        for member in node.members:
            seen.setdefault(member.id, member)
        for dep in node.dependencies:
            walk(dep)

    walk(svc)
    return list(seen.values())


def model_decide(cats, person_plans, app_plans, svc, capability=CAPABILITY):
    """Return (outcome, reason, rule, category, member, waivers)."""
    if not person_plans or not app_plans:
        return ("deny", "NO_PLANS", "identity", "", "", ())
    subjects = closure(svc)
    plans = list(person_plans) + list(app_plans)
    root = svc.members[0]

    def accepted(s, cid):
        return {a.value for a in s.assertions if a.accepted is True and a.category == cid}

    def applies(cid, s):
        return s.kind in cats[cid].applies_to

    def grant_matches(g):
        bound = {"release": g.release, "workflow": g.workflow}
        return (g.capability == capability and g.service == svc.id
                and all(value is None or (root.kind == kind and value == root.id)
                        for kind, value in bound.items()))

    faults = []
    for pi, p in enumerate(plans):
        for ri, r in enumerate(p.deny):
            for si, s in enumerate(subjects):
                if not applies(r.category, s):
                    continue
                values = accepted(s, r.category)
                if r.op == "missing":
                    fires, reason = not values, "EXPLICIT_DENY"
                elif not values:  # "in" over no accepted fact: unknown, which denies
                    fires, reason = True, "DENY_INPUT_UNKNOWN"
                else:
                    fires, reason = bool(values & set(r.values)), "EXPLICIT_DENY"
                if fires:
                    faults.append(((0, pi, ri, si), reason, f"{p.id}:{r.id}", r.category, s.id))
    waivers = []
    for pi, p in enumerate(plans):
        if not any(grant_matches(g) for g in p.grants):
            faults.append(((1, pi, 0, 0), "NO_GRANT", f"{p.id}:grant", "", ""))
            continue
        for ri, r in enumerate(p.require):
            targets = [s for s in subjects if applies(r.category, s)]
            if not targets:
                faults.append(((1, pi, 1 + ri, 0), "NO_APPLICABLE_SUBJECT", f"{p.id}:{r.id}", r.category, ""))
                continue
            for si, s in enumerate(targets):
                values = accepted(s, r.category)
                passes = {"missing": not values, "exists": bool(values),
                          "in": bool(values) and values <= set(r.values)}[r.op]
                if passes:
                    continue
                if not values and r.op != "missing" and p.missing.get(r.id) == "allow":
                    waivers.append(((1, pi, 1 + ri, si), (p.id, r.id, s.id)))
                    continue
                faults.append(((1, pi, 1 + ri, si), "REQUIRE_MISSING" if not values else "REQUIRE_MISMATCH",
                               f"{p.id}:{r.id}", r.category, s.id))
    if faults:
        key, reason, rule, category, member = min(faults)
        # A denial reports the waivers granted before the fault in evaluation
        # order (none when a deny row fires, which precedes every requirement).
        return ("deny", reason, rule, category, member, tuple(w for k, w in sorted(waivers) if k < key))
    return ("allow", "ALLOW", "", "", "", tuple(w for _k, w in sorted(waivers)))


def as_tuple(decision: kernel.Decision):
    return (decision.outcome, decision.reason, decision.rule, decision.category, decision.member,
            decision.waivers)


def decide(cats, people, apps, svc, capability=CAPABILITY):
    return kernel.evaluate(cats, people, apps, svc, capability)


# --------------------------------------------------------------------------
# category_policy: properties
# --------------------------------------------------------------------------

_ROOT_RELEASE = kernel.Subject("s0@1", "release")
_EXAMPLE_CATS = {"aud": kernel.Category("aud", "Audience", values=VALUES["aud"]),
                 "loc": kernel.Category("loc", "Location", values=VALUES["loc"])}
_GRANT_ALL = kernel.Grant(CAPABILITY, "svc@r1")


@given(world())
@example((  # An "in" deny over a subject with no accepted fact is unknown and denies.
    _EXAMPLE_CATS,
    (kernel.Plan("pp0", (_GRANT_ALL,), (), (kernel.Row("d0", "aud", "in", ("a",)),)),),
    (kernel.Plan("ap0", (_GRANT_ALL,)),),
    kernel.Service("svc@r1", (_ROOT_RELEASE,)),
))
@example((  # A waiver covers a missing value, never a mismatching one.
    _EXAMPLE_CATS,
    (kernel.Plan("pp0", (_GRANT_ALL,), (kernel.Row("r0", "aud", "in", ("g",)),), (), {"r0": "allow"}),),
    (kernel.Plan("ap0", (_GRANT_ALL,)),),
    kernel.Service("svc@r1", (kernel.Subject("s0@1", "release", (_accepted("aud", "a"),)),)),
))
@example((  # A waiver granted before a later plan's missing grant travels with that denial.
    _EXAMPLE_CATS,
    (kernel.Plan("pp0", (_GRANT_ALL,), (kernel.Row("r0", "aud", "exists"),), (), {"r0": "allow"}),),
    (kernel.Plan("ap0", ()),),
    kernel.Service("svc@r1", (kernel.Subject("s0@1", "release"),)),
))
@example((  # Of two firing deny rows in one plan, the first one decides.
    _EXAMPLE_CATS,
    (kernel.Plan("pp0", (_GRANT_ALL,), (), (kernel.Row("d0", "aud", "in", ("a",)),
                                            kernel.Row("d1", "aud", "in", ("a", "g")))),),
    (kernel.Plan("ap0", (_GRANT_ALL,)),),
    kernel.Service("svc@r1", (kernel.Subject("s0@1", "release", (_accepted("aud", "a"),)),)),
))
def test_kernel_matches_the_fault_model(case):
    cats, people, apps, svc = case
    expected = model_decide(cats, people, apps, svc)
    actual = decide(cats, people, apps, svc)
    assert as_tuple(actual) == expected


@pytest.mark.parametrize("people, apps", [
    ((), (kernel.Plan("ap0", (_GRANT_ALL,)),)),
    ((kernel.Plan("pp0", (_GRANT_ALL,)),), ()),
    ((), ()),
])
def test_a_side_with_no_plans_denies_before_any_grant_is_read(people, apps):
    svc = kernel.Service("svc@r1", (kernel.Subject("s0@1", "release"),))
    actual = decide(_EXAMPLE_CATS, people, apps, svc)
    assert as_tuple(actual) == model_decide(_EXAMPLE_CATS, people, apps, svc)
    assert (actual.outcome, actual.reason, actual.rule) == ("deny", "NO_PLANS", "identity")


@given(
    accepted=st.frozensets(st.sampled_from(VALUES["aud"])),
    op=st.sampled_from(["in", "exists", "missing"]),
    candidates=st.frozensets(st.sampled_from(VALUES["aud"])),
    waived=st.booleans(),
)
@example(accepted=frozenset({"g", "a"}), op="in", candidates=frozenset({"g"}), waived=False)
def test_one_requirement_row_is_a_relation_over_accepted_values(accepted, op, candidates, waived):
    """in: every accepted value is a candidate; exists: some value; missing: none.
    A waiver turns only a missing value (never a mismatch) into an allow."""
    facts = tuple(_accepted("aud", value) for value in sorted(accepted))
    svc = kernel.Service("svc@r1", (kernel.Subject("s0@1", "release", facts),))
    rule = kernel.Row("r0", "aud", op, tuple(sorted(candidates)) if op == "in" else ())
    person = kernel.Plan("pp0", (_GRANT_ALL,), (rule,), (), {"r0": "allow"} if waived else {})
    decision = decide(_EXAMPLE_CATS, (person,), (kernel.Plan("ap0", (_GRANT_ALL,)),), svc)
    holds = {"in": bool(accepted) and accepted <= candidates, "exists": bool(accepted),
             "missing": not accepted}[op]
    if holds:
        expected = "ALLOW"
    elif not accepted and op != "missing" and waived:
        expected = "ALLOW"
    else:
        expected = "REQUIRE_MISSING" if not accepted else "REQUIRE_MISMATCH"
    assert decision.reason == expected


def _tighten_plans(draw, people, apps, svc):
    """Return (people, apps) with one restriction added; never a widening."""
    choice = draw(st.sampled_from(["add-person-plan", "add-app-plan", "add-deny", "add-require",
                                   "drop-grant", "drop-waiver"]))
    if choice == "add-person-plan":
        return people + (draw(plan(svc, "extra-p")),), apps
    if choice == "add-app-plan":
        return people, apps + (draw(plan(svc, "extra-a")),)
    side = draw(st.sampled_from(["people", "apps"]))
    plans = list(people if side == "people" else apps)
    index = draw(st.integers(0, len(plans) - 1))
    target = plans[index]
    if choice == "add-deny":
        target = replace(target, deny=target.deny + (draw(row("dx", ("in", "missing"))),))
    elif choice == "add-require":
        target = replace(target, require=target.require + (draw(row("rx", ("in", "exists", "missing"))),))
    elif choice == "drop-grant" and target.grants:
        target = replace(target, grants=target.grants[1:])
    elif choice == "drop-waiver" and target.missing:
        dropped = draw(st.sampled_from(sorted(target.missing)))
        target = replace(target, missing={k: v for k, v in target.missing.items() if k != dropped})
    plans[index] = target
    return (tuple(plans), apps) if side == "people" else (people, tuple(plans))


@given(world(), st.data())
def test_kernel_restrictions_never_grant(case, data):
    """Plans intersect: one more plan, deny or requirement, or one less grant or
    waiver, can turn an allow into a denial but never a denial into an allow."""
    cats, people, apps, svc = case
    before = decide(cats, people, apps, svc)
    tight_people, tight_apps = _tighten_plans(data.draw, people, apps, svc)
    after = decide(cats, tight_people, tight_apps, svc)
    if before.outcome == "deny":
        assert after.outcome == "deny"


@given(world(), st.data())
def test_unaccepted_evidence_is_inert(case, data):
    cats, people, apps, svc = case
    members = list(svc.members)
    index = data.draw(st.integers(0, len(members) - 1))
    noise = kernel.Assertion(data.draw(st.sampled_from(["aud", "loc", "nope"])),
                             data.draw(st.sampled_from(["g", "t", "a", "l", "c", "zzz", None])),
                             data.draw(st.sampled_from(["owner", "outside", ""])))
    members[index] = replace(members[index], assertions=members[index].assertions + (noise,))
    noisy = replace(svc, members=tuple(members))
    assert decide(cats, people, apps, noisy) == decide(cats, people, apps, svc)


@given(world(), st.data())
def test_incomplete_accepted_provenance_fails_closed(case, data):
    cats, people, apps, svc = case
    field = data.draw(st.sampled_from(sorted(PROVENANCE)))
    blank = data.draw(st.sampled_from(["", None]))
    root = svc.members[0]
    category = "aud"  # applies to every kind
    broken = kernel.Assertion(category, "g", accepted=True, **{**PROVENANCE, field: blank})
    svc = replace(svc, members=(replace(root, assertions=root.assertions + (broken,)), *svc.members[1:]))
    assert decide(cats, people, apps, svc) == kernel.deny("POLICY_UNAVAILABLE")


@given(world())
def test_snapshot_and_preview_agree_with_the_evaluator(case):
    """Differential pair (strategy 4.2.3): decision_for and preview_as wrap evaluate."""
    cats, people, apps, svc = case
    version = kernel.PolicyVersion("v1", cats, {"p": people}, {"a": apps}, {svc.id: svc})
    direct = kernel.evaluate(cats, people, apps, svc, CAPABILITY, person="p", app="a")
    request = {"person": "p", "app": "a", "service": svc.id, "capability": CAPABILITY}
    assert kernel.decision_for(version, request) == direct
    assert kernel.preview_as(version, "p", "a", CAPABILITY) == {svc.id: direct}


# --------------------------------------------------------------------------
# category_enforcement: the gate never widens and follows its truth table
# --------------------------------------------------------------------------

class _FixedGate(category_enforcement.CategoryGate):
    """The gate with its store read replaced by a fixed answer.

    The store, its revision counter and its cache have real-SQLite tests in
    test_category_enforcement.py; these properties are about ``check`` and
    ``apply`` given an active snapshot.
    """

    def __init__(self, version, unavailable):
        super().__init__("/nonexistent/categories.db")
        self._fixed = (version, unavailable)

    def _active(self):
        return self._fixed


PEOPLE, APPS = ("p0", "p1"), ("a0", "a1")


@st.composite
def enforcement_case(draw):
    cats = draw(categories())
    stems = draw(st.lists(st.sampled_from(["flux", "sdxl", "tts"]), unique=True,
                          min_size=draw(st.sampled_from([0, 1, 2, 2, 3])), max_size=3))
    services = {}
    for stem in stems:
        svc = draw(service(cats, prefix=stem, service_id=f"{stem}@r1"))
        services[svc.id] = svc
    probe = next(iter(services.values()), kernel.Service("none@r1", (kernel.Subject("n@1", "workflow"),)))
    # Choose the governance case first, so that each is common, including a
    # governed request that the kernel allows (the case the gate must not refuse).
    def plans_for(owner):
        kind = draw(st.sampled_from(["open", "open", "open", "random", "random", "none"]))
        if kind == "open":  # One grant on the first service and no rows.
            return (kernel.Plan(f"{owner}-open", (kernel.Grant(CAPABILITY, probe.id),)),)
        if kind == "none":
            return ()
        return tuple(draw(plan(probe, f"{owner}-plan{i}")) for i in range(draw(st.integers(1, 2))))

    scenario = draw(st.sampled_from(["both", "both", "both", "person-only", "app-only", "neither"]))
    person = draw(st.sampled_from(PEOPLE))
    app = draw(st.sampled_from(APPS))
    people = {p: plans_for(p) for p in draw(st.lists(st.sampled_from(PEOPLE), unique=True))
              if p != person}
    apps = {a: plans_for(a) for a in draw(st.lists(st.sampled_from(APPS), unique=True)) if a != app}
    if scenario in {"both", "person-only"}:
        people[person] = plans_for(person)
    if scenario in {"both", "app-only"}:
        apps[app] = plans_for(app)
    state = draw(st.sampled_from(["active", "active", "active", "none", "unavailable", "no-gate"]))
    version = kernel.PolicyVersion("rules", cats, people, apps, services) if state == "active" else None
    identity = type("Identity", (), {
        "person_id": draw(st.sampled_from([person] * 4 + ["px", "", None])),
        "app_id": draw(st.sampled_from([app] * 4 + ["ax", "", None])),
    })()
    targets = [probe.id.rsplit("@", 1)[0]] * 2 + [*stems, "unknown", None]
    early = draw(st.sampled_from([False, False, True]))  # Route check before resolution.
    request = PolicyRequest(intent=draw(st.sampled_from([CAPABILITY] * 4 + ["other", None])),
                            workflow=None if early else draw(st.sampled_from(targets)),
                            model=None if early else draw(st.sampled_from(targets)))
    prior = PolicyDecision(
        decision=draw(st.sampled_from([ALLOWED, ALLOWED, DENIED, "not_evaluated"])),
        sources=tuple(draw(st.lists(st.sampled_from(["profile:x", "policy:y"]), max_size=2))),
        reasons=tuple(draw(st.lists(st.sampled_from(["r1", "r2"]), max_size=2))),
        visibility_level=draw(st.sampled_from(["none", "basic", "full"])),
    )
    return state, version, identity, request, prior


def _model_kernel(version, person, app, svc, capability):
    """decision_for's identity rule, then the kernel fault model: (outcome, reason, rule, category, member)."""
    if not person or not app or person not in version.people or app not in version.apps:
        return ("deny", "IDENTITY_UNAVAILABLE", "identity", "", "")
    if not capability:
        return ("deny", "POLICY_UNAVAILABLE", "evaluation", "", "")  # Invalid input fails closed.
    return model_decide(version.categories, version.people[person], version.apps[app], svc,
                        capability)[:5]


def _model_allows(version, person, app, svc, capability):
    return _model_kernel(version, person, app, svc, capability)[0] == "allow"


def _kernel_denial_text(version, person, app, svc, capability):
    _outcome, reason, *detail = _model_kernel(version, person, app, svc, capability)
    return f"category rules: {reason} {' '.join(part for part in detail if part)}".strip()


def expected_gate(state, version, identity, request, prior):
    """Return ("unchanged",) or ("denied", the complete reason)."""
    if not prior.allowed or state in {"no-gate", "none"}:
        return ("unchanged",)
    if state == "unavailable":
        return ("denied", "category rules: POLICY_UNAVAILABLE")
    person, app = identity.person_id or "", identity.app_id or ""
    if person not in version.people and app not in version.apps:
        return ("unchanged",)
    capability = request.intent or ""
    target = request.workflow or request.model or ""
    if not target:
        if any(_model_allows(version, person, app, svc, capability) for svc in version.services.values()):
            return ("unchanged",)
        if not version.services:
            return ("denied", f"category rules: NO_GRANT (no service grants {capability or 'this request'})")
        return ("denied", _kernel_denial_text(version, person, app, next(iter(version.services.values())), capability))
    matches = [svc for key, svc in version.services.items() if key.rsplit("@", 1)[0] == target]
    if not matches:
        return ("denied", f"category rules: NO_GRANT (no service for {target})")
    if _model_allows(version, person, app, matches[0], capability):
        return ("unchanged",)
    return ("denied", _kernel_denial_text(version, person, app, matches[0], capability))


def _apply(state, version, identity, request, prior):
    saved = category_enforcement._GATE
    category_enforcement._GATE = None if state == "no-gate" else _FixedGate(version, state == "unavailable")
    try:
        return (category_enforcement.apply(identity, request, prior),
                category_enforcement.visible(identity, request))
    finally:
        category_enforcement._GATE = saved


@given(enforcement_case())
def test_category_gate_never_widens(case):
    state, version, identity, request, prior = case
    result, _visible = _apply(*case)
    if not prior.allowed:
        assert result is prior
        return
    assert result is prior or (
        result.decision == DENIED
        and result.sources == prior.sources + (category_enforcement.SOURCE,)
        and result.visibility_level == prior.visibility_level
        and len(result.reasons) == 1 and result.reasons[0].startswith("category rules: ")
    )


def _simple_service(stem):
    return kernel.Service(f"{stem}@r1", (kernel.Subject(f"{stem}0@1", "release"),))


_OPEN = kernel.Plan("open", (kernel.Grant(CAPABILITY, "flux@r1"),))
_GOVERNED = type("Identity", (), {"person_id": "p0", "app_id": "a0"})()


@given(enforcement_case())
@example((  # Shrunk (nightly): an early check passes when one of three services is granted.
    "active",
    kernel.PolicyVersion("rules", _EXAMPLE_CATS, {"p0": (_OPEN,)}, {"a0": (_OPEN,)},
                         {stem + "@r1": _simple_service(stem) for stem in ("flux", "sdxl", "tts")}),
    _GOVERNED, PolicyRequest(intent=CAPABILITY), PolicyDecision(decision=ALLOWED),
))
def test_category_gate_follows_its_truth_table(case):
    state, version, identity, request, prior = case
    result, visible = _apply(*case)
    expected = expected_gate(*case)
    if expected == ("unchanged",):
        assert result is prior
    else:
        assert result.decision == DENIED
        assert result.reasons == (expected[1],)
    # Discovery shows an entry exactly when an allowed request would stay allowed.
    allowed = replace(prior, decision=ALLOWED)
    assert visible == (expected_gate(state, version, identity, request, allowed) == ("unchanged",))


@given(granted=st.sets(st.sampled_from(["flux", "sdxl", "tts"])),
       ungranted=st.sets(st.sampled_from(["mix", "edit"])))
def test_early_category_check_passes_when_any_service_is_granted(granted, ungranted):
    """Before resolution the gate asks: is SOME service allowed for this capability?"""
    plan = kernel.Plan("plan", tuple(kernel.Grant(CAPABILITY, f"{stem}@r1") for stem in sorted(granted)))
    services = {f"{stem}@r1": _simple_service(stem) for stem in sorted(granted | ungranted)}
    version = kernel.PolicyVersion("rules", _EXAMPLE_CATS, {"p0": (plan,)}, {"a0": (plan,)}, services)
    prior = PolicyDecision(decision=ALLOWED, sources=("profile:x",))
    result, _visible = _apply("active", version, _GOVERNED, PolicyRequest(intent=CAPABILITY), prior)
    assert (result is prior) == bool(granted)


# --------------------------------------------------------------------------
# policy: two access plans (design v4 §2.5, §2.6)
# --------------------------------------------------------------------------

OPS = ("GET /v1/models", "POST /v1/chat/completions", "GET /api/tags")
# Hand-written from matches_operation's docstring: exact strings, "*",
# "METHOD *", path-only scopes and prefix scopes ending in "*". An empty
# scope set matches nothing.
SCOPE_MATCHES = {
    "*": set(OPS),
    "GET *": {"GET /v1/models", "GET /api/tags"},
    "POST *": {"POST /v1/chat/completions"},
    "GET /v1/models": {"GET /v1/models"},
    "/v1/models": {"GET /v1/models"},
    "v1/models": {"GET /v1/models"},
    "/v1/*": {"GET /v1/models", "POST /v1/chat/completions"},
    "POST /v1/chat/completions": {"POST /v1/chat/completions"},
}
CAPS = ("chat", "image", "embed")
INTENTS = {"chat": "chat.completions", "image": "image.generate", "embed": "text.embeddings"}
ITEMS = ("m1", "m2")
ENGINES = ("b1", "b2")
PROVIDERS = ("openrouter", "typesafe")


@st.composite
def access_plans(draw, plan_id="prop") -> AccessPlan:
    can_use = {}
    for cap in draw(st.frozensets(st.sampled_from(CAPS))):
        if draw(st.booleans()):
            can_use[cap] = CapabilityGrant(all_items=True)
        else:
            can_use[cap] = CapabilityGrant(models=draw(st.frozensets(st.sampled_from(ITEMS))))
    all_engines = draw(st.booleans())
    return AccessPlan(
        id=plan_id, name=plan_id, can_use=can_use,
        direct_engine_access=DirectEngineAccess(
            all_engines=all_engines,
            engines=frozenset() if all_engines else draw(st.frozensets(st.sampled_from(ENGINES))),
            operations=draw(st.frozensets(st.sampled_from(sorted(SCOPE_MATCHES)), max_size=3)),
        ),
        paid_providers=draw(st.frozensets(st.sampled_from(PROVIDERS))),
        provider_fallback=draw(st.booleans()),
        can_load_models=draw(st.booleans()),
        receipt_detail=draw(st.sampled_from(["none", "basic", "standard", "full"])),
        media_metadata=draw(st.sampled_from(["none", "full"])),
    )


@st.composite
def plan_requests(draw) -> PolicyRequest:
    if draw(st.integers(0, 3)) == 0:
        return PolicyRequest(intent="provider.native", native_passthrough=True,
                             native_backend=draw(st.sampled_from(ENGINES + ("b3",))),
                             native_operation=draw(st.one_of(st.none(), st.sampled_from(OPS))))
    cap = draw(st.sampled_from(CAPS + ("video",)))
    return PolicyRequest(
        intent=INTENTS.get(cap, "video.generate"),
        model=draw(st.one_of(st.none(), st.sampled_from(ITEMS + ("m3",)))),
        remote_provider=draw(st.one_of(st.none(), st.sampled_from(PROVIDERS + ("other",)))),
        remote_paid_fallback=draw(st.booleans()),
    )


def one_plan_allows(plan: AccessPlan, request: PolicyRequest) -> bool:
    """The written rules of one plan (design §2.1, §2.7), independent of the code."""
    if request.native_passthrough:
        direct = plan.direct_engine_access
        engine_ok = direct.all_engines or request.native_backend in direct.engines
        if not engine_ok or not direct.operations or not (direct.all_engines or direct.engines):
            return False
        return request.native_operation is None or any(
            request.native_operation in SCOPE_MATCHES[scope] for scope in direct.operations)
    cap = next((c for c, intent in INTENTS.items() if intent == request.intent), None)
    grant = plan.can_use.get(cap) if cap else None
    if grant is None:
        return False
    if not grant.all_items:
        if request.model is None:
            if not grant.models:
                return False
        elif request.model not in grant.models:
            return False
    if request.remote_provider is not None and request.remote_provider not in plan.paid_providers:
        return False
    if request.remote_paid_fallback and not plan.provider_fallback:
        return False
    return True


@given(access_plans("p"), access_plans("a"), plan_requests())
def test_a_request_runs_only_when_both_plans_allow_it(person, app, request):
    decision = evaluate(EffectiveAccess(person_plan=person, app_plan=app), request)
    assert decision.allowed == (one_plan_allows(person, request) and one_plan_allows(app, request))
    if decision.denied:
        assert decision.side == ("person" if not one_plan_allows(person, request) else "app")


@given(access_plans("p"), access_plans("a"), plan_requests())
def test_a_missing_plan_on_either_side_denies_everything(person, app, request):
    for effective in (EffectiveAccess(person, None), EffectiveAccess(None, app), EffectiveAccess(None, None)):
        assert evaluate(effective, request).code == "no_access_plan"


@given(access_plans("p"), access_plans("a"))
def test_settings_compose_to_the_lower_of_both_plans(person, app):
    effective = EffectiveAccess(person, app)
    order = ("none", "basic", "standard", "full")
    assert order.index(effective.receipt_detail) == min(order.index(person.receipt_detail),
                                                       order.index(app.receipt_detail))
    assert effective.paid_providers == person.paid_providers & app.paid_providers
    assert effective.can_load_models == (person.can_load_models and app.can_load_models)
    assert effective.provider_fallback == (person.provider_fallback and app.provider_fallback)


@given(access_plans("p"), access_plans("a"), access_plans("wide"), plan_requests())
def test_a_gain_on_one_side_never_widens_the_other(person, app, wide, request):
    """Swapping the app's plan for any other never allows what the person's plan denies."""
    if not one_plan_allows(person, request):
        assert evaluate(EffectiveAccess(person, wide), request).denied
        assert evaluate(EffectiveAccess(person, app), request).denied


@given(enabled_ops=st.frozensets(st.sampled_from(sorted(SCOPE_MATCHES)), max_size=3),
       operation=st.sampled_from(OPS))
def test_operation_scopes_follow_the_scope_table(enabled_ops, operation):
    assert matches_operation(operation, enabled_ops) == any(
        operation in SCOPE_MATCHES[scope] for scope in enabled_ops)
