"""Product kernel scenarios ported from the design v3 reference suite.

Storage/activation-only scenarios 51, 85-88, 94, 96-97, 130 and 132 are
outside this slice. Mixed preview/cache scenarios exercise the pure boundary;
they make no claims about runtime wiring, durable commit or cache eviction.
Run with: python -m unittest discover -s tests -p test_category_policy.py
"""
import unittest
from dataclasses import replace
from gateway import category_policy as model
from gateway.category_policy import (Assertion, Category, Row, Plan, Subject, Service, PolicyError,
                   PolicyVersion, Registry, accept_evidence, evaluate,
                   evaluate_routes, decision_for, change_preview, migration_comparison,
                   legacy_exact, legacy_gated, preview_as, validate_snapshot, Grant)


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.categories = {
            "audience": Category("audience", "Audience", values=("general", "teen", "adult")),
            "location": Category("location", "Location", values=("local", "cloud")),
            "licence": Category("licence", "Licence", values=("commercial", "non-commercial")),
        }
        self.plan = Plan("person-plan", ({"capability": "generate", "service": "choice@1"},))
        self.app = replace(self.plan, id="app-plan")
        self.service = Service("choice@1", (Subject("release@1", "release"),))
        self.request = dict(person="person", app="app", service="choice@1", capability="generate")
        self.auth = dict(actor="owner", owner="owner")

    def value(self, category="audience", value="general", source="owner"):
        return accept_evidence(Assertion(category, value, source),
                               observed_at="2026-10-01", accepted_at="2026-10-02",
                               mapping_version="mapping@1",
                               mapped_value=value if source != "owner" else None, **self.auth)

    def with_values(self, *values):
        return replace(self.service, members=(Subject("release@1", "release", values),))

    def req(self, op="in", values=("general",), row_id="audience-rule", **changes):
        return replace(self.plan, require=(Row(row_id, "audience", op, values),), **changes)

    def block(self, op="in", values=("adult",), **changes):
        return replace(self.plan, deny=(Row("audience-deny", "audience", op, values),), **changes)

    def decide(self, plan=None, service=None, **kwargs):
        return evaluate(self.categories, (plan or self.plan,), (self.app,),
                        service or self.service, "generate", **kwargs)

    def assertDecision(self, actual, outcome, reason=None):
        self.assertEqual(actual.outcome, outcome)
        if reason:
            self.assertEqual(actual.reason, reason)
        if outcome == "deny":
            self.assertTrue(actual.rule)

    def snapshot(self, identifier="v1", **changes):
        return replace(PolicyVersion(identifier, self.categories, {"person": (self.plan,)},
                                    {"app": (self.app,)}, {"choice@1": self.service},
                                    retained={"output@1": self.service}), **changes)

    def assertMigration(self, request, version, outcomes, reasons=None, legacy_grants=True):
        result = migration_comparison(("general",), request, version, legacy_grants=legacy_grants)
        keys = ("legacy_exact", "legacy_gated", "v3")
        self.assertEqual(tuple(result[k].outcome for k in keys), outcomes)
        if reasons:
            self.assertEqual(tuple(result[k].reason for k in keys), reasons)
        return result

    def test_01_matching_grant_allows(self):
        """A9: exact capability plus root service grant allows."""
        self.assertDecision(self.decide(), "allow")

    def test_02_empty_grant_grants_nothing(self):
        """Preserved v2: an empty grant list grants nothing."""
        self.assertDecision(self.decide(replace(self.plan, grants=())), "deny", "NO_GRANT")

    def test_03_deny_wins_over_grant(self):
        """Preserved v2: explicit denies win."""
        self.assertDecision(self.decide(self.block(), self.with_values(self.value(value="adult"))), "deny", "EXPLICIT_DENY")

    def test_04_unknown_requirement_fails(self):
        """A3: missing accepted input cannot satisfy in."""
        self.assertDecision(self.decide(self.req()), "deny", "REQUIRE_MISSING")

    def test_05_named_missing_waiver(self):
        """A4 changes v2 category-wide waiver to one named requirement row."""
        p = self.req(missing={"audience-rule": "allow"})
        d = self.decide(p)
        self.assertDecision(d, "allow")
        self.assertEqual(d.waivers, (("person-plan", "audience-rule", "release@1"),))

    def test_06_waiver_never_bypasses_known_mismatch(self):
        """Preserved v2: missing waiver cannot fix a known mismatch."""
        p = self.req(missing={"audience-rule": "allow"})
        self.assertDecision(self.decide(p, self.with_values(self.value(value="adult"))), "deny", "REQUIRE_MISMATCH")

    def test_07_hold_declaration_rejected(self):
        """A14 first-slice deferral changes v2 hold behavior to configuration error."""
        self.assertDecision(self.decide(self.req(missing={"audience-rule": "hold"})), "deny", "POLICY_UNAVAILABLE")

    def test_08_output_category_rejected(self):
        """First-slice deferral changes v2 output holds to configuration error."""
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        self.assertDecision(self.decide(), "deny", "POLICY_UNAVAILABLE")

    def test_09_require_categories_are_and(self):
        """Preserved v2: separate category requirements intersect."""
        p = self.req()
        p = replace(p, require=p.require + (Row("location-rule", "location", "in", ("local",)),))
        s = self.with_values(self.value(), self.value("location", "cloud"))
        self.assertDecision(self.decide(p, s), "deny", "REQUIRE_MISMATCH")

    def test_10_values_within_row_are_or(self):
        """A2: candidate values OR; every accepted value must fit."""
        self.assertDecision(self.decide(self.req(values=("general", "teen")), self.with_values(self.value(value="teen"))), "allow")

    def test_11_person_and_app_intersect(self):
        """Preserved v2: an app with no grant blocks person access."""
        self.app = replace(self.app, grants=())
        self.assertDecision(self.decide(), "deny", "NO_GRANT")

    def test_12_app_deny_wins(self):
        """Preserved v2: app denial overrides a person grant."""
        self.app = replace(self.block(), id="app-plan")
        self.assertDecision(self.decide(service=self.with_values(self.value(value="adult"))), "deny", "EXPLICIT_DENY")

    def test_13_unknown_category_is_error(self):
        """Errors are POLICY_UNAVAILABLE, never content denials."""
        p = replace(self.plan, require=(Row("unknown", "absent", "exists"),))
        self.assertDecision(self.decide(p), "deny", "POLICY_UNAVAILABLE")

    def test_14_bad_accepted_type_is_error(self):
        """A14: accepted set IDs are strings, without coercion."""
        self.assertDecision(self.decide(service=self.with_values(self.value(value=True))), "deny", "POLICY_UNAVAILABLE")

    def test_15_empty_bundle_is_error(self):
        """Preserved v2: broken closure fails closed."""
        self.assertDecision(self.decide(service=replace(self.service, members=())), "deny", "POLICY_UNAVAILABLE")

    def test_17_unaccepted_evidence_leaves_deny_unknown(self):
        """R1 changes v2 test 17 allow to DENY_INPUT_UNKNOWN."""
        s = self.with_values(Assertion("audience", "adult", "manifest"))
        self.assertDecision(self.decide(self.block(), s), "deny", "DENY_INPUT_UNKNOWN")

    def test_18_explicit_owner_acceptance(self):
        """A11 replaces v2 standing-rule acceptance with explicit owner acceptance."""
        a = self.value(source="manifest:license@1")
        self.assertEqual((a.source, a.accepting_owner, a.mapping_version), ("manifest:license@1", "owner", "mapping@1"))
        self.assertEqual((a.observed_at, a.accepted_at), ("2026-10-01", "2026-10-02"))
        self.assertDecision(self.decide(self.req(), self.with_values(a)), "allow")

    def test_19_nonowner_cannot_accept(self):
        """A11: source metadata cannot authorize its own acceptance."""
        with self.assertRaises(PolicyError):
            accept_evidence(Assertion("audience", "general", "owner"), actor="app", owner="owner",
                            observed_at="date", accepted_at="date", mapping_version="m1")

    def test_20_request_subject_rejected(self):
        """First-slice deferral changes v2 request-side restrictions to rejection."""
        s = replace(self.service, members=(Subject("request@1", "request"),))
        self.assertDecision(self.decide(service=s), "deny", "POLICY_UNAVAILABLE")

    def test_21_classifier_declaration_rejected(self):
        """First-slice deferral removes v2 classifier execution, including coverage gaps."""
        with self.assertRaises(TypeError):
            self.decide(classifier={"modality": "audio"})

    def test_22_output_check_declaration_rejected(self):
        """First-slice deferral rejects unsupported output-check arguments."""
        with self.assertRaises(TypeError):
            self.decide(check={"language": "fa"})

    def test_23_timeout_hold_declaration_rejected(self):
        """First-slice deferral: no v2 timeout-to-hold path."""
        with self.assertRaises(TypeError):
            Plan("p", timeout_behavior="hold")

    def test_24_pending_hold_declaration_rejected(self):
        """First-slice deferral: no v2 pending-held decision state."""
        with self.assertRaises(TypeError):
            model.Decision("deny", "pending", status="held")

    def test_25_classifier_pass_cannot_grant(self):
        """First-slice deferral changes v2 classifier pass to unsupported declaration."""
        with self.assertRaises(TypeError):
            self.decide(output_check={"result": "pass"})

    def test_26_classifier_failure_is_not_content_reason(self):
        """First-slice deferral: unsupported classifier declarations cannot enter policy."""
        with self.assertRaises(TypeError):
            Plan("p", outputs=("generate",))

    def test_27_uncheckable_route_not_offered(self):
        """R4: every action must be checkable before its effect."""
        self.assertDecision(self.decide(service=replace(self.service, checkable=False)), "deny", "POLICY_UNAVAILABLE")

    def test_28_lineage_declaration_rejected(self):
        """First-slice deferral removes v2 inherited licence review state."""
        with self.assertRaises(TypeError):
            Assertion("licence", "commercial", "owner", needs_review=True)

    def test_29_inherited_deny_declaration_rejected(self):
        """First-slice deferral removes v2 lineage deny inheritance."""
        with self.assertRaises(TypeError):
            Subject("release@1", "release", inherited_denies=(Row("d", "licence", "missing"),))

    def test_30_change_preview_gains_losses_reasons(self):
        """A25 extends v2 gains/losses with explanation changes and explicit scope."""
        before = self.snapshot(people={"one": (self.plan,), "two": (replace(self.plan, grants=()),)})
        after = replace(before, id="v2", people={"one": (replace(self.plan, grants=()),), "two": (self.plan,)})
        requests = [dict(self.request, person=p) for p in ("one", "two")]
        preview = change_preview(before, after, requests)
        self.assertIn(requests[1], preview["gains"])
        self.assertIn(requests[0], preview["losses"])
        self.assertGreaterEqual(len(preview["reason_changes"]), 2)
        self.assertTrue(all(r in preview["scope"] for r in requests))

    def test_31_reset_removes_override(self):
        """Preserved v2/A23: reset removes only an overlay."""
        r = Registry({"a": "base"}, {"a": "overlay"})
        r.reset("a")
        self.assertEqual(r.effective(), {"a": "base"})

    def test_32_reset_does_not_undelete(self):
        """A23: deleted defaults stay deleted through reset and a defaults update."""
        r = Registry({"a": "base"}, {"a": "overlay"})
        r.delete("a")
        r.reset("a")
        r.update_defaults({"a": "new"})
        self.assertEqual(r.effective(), {})

    def test_33_time_never_removes_deny(self):
        """A24: age is explanatory; time never changes access."""
        p = self.block()
        r = Registry({"rule": {"observed_at": "2020-01-01", "plan": p}})
        self.assertEqual(r.tick("2030-01-01"), {"rule": True})
        self.assertDecision(self.decide(r.effective()["rule"]["plan"], self.with_values(self.value(value="adult"))), "deny", "EXPLICIT_DENY")

    def test_34_cloud_teen_category_intersection(self):
        """R3: legacy any-tag OR differs deliberately from category intersection."""
        p = self.req(values=("teen",))
        p = replace(p, require=p.require + (Row("place", "location", "in", ("local",)),))
        s = self.with_values(self.value(value="teen"), self.value("location", "cloud"))
        self.assertDecision(legacy_exact(("local", "teen"), ("cloud", "teen")), "allow")
        self.assertDecision(self.decide(p, s), "deny", "REQUIRE_MISMATCH")

    def test_35_ordered_category_rejected(self):
        """A14 changes v2 ordered allow to POLICY_UNAVAILABLE."""
        self.categories["audience"] = replace(self.categories["audience"], kind="ordered")
        self.assertDecision(self.decide(), "deny", "POLICY_UNAVAILABLE")

    def test_36_at_most_rejected(self):
        """A14 changes v2 ordered comparison to unsupported operator."""
        self.assertDecision(self.decide(self.req(op="at_most")), "deny", "POLICY_UNAVAILABLE")

    def test_37_number_category_rejected(self):
        """A14: number categories are deferred, not coerced."""
        self.categories["cost"] = Category("cost", "Cost", "number")
        self.assertDecision(self.decide(), "deny", "POLICY_UNAVAILABLE")

    def test_38_boolean_category_rejected(self):
        """A14 changes v2 boolean membership allow to POLICY_UNAVAILABLE."""
        self.categories["training"] = Category("training", "Training", "boolean")
        self.assertDecision(self.decide(), "deny", "POLICY_UNAVAILABLE")

    def test_39_number_membership_rejected(self):
        """A14 changes v2 numeric membership allow to POLICY_UNAVAILABLE."""
        self.assertDecision(self.decide(self.req(values=(2.5,))), "deny", "POLICY_UNAVAILABLE")

    def test_40_exists_requires_accepted_value(self):
        """Preserved v2 exists behavior using supported set IDs."""
        self.assertDecision(self.decide(self.req(op="exists", values=()), self.with_values(self.value())), "allow")

    def test_41_missing_matches_absence(self):
        """Preserved v2: explicit require-missing passes absence."""
        self.assertDecision(self.decide(self.req(op="missing", values=())), "allow")

    def test_42_missing_rejects_present_value(self):
        """Preserved v2: explicit require-missing rejects presence."""
        self.assertDecision(self.decide(self.req(op="missing", values=()), self.with_values(self.value())), "deny", "REQUIRE_MISMATCH")

    def test_43_deny_only_bypass_closed(self):
        """R1 changes v2 test 43 allow to DENY_INPUT_UNKNOWN."""
        self.assertDecision(self.decide(self.block()), "deny", "DENY_INPUT_UNKNOWN")

    def test_44_deny_missing_matches_absence(self):
        """Preserved v2: deny-missing blocks absence."""
        self.assertDecision(self.decide(self.block(op="missing", values=())), "deny", "EXPLICIT_DENY")

    def test_45_helper_cloud_is_checked(self):
        """A1/R2: every helper's own facts are checked."""
        helper = Service("helper@1", (Subject("helper-release@1", "release", (self.value("location", "cloud"),)),))
        s = replace(self.with_values(self.value("location", "local")), dependencies=(helper,))
        p = replace(self.plan, require=(Row("place", "location", "in", ("local",)),))
        d = self.decide(p, s)
        self.assertDecision(d, "deny", "REQUIRE_MISMATCH")
        self.assertEqual(d.member, "helper-release@1")

    def test_46_deny_any_member_wins(self):
        """A1: a safe root never hides a denied helper."""
        s = replace(self.with_values(self.value()), members=(
            Subject("release@1", "release", (self.value(),)),
            Subject("other@1", "release", (self.value(value="adult"),))))
        self.assertDecision(self.decide(self.block(), s), "deny", "EXPLICIT_DENY")

    def test_47_accepted_set_subset_semantics(self):
        """A2: allowed plus disallowed accepted values fail, as required in v3."""
        s = self.with_values(self.value(), self.value(value="adult"))
        self.assertDecision(self.decide(self.req(), s), "deny", "REQUIRE_MISMATCH")

    def test_48_cycle_denies(self):
        """R2: repeated service revision in the dependency stack is a cycle."""
        s = replace(self.service, dependencies=(self.service,))
        self.assertDecision(self.decide(service=s), "deny", "POLICY_UNAVAILABLE")

    def test_49_retrieval_rechecks_current_plans(self):
        """R4: recorded bundle uses current policy at retrieval."""
        v = self.snapshot()
        r = dict(self.request, phase="retrieval", output="output@1")
        self.assertDecision(decision_for(v, r), "allow")
        self.assertDecision(decision_for(replace(v, apps={"app": (replace(self.app, grants=()),)}), r), "deny", "NO_GRANT")

    def test_50_share_rechecks_current_policy(self):
        """R4: shares cannot reuse an old authorization result."""
        v = self.snapshot(apps={"app": (replace(self.app, grants=()),)})
        self.assertDecision(decision_for(v, dict(self.request, phase="share", output="output@1")), "deny", "NO_GRANT")

    def test_52_no_automatic_acceptance(self):
        """A11 removes standing acceptance rules, even when source and kind match."""
        a = Assertion("audience", "general", "manifest")
        self.assertFalse(a.accepted)
        self.assertFalse(hasattr(model, "StandingRule"))
        self.assertDecision(self.decide(self.req(), self.with_values(a)), "deny", "REQUIRE_MISSING")

    def test_53_unaccepted_broken_data_is_inert(self):
        """Preserved v2: malformed unaccepted evidence never changes authorization."""
        self.assertDecision(self.decide(service=self.with_values(Assertion("unknown", object(), ""))), "allow")

    def test_54_incomplete_provenance_is_error(self):
        """A11: all accepted provenance fields are required."""
        a = Assertion("audience", "general", "manifest", accepted=True)
        self.assertDecision(self.decide(service=self.with_values(a)), "deny", "POLICY_UNAVAILABLE")

    def test_55_category_waiver_rejected(self):
        """A4: old category-keyed waiver cannot silently become row-scoped."""
        self.assertDecision(self.decide(self.req(missing={"audience": "allow"})), "deny", "POLICY_UNAVAILABLE")

    def test_56_unknown_deny_cannot_be_waived(self):
        """A4/R1: requirement waiver cannot clear unknown deny input."""
        p = self.req(missing={"audience-rule": "allow"})
        p = replace(p, deny=self.block().deny)
        self.assertDecision(self.decide(p), "deny", "DENY_INPUT_UNKNOWN")

    def test_57_missing_recorded_output_is_error(self):
        """R4 replaces v2 output-subject requirement with a retained recorded bundle."""
        self.assertDecision(decision_for(self.snapshot(), dict(self.request, phase="release")), "deny", "POLICY_UNAVAILABLE")

    def test_58_empty_in_candidates_match_nothing(self):
        """Preserved v2: empty candidate set fails even with an accepted value."""
        self.assertDecision(self.decide(self.req(values=()), self.with_values(self.value())), "deny", "REQUIRE_MISMATCH")

    def test_59_first_failing_rule_and_member(self):
        """A25: named rule and member make the first failure inspectable."""
        p = replace(self.plan, require=(Row("first", "audience", "exists"), Row("second", "location", "exists")))
        d = self.decide(p)
        self.assertEqual((d.rule, d.category, d.member), ("person-plan:first", "audience", "release@1"))

    def test_60_missing_app_denies(self):
        """A10: no implicitly unrestricted app."""
        self.assertDecision(self.decide(app=None), "deny", "IDENTITY_UNAVAILABLE")

    def test_61_missing_person_denies(self):
        """A10: person authentication is mandatory."""
        self.assertDecision(self.decide(person=None), "deny", "IDENTITY_UNAVAILABLE")

    def test_62_empty_app_plans_denies(self):
        """A10: an authenticated app still needs attached plans."""
        self.assertDecision(evaluate(self.categories, (self.plan,), (), self.service, "generate"), "deny", "NO_PLANS")

    def test_63_empty_person_plans_denies(self):
        """A10: empty person plan list is not unrestricted."""
        self.assertDecision(evaluate(self.categories, (), (self.app,), self.service, "generate"), "deny", "NO_PLANS")

    def test_64_output_subject_rejected(self):
        """First-slice deferral changes v2 output assertion allow to rejection."""
        s = replace(self.service, members=self.service.members + (Subject("output@1", "output"),))
        self.assertDecision(self.decide(service=s), "deny", "POLICY_UNAVAILABLE")

    def test_65_no_output_rule_dispatch_deferral(self):
        """First-slice deferral changes v2 output-only dispatch skip to configuration error."""
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        self.assertDecision(self.decide(self.req(op="exists", values=())), "deny", "POLICY_UNAVAILABLE")

    def test_66_request_attribute_declaration_rejected(self):
        """First-slice deferral removes v2 request attributes, including restrictive ones."""
        with self.assertRaises(TypeError):
            self.decide(request_attributes={"audience": "adult"})

    def test_67_waiver_does_not_override_deny(self):
        """Preserved v2/A4: explicit missing deny wins."""
        p = self.req(missing={"audience-rule": "allow"})
        p = replace(p, deny=self.block(op="missing", values=()).deny)
        self.assertDecision(self.decide(p), "deny", "EXPLICIT_DENY")

    def test_68_waiver_does_not_override_error(self):
        """A4: errors are never waived."""
        p = self.req(missing={"audience-rule": "allow"})
        self.categories["audience"] = replace(self.categories["audience"], kind="ordered")
        self.assertDecision(self.decide(p), "deny", "POLICY_UNAVAILABLE")

    def test_69_waiver_does_not_override_grant(self):
        """A4: missing value waiver grants no service access."""
        p = self.req(missing={"audience-rule": "allow"}, grants=())
        self.assertDecision(self.decide(p), "deny", "NO_GRANT")

    def test_70_waiver_is_one_row_only(self):
        """A4: same-category second requirement remains mandatory."""
        p = self.req(missing={"audience-rule": "allow"})
        p = replace(p, require=p.require + (Row("second", "audience", "exists"),))
        d = self.decide(p)
        self.assertDecision(d, "deny", "REQUIRE_MISSING")
        self.assertEqual(d.rule, "person-plan:second")
        self.assertEqual(len(d.waivers), 1)

    def test_71_uncheckable_helper_not_offered(self):
        """R4: enforcement before effects also applies to helpers."""
        helper = Service("helper@1", (Subject("helper-release@1", "release"),), checkable=False)
        self.assertDecision(self.decide(service=replace(self.service, dependencies=(helper,))), "deny", "POLICY_UNAVAILABLE")

    def test_72_missing_member_cannot_borrow_facts(self):
        """A1: every applicable member satisfies its own requirement."""
        s = replace(self.service, members=(Subject("release@1", "release", (self.value(),)), Subject("helper@1", "release")))
        self.assertDecision(self.decide(self.req(), s), "deny", "REQUIRE_MISSING")

    def test_73_helper_id_cannot_satisfy_selector(self):
        """A9: optional release selector matches root only, never helper."""
        helper = Subject("helper-release@1", "release")
        s = replace(self.service, members=self.service.members + (helper,))
        p = replace(self.plan, grants=({"capability": "generate", "service": "choice@1", "release": "helper-release@1"},))
        self.assertDecision(self.decide(p, s), "deny", "NO_GRANT")

    def test_74_bare_helper_selector_invalid(self):
        """A9 changes v2 broad selectors: capability and root service are mandatory."""
        p = replace(self.plan, grants=({"release": "release@1"},))
        self.assertDecision(self.decide(p), "deny", "POLICY_UNAVAILABLE")

    def test_75_new_revision_needs_new_grant(self):
        """A9: no grant inheritance between service revisions."""
        self.assertDecision(self.decide(service=replace(self.service, id="choice@2")), "deny", "NO_GRANT")

    def test_76_exact_optional_root_selector(self):
        """A9: optional release and exact service selectors AND."""
        p = replace(self.plan, grants=({"capability": "generate", "service": "choice@1", "release": "release@1"},))
        self.assertDecision(self.decide(p), "allow")

    def test_77_grant_selectors_are_or(self):
        """A9: two complete selectors are alternatives."""
        p = replace(self.plan, grants=({"capability": "other", "service": "choice@1"},) + self.plan.grants)
        self.assertDecision(self.decide(p), "allow")

    def test_78_every_attached_person_plan_passes(self):
        """A10: a second attached person plan can block."""
        self.assertDecision(evaluate(self.categories, (self.plan, replace(self.plan, id="second", grants=())),
                                    (self.app,), self.service, "generate"), "deny", "NO_GRANT")

    def test_79_every_attached_app_plan_passes(self):
        """A10: a second app plan is not ignored."""
        self.assertDecision(evaluate(self.categories, (self.plan,), (self.app, replace(self.app, id="second", grants=())),
                                    self.service, "generate"), "deny", "NO_GRANT")

    def test_80_lookup_failure_denies(self):
        """A10: missing identity lookup fails closed."""
        self.assertDecision(decision_for(self.snapshot(), dict(self.request, app="unknown")), "deny", "IDENTITY_UNAVAILABLE")

    def test_81_alternative_routes_are_separate(self):
        """R2: route X failing does not poison route Y."""
        bad = self.with_values(self.value(value="adult"))
        good = replace(self.with_values(self.value()), id="choice@2")
        p = self.req(grants=self.plan.grants + ({"capability": "generate", "service": "choice@2"},))
        app = replace(self.app, grants=p.grants)
        results = evaluate_routes(self.categories, (p,), (app,), {"X": bad, "Y": good}, "generate")
        self.assertDecision(results["X"], "deny", "REQUIRE_MISMATCH")
        self.assertDecision(results["Y"], "allow")

    def test_82_routes_cannot_borrow_facts(self):
        """A1/R2: a classified alternative does not classify a missing alternative."""
        good = self.with_values(self.value())
        missing = replace(self.service, id="choice@2")
        p = self.req(grants=self.plan.grants + ({"capability": "generate", "service": "choice@2"},))
        results = evaluate_routes(self.categories, (p,), (replace(self.app, grants=p.grants),),
                                  {"X": missing, "Y": good}, "generate")
        self.assertDecision(results["X"], "deny", "REQUIRE_MISSING")
        self.assertDecision(results["Y"], "allow")

    def test_83_dynamic_dependency_rejected(self):
        """R2: mutable dependency names are not pinned closures."""
        self.assertDecision(self.decide(service=replace(self.service, dependencies=("latest-helper",))), "deny", "POLICY_UNAVAILABLE")

    def test_84_unresolved_alias_rejected(self):
        """R2: alias resolution must precede authorization."""
        self.assertDecision(self.decide(service=replace(self.service, id="latest")), "deny", "POLICY_UNAVAILABLE")

    def test_89_no_committed_snapshot_denies(self):
        """A22: absence of a caller-supplied committed snapshot fails closed."""
        self.assertDecision(decision_for(None, self.request), "deny", "POLICY_UNAVAILABLE")

    def test_90_reason_only_preview_change(self):
        """A25: unchanged denial outcome can still have a changed explanation."""
        before = self.snapshot(people={"person": (replace(self.plan, grants=()),)})
        after = self.snapshot("v2", people={"person": (self.req(),)})
        p = change_preview(before, after, [self.request])
        self.assertEqual((p["gains"], p["losses"]), ([], []))
        self.assertEqual(p["reason_changes"][0]["before"].reason, "NO_GRANT")
        self.assertEqual(p["reason_changes"][0]["after"].reason, "REQUIRE_MISSING")

    def test_91_snapshot_maps_are_immutable(self):
        """A25: an existing preview input cannot be changed through its maps."""
        version = self.snapshot()
        with self.assertRaises(TypeError):
            version.people["person"] = (replace(self.plan, grants=()),)
        self.assertDecision(decision_for(version, self.request), "allow")

    def test_92_snapshot_defensively_copies_caller_maps(self):
        """A22: input map edits cannot rewrite the supplied policy value."""
        people = {"person": [self.plan]}
        version = self.snapshot(people=people)
        people["person"].clear()
        people.clear()
        self.assertDecision(decision_for(version, self.request), "allow")

    def test_93_unsafe_legacy_gate_rejected(self):
        """A22/R3: candidate validation and runtime reject an unsafe gate."""
        version = self.snapshot(completeness_gate=False)
        with self.assertRaises(PolicyError):
            validate_snapshot(version)
        self.assertDecision(decision_for(version, self.request), "deny", "POLICY_UNAVAILABLE")

    def test_95_reset_and_undelete_are_distinct(self):
        """A23: only explicit undelete revives a tombstoned base entry."""
        r = Registry({"a": "base"}, {"a": "overlay"}, {"a"})
        r.reset("a")
        self.assertEqual(r.effective(), {})
        r.undelete("a")
        self.assertEqual(r.effective(), {"a": "base"})

    def test_98_accepted_values_are_deduplicated(self):
        """A2: repeated provenance records do not duplicate accepted set values."""
        s = self.with_values(self.value(), self.value(), self.value(source="manifest"))
        self.assertDecision(self.decide(self.req(), s), "allow")
        self.assertEqual(model._values(s.members[0], self.categories["audience"]), {"general"})

    def test_99_explicit_mapping_required(self):
        """A11: outside raw values require an owner-specified mapping."""
        with self.assertRaises(PolicyError):
            accept_evidence(Assertion("audience", "raw", "manifest"), observed_at="date",
                            accepted_at="date", mapping_version="mapping@1", **self.auth)

    def test_100_duplicate_row_ids_rejected(self):
        """A4: a waiver cannot ambiguously name multiple rows."""
        p = self.req()
        p = replace(p, require=p.require + p.require)
        self.assertDecision(self.decide(p), "deny", "POLICY_UNAVAILABLE")

    def test_101_deny_exists_is_unsupported(self):
        """v3 deny supports in/missing only."""
        self.assertDecision(self.decide(self.block(op="exists", values=())), "deny", "POLICY_UNAVAILABLE")

    def test_102_deny_missing_known_value_does_not_block(self):
        """A3: accepted presence makes deny-missing false."""
        self.assertDecision(self.decide(self.block(op="missing", values=()), self.with_values(self.value())), "allow")

    def test_103_deny_known_nonmatching_value_does_not_block(self):
        """R1: a deny proved false permits ordinary grant evaluation."""
        self.assertDecision(self.decide(self.block(), self.with_values(self.value())), "allow")

    def test_104_no_applicable_requirement_denies(self):
        """A1 ambiguity: model requires at least one applicable requirement target."""
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("route",))
        self.assertDecision(self.decide(self.req()), "deny", "NO_APPLICABLE_SUBJECT")

    def test_105_output_preview_uses_recorded_bundle(self):
        """A25/R4: preview includes retained outputs under their original bundle."""
        before = self.snapshot()
        after = self.snapshot("v2", apps={"app": (replace(self.app, grants=()),)})
        r = dict(self.request, phase="download", output="output@1")
        self.assertIn(r, change_preview(before, after, [r])["losses"])

    def test_106_unknown_value_configuration_rejected(self):
        """A22: candidate validation rejects unknown rule values."""
        version = self.snapshot(people={"person": (self.req(values=("unregistered",)),)})
        with self.assertRaises(PolicyError):
            validate_snapshot(version)

    def test_107_migration_missing_tags(self):
        """R3 fixture: omitted tags disclose the old bypass and new completeness gate."""
        self.assertMigration(dict(self.request, metadata_complete=False), self.snapshot(),
                             ("allow", "deny", "deny"), ("ALLOW", "METADATA_INCOMPLETE", "METADATA_INCOMPLETE"))

    def test_108_migration_none_tags(self):
        """R3 fixture: None tags are compared in all three modes."""
        self.assertMigration(dict(self.request, tags=None, metadata_complete=False), self.snapshot(),
                             ("allow", "deny", "deny"))

    def test_109_migration_empty_tags(self):
        """R3 fixture: empty tags are not considered complete legacy metadata."""
        self.assertMigration(dict(self.request, tags=[], metadata_complete=False), self.snapshot(),
                             ("allow", "deny", "deny"))

    def test_110_migration_allowed_and_disallowed_tags(self):
        """R3/A2 fixture: legacy OR passes mixed tags while v3 subset fails."""
        s = self.with_values(self.value(), self.value(value="adult"))
        v = self.snapshot(people={"person": (self.req(),)}, services={"choice@1": s})
        self.assertMigration(dict(self.request, tags=("general", "adult")), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "REQUIRE_MISMATCH"))

    def test_111_migration_unknown_category(self):
        """R3 fixture: v3 configuration error is separate from legacy tag results."""
        p = replace(self.plan, require=(Row("unknown-row", "unknown", "exists"),))
        v = self.snapshot(people={"person": (p,)})
        self.assertMigration(dict(self.request, tags=("general",)), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "POLICY_UNAVAILABLE"))

    def test_112_migration_multiple_assertions(self):
        """R3 fixture: separately sourced conflicting accepted assertions stay visible."""
        s = self.with_values(self.value(), self.value(value="adult", source="manifest"))
        v = self.snapshot(people={"person": (self.req(),)}, services={"choice@1": s})
        self.assertMigration(dict(self.request, tags=("general",)), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "REQUIRE_MISMATCH"))
        self.assertEqual(len(s.members[0].assertions), 2)

    def test_113_migration_person_app_mismatch(self):
        """R3 fixture: app mismatch cannot be rescued by the person's tag allow."""
        v = self.snapshot(apps={"app": (replace(self.app, grants=()),)})
        self.assertMigration(dict(self.request, tags=("general",)), v,
                             ("deny", "deny", "deny"), legacy_grants=False)

    def test_114_migration_empty_grants(self):
        """R3 fixture: empty grants deny in all three comparisons."""
        v = self.snapshot(people={"person": (replace(self.plan, grants=()),)})
        self.assertMigration(dict(self.request, tags=("general",)), v,
                             ("deny", "deny", "deny"), ("NO_GRANT", "NO_GRANT", "NO_GRANT"), legacy_grants=False)

    def test_115_migration_route_change(self):
        """R3/R2 fixture: changed selected route is a new service revision."""
        new = replace(self.service, id="choice@2", members=self.service.members + (Subject("route@2", "route"),))
        v = self.snapshot(services={"choice@2": new})
        self.assertMigration(dict(self.request, service="choice@2", tags=("general",)), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "NO_GRANT"))

    def test_116_migration_helpers(self):
        """R3/A1 fixture: root tags cannot cover a helper's missing accepted facts."""
        helper = Service("helper@1", (Subject("helper-release@1", "release"),))
        s = replace(self.with_values(self.value()), dependencies=(helper,))
        v = self.snapshot(people={"person": (self.req(),)}, services={"choice@1": s})
        self.assertMigration(dict(self.request, tags=("general",)), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "REQUIRE_MISSING"))

    def test_117_migration_retry(self):
        """R3/R4 fixture: retries reauthorize rather than reuse dispatch approval."""
        before = self.snapshot()
        self.assertDecision(decision_for(before, self.request), "allow")
        after = replace(before, id="v2", people={"person": (replace(self.plan, grants=()),)})
        self.assertMigration(dict(self.request, tags=("general",), phase="retry"), after,
                             ("deny", "deny", "deny"), legacy_grants=False)

    def test_118_migration_cache_rechecks_current_policy(self):
        """R3/A22: a cache boundary evaluates current policy, not old approval."""
        before = self.snapshot()
        self.assertDecision(decision_for(before, dict(self.request, phase="cache")), "allow")
        after = self.snapshot("v2", people={"person": (self.req(),)})
        self.assertMigration(dict(self.request, tags=("general",), phase="cache"), after,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "REQUIRE_MISSING"))

    def test_119_migration_output(self):
        """R3/R4 fixture: output release rechecks the recorded closure with current policy."""
        v = self.snapshot(people={"person": (self.req(),)})
        self.assertMigration(dict(self.request, tags=("general",), phase="release", output="output@1"), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "REQUIRE_MISSING"))

    def test_120_migration_share(self):
        """R3/R4 fixture: output sharing has the same deny-on-unknown boundary."""
        v = self.snapshot(people={"person": (self.block(),)})
        self.assertMigration(dict(self.request, tags=("general",), phase="share", output="output@1"), v,
                             ("allow", "allow", "deny"), ("ALLOW", "ALLOW", "DENY_INPUT_UNKNOWN"))

    def test_121_legacy_mode_preserves_any_tag_or(self):
        """R3: explicit legacy mode preserves any-tag OR with complete metadata."""
        p = replace(self.plan, mode="legacy", legacy_tags=("general",))
        self.assertDecision(self.decide(p, tags=("general", "adult")), "allow")

    def test_122_legacy_mode_gate_is_mandatory(self):
        """R3: legacy runtime missing tags is blocked even with a broad grant."""
        p = replace(self.plan, mode="legacy", legacy_tags=("general",))
        self.assertDecision(self.decide(p), "deny", "METADATA_INCOMPLETE")

    def test_123_legacy_conversion_not_implicit(self):
        """R3: mixing legacy tags and category rows is invalid configuration."""
        p = replace(self.req(), mode="legacy", legacy_tags=("general",))
        self.assertDecision(self.decide(p, tags=("general",)), "deny", "POLICY_UNAVAILABLE")

    def test_124_legacy_conversion_previewed_per_plan(self):
        """R3: a proposed conversion discloses the specific plan's losses."""
        legacy = replace(self.plan, mode="legacy", legacy_tags=("general",))
        s = self.with_values(self.value(), self.value(value="adult"))
        before = self.snapshot(people={"person": (legacy,)}, services={"choice@1": s})
        request = dict(self.request, tags=("general", "adult"))
        after = replace(before, id="v2", people={"person": (self.req(),)})
        self.assertIn(request, change_preview(before, after, [request])["losses"])
        self.assertDecision(decision_for(before, request), "allow")
        self.assertDecision(decision_for(after, request), "deny", "REQUIRE_MISMATCH")

    def test_125_preview_inventory_includes_known_routes_and_outputs(self):
        """A25: change preview expands beyond one caller-supplied example."""
        result = change_preview(self.snapshot(), self.snapshot("v2"), [])
        self.assertTrue(any(r.get("phase") == "helper" for r in result["scope"]))
        self.assertTrue(any(r.get("phase") == "download" and r.get("output") == "output@1"
                            for r in result["scope"]))
        self.assertTrue(any(r.get("phase") == "retry" for r in result["scope"]))

    def test_127_base_and_effective_views_are_read_only_copies(self):
        """A23: editing an exposed view never mutates bundled base."""
        r = Registry({"a": {"value": "base"}})
        r.defaults["a"]["value"] = "changed"
        r.effective()["a"]["value"] = "changed"
        self.assertEqual(r.effective(), {"a": {"value": "base"}})

    def test_128_default_reset_preview_can_lose_access(self):
        """A23/A25: a draft reset discloses losses without mutating the before value."""
        registry = Registry({"person": (replace(self.plan, grants=()),)},
                            {"person": (self.plan,)})
        before = self.snapshot(people=registry.effective())
        registry.reset("person")
        after = self.snapshot("v2", people=registry.effective())
        self.assertIn(self.request, change_preview(before, after, [self.request])["losses"])
        self.assertDecision(decision_for(before, self.request), "allow")
        self.assertDecision(decision_for(after, self.request), "deny", "NO_GRANT")

    def test_129_default_undelete_preview_can_gain_access(self):
        """A23/A25: undelete is a separate draft change with disclosed gains."""
        registry = Registry({"person": (self.plan,)}, deleted={"person"})
        before = self.snapshot(people=registry.effective())
        registry.undelete("person")
        after = self.snapshot("v2", people=registry.effective())
        self.assertIn(self.request, change_preview(before, after, [self.request])["gains"])
        self.assertDecision(decision_for(before, self.request), "deny", "IDENTITY_UNAVAILABLE")
        self.assertDecision(decision_for(after, self.request), "allow")

    def test_131_evidence_age_does_not_change_acceptance(self):
        """A24: accepted evidence age does not expire access."""
        old = replace(self.value(), observed_at="1999-01-01")
        self.assertDecision(self.decide(self.req(), self.with_values(old)), "allow")

    def test_133_snapshot_config_errors_precede_content_denies(self):
        """Errors: invalid later rows are not hidden behind an earlier content denial."""
        p = replace(self.block(), require=(Row("bad", "unknown", "exists"),))
        self.assertDecision(self.decide(p, self.with_values(self.value(value="adult"))), "deny", "POLICY_UNAVAILABLE")

    def test_134_subject_applicability_is_enforced(self):
        """A7: a release-only assertion on a route is malformed accepted data."""
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("release",))
        s = replace(self.service, members=self.service.members + (Subject("route@1", "route", (self.value(),)),))
        self.assertDecision(self.decide(service=s), "deny", "POLICY_UNAVAILABLE")

    def test_135_workflow_optional_selector_is_root_only(self):
        """A9: workflow selectors also bind the picked root."""
        s = replace(self.service, members=(Subject("workflow@1", "workflow"), Subject("helper-workflow@1", "workflow")))
        p = replace(self.plan, grants=({"capability": "generate", "service": "choice@1", "workflow": "helper-workflow@1"},))
        self.assertDecision(self.decide(p, s), "deny", "NO_GRANT")


    def test_136_typed_grant_matches_root(self):
        plan = replace(self.plan, grants=(Grant("generate", "choice@1", release="release@1"),))
        self.assertDecision(self.decide(plan), "allow")
        self.assertDecision(self.decide(replace(plan, grants=(
            Grant("generate", "choice@1", workflow="workflow@1"),))), "deny", "NO_GRANT")

    def test_137_preview_as_uses_runtime_explanations(self):
        version = self.snapshot(people={"person": (self.req(),)})
        result = preview_as(version, "person", "app", "generate")
        self.assertEqual(result["choice@1"], decision_for(version, self.request))
        self.assertEqual((result["choice@1"].rule, result["choice@1"].member),
                         ("person-plan:audience-rule", "release@1"))

    def test_138_preview_as_recorded_output_uses_current_policy(self):
        version = self.snapshot(apps={"app": (replace(self.app, grants=()),)})
        result = preview_as(version, "person", "app", "generate", phase="download")
        self.assertEqual(set(result), {"output@1"})
        self.assertDecision(result["output@1"], "deny", "NO_GRANT")

    def test_139_preview_as_missing_identity_is_denied_per_service(self):
        result = preview_as(self.snapshot(), "unknown", "app", "generate")
        self.assertDecision(result["choice@1"], "deny", "IDENTITY_UNAVAILABLE")
        self.assertEqual(preview_as(None, "person", "app", "generate"), {})

    def test_140_plan_inputs_cannot_mutate_grants_or_waivers(self):
        grant = {"capability": "generate", "service": "choice@1"}
        missing = {"audience-rule": "allow"}
        plan = self.req(grants=[grant], missing=missing)
        grant["service"] = "choice@2"
        missing.clear()
        self.assertDecision(self.decide(plan), "allow")
        with self.assertRaises(TypeError):
            plan.grants[0]["service"] = "choice@2"
        with self.assertRaises(TypeError):
            plan.missing["audience-rule"] = "hold"

    def test_141_sequence_inputs_are_defensively_frozen(self):
        values = ["general"]
        category = Category("audience", "Audience", values=values)
        values.append("adult")
        self.assertEqual(category.values, ("general",))
        members = [Subject("release@1", "release")]
        service = Service("choice@1", members)
        members.clear()
        self.assertDecision(self.decide(service=service), "allow")

    def test_142_conflicting_service_revision_in_closure_fails_closed(self):
        helper = Service("helper@1", (Subject("helper-release@1", "release"),))
        changed = replace(helper, members=helper.members + (Subject("route@1", "route"),))
        service = replace(self.service, dependencies=(helper, changed))
        self.assertDecision(self.decide(service=service), "deny", "POLICY_UNAVAILABLE")

    def test_143_conflicting_subject_revision_in_closure_fails_closed(self):
        service = replace(self.service, members=(
            Subject("release@1", "release", (self.value(),)),
            Subject("release@1", "release", (self.value(value="adult"),))))
        self.assertDecision(self.decide(service=service), "deny", "POLICY_UNAVAILABLE")

    def test_144_identical_shared_dependency_is_deduplicated(self):
        helper = Service("helper@1", (Subject("helper-release@1", "release"),))
        service = replace(self.service, dependencies=(helper, helper))
        self.assertEqual(tuple(s.id for s in model.resolve(service)),
                         ("release@1", "helper-release@1"))
        self.assertDecision(self.decide(service=service), "allow")

    def test_145_waivers_survive_later_plan_grant_failure(self):
        plan = self.req(missing={"audience-rule": "allow"})
        self.app = replace(self.app, grants=())
        decision = self.decide(plan)
        self.assertDecision(decision, "deny", "NO_GRANT")
        self.assertEqual(decision.waivers,
                         (("person-plan", "audience-rule", "release@1"),))

    def test_146_waivers_survive_later_legacy_failure(self):
        plan = self.req(missing={"audience-rule": "allow"})
        self.app = replace(self.app, mode="legacy", legacy_tags=("general",))
        decision = self.decide(plan, tags=("adult",))
        self.assertDecision(decision, "deny", "LEGACY_TAG_MISMATCH")
        self.assertEqual(decision.waivers,
                         (("person-plan", "audience-rule", "release@1"),))

    def test_147_completeness_flags_require_booleans(self):
        for flag in ("false", 1, None):
            with self.subTest(flag=flag):
                self.assertDecision(self.decide(metadata_complete=flag),
                                    "deny", "POLICY_UNAVAILABLE")
                self.assertDecision(self.decide(completeness_gate=flag),
                                    "deny", "POLICY_UNAVAILABLE")
                self.assertDecision(legacy_gated((), ("general",), metadata_complete=flag),
                                    "deny", "POLICY_UNAVAILABLE")

    def test_148_acceptance_flag_requires_boolean(self):
        assertion = replace(self.value(), accepted="false")
        self.assertDecision(self.decide(service=self.with_values(assertion)),
                            "deny", "POLICY_UNAVAILABLE")

    def test_149_all_provenance_fields_are_nonempty_strings(self):
        for field in ("source", "observed_at", "accepted_at", "accepting_owner", "mapping_version"):
            for invalid in ("", 1):
                with self.subTest(field=field, invalid=invalid):
                    assertion = replace(self.value(), **{field: invalid})
                    self.assertDecision(self.decide(service=self.with_values(assertion)),
                                        "deny", "POLICY_UNAVAILABLE")

    def test_150_deny_without_applicable_subject_is_false(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("route",))
        self.assertDecision(self.decide(self.block()), "allow")

    def test_151_empty_deny_candidates_still_unknown_when_missing(self):
        self.assertDecision(self.decide(self.block(values=())), "deny", "DENY_INPUT_UNKNOWN")
        self.assertDecision(self.decide(self.block(values=()), self.with_values(self.value())), "allow")

    def test_152_deny_order_is_plan_row_and_member_order(self):
        service = replace(self.service, members=(
            Subject("release@1", "release"),
            Subject("release@2", "release", (self.value(value="adult"),))))
        decision = self.decide(self.block(), service)
        self.assertEqual((decision.reason, decision.rule, decision.member),
                         ("DENY_INPUT_UNKNOWN", "person-plan:audience-deny", "release@1"))

    def test_153_every_declared_boundary_uses_same_evaluator(self):
        for phase in sorted(model.BOUNDARIES):
            with self.subTest(phase=phase):
                self.assertDecision(self.decide(self.req(), phase=phase), "deny", "REQUIRE_MISSING")
        self.assertDecision(self.decide(phase="unknown"), "deny", "POLICY_UNAVAILABLE")

    def test_154_category_applicability_skips_inapplicable_members(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("release",))
        service = replace(self.with_values(self.value()),
                          members=self.with_values(self.value()).members + (Subject("route@1", "route"),))
        self.assertDecision(self.decide(self.req(), service), "allow")

    def test_155_presence_operators_reject_candidate_values(self):
        for op in ("exists", "missing"):
            with self.subTest(op=op):
                self.assertDecision(self.decide(self.req(op=op)), "deny", "POLICY_UNAVAILABLE")

    def test_156_legacy_empty_allowlist_and_malformed_inputs(self):
        self.assertDecision(legacy_exact((), ("adult",)), "allow")
        self.assertDecision(legacy_gated((), ("adult",)), "allow")
        self.assertDecision(legacy_gated((), ()), "deny", "METADATA_INCOMPLETE")
        self.assertDecision(legacy_exact(("general",), ("adult",)), "deny", "LEGACY_TAG_MISMATCH")
        self.assertDecision(legacy_exact((), grants="yes"), "deny", "POLICY_UNAVAILABLE")

    def test_157_preview_scope_is_defensively_copied(self):
        request = dict(self.request, tags=["general"])
        result = change_preview(self.snapshot(), self.snapshot("v2"), [request])
        request["tags"].append("adult")
        self.assertEqual(result["scope"][0]["tags"], ["general"])

    def test_158_preview_reports_waiver_only_explanation_change(self):
        before = self.snapshot()
        after = self.snapshot("v2", people={"person": (
            self.req(missing={"audience-rule": "allow"}),)})
        result = change_preview(before, after, [self.request])
        self.assertEqual((result["gains"], result["losses"]), ([], []))
        change = result["reason_changes"][0]
        self.assertEqual(change["before"].waivers, ())
        self.assertTrue(change["after"].waivers)

    def test_159_snapshot_validation_rejects_conflicting_catalog_closures(self):
        changed = replace(self.service, members=self.service.members + (Subject("route@1", "route"),))
        version = self.snapshot(retained={"output@1": changed})
        with self.assertRaises(PolicyError):
            validate_snapshot(version)

    def test_160_snapshot_lookup_key_must_match_service_revision(self):
        version = self.snapshot(services={"choice@1": replace(self.service, id="choice@2")})
        self.assertDecision(decision_for(version, self.request), "deny", "POLICY_UNAVAILABLE")

    def test_161_ids_are_strings_and_never_coerced(self):
        self.assertDecision(self.decide(replace(self.plan, id=1)), "deny", "POLICY_UNAVAILABLE")
        self.assertDecision(self.decide(person=1), "deny", "IDENTITY_UNAVAILABLE")
        self.categories["audience"] = replace(self.categories["audience"], id=1)
        self.assertDecision(self.decide(), "deny", "POLICY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
