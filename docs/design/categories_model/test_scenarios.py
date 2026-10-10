"""Named plain-value scenarios; owner runs python -m unittest discover docs/design/categories_model."""
import unittest
from dataclasses import replace

from model import (
    Assertion, Category, Classifier, Decision, HeldItem, History, OutputCheck,
    Plan, PolicyVersion, Registry, Row, Service, StandingRule, Subject,
    apply_evidence, change_preview, derive, evaluate, inherit_owner_deny, resolve,
)


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.categories = {
            "audience": Category("audience", "Audience", "ordered",
                                 ("general", "teen", "adult")),
            "location": Category("location", "Location", "set", ("local", "cloud")),
            "training": Category("training", "Training", "boolean"),
            "cost": Category("cost", "Cost", "number"),
            "licence": Category("licence", "Licence", "set",
                                ("commercial", "non-commercial"), restriction=True),
        }
        self.plan = Plan("person", ({"capability": "generate"},))
        self.app = Plan("app", ({"capability": "generate"},))
        self.service = Service("choice", (Subject("release@1", "release"),))

    def value(self, category, value, source="owner", standing=(), kind="release"):
        return apply_evidence(Assertion(category, value, source), kind, standing)

    def with_values(self, *values):
        return replace(self.service, members=(Subject("release@1", "release", values),))

    def decide(self, plan=None, service=None, **kwargs):
        return evaluate(self.categories, (plan or self.plan, self.app),
                        service or self.service, "generate", **kwargs)

    def assertDecision(self, actual, outcome, contains="", status="decided"):
        self.assertEqual(actual.outcome, outcome)
        self.assertEqual(actual.status, status)
        self.assertTrue(actual.reason)
        self.assertIn(contains.lower(), actual.reason.lower())
        if outcome == "deny":
            self.assertTrue(actual.rule)

    def test_01_matching_grant_allows(self):
        self.assertDecision(self.decide(), "allow")

    def test_02_empty_grant_grants_nothing(self):
        self.assertDecision(self.decide(replace(self.plan, grants=())), "deny", "no matching grant")

    def test_03_deny_wins_over_grant(self):
        plan = replace(self.plan, deny=(Row("audience", "in", ("teen",)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "teen"))),
                            "deny", "denied by")

    def test_04_unknown_value_is_not_a_match(self):
        plan = replace(self.plan, require=(Row("audience", "in", ("general",)),))
        self.assertDecision(self.decide(plan), "deny", "not reviewed for Audience")

    def test_05_explicit_missing_allow(self):
        plan = replace(self.plan, require=(Row("audience", "at_most", ("general",)),),
                       missing={"audience": "allow"})
        self.assertDecision(self.decide(plan), "allow")

    def test_06_missing_allow_never_bypasses_known_mismatch(self):
        plan = replace(self.plan, require=(Row("audience", "at_most", ("general",)),),
                       missing={"audience": "allow"})
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "adult"))),
                            "deny", "did not match")

    def test_07_missing_hold_is_output_only(self):
        plan = replace(self.plan, require=(Row("audience", "exists"),),
                       missing={"audience": "hold"})
        self.assertDecision(self.decide(plan), "deny", "not reviewed")

    def test_08_missing_output_hold_is_explicit_status(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        plan = replace(self.plan, require=(Row("audience", "exists"),),
                       missing={"audience": "hold"})
        self.assertDecision(self.decide(plan, phase="release", output=Subject("result", "output")),
                            "deny", "held", "held")

    def test_09_require_categories_are_and(self):
        plan = replace(self.plan, require=(Row("audience", "in", ("teen",)),
                                          Row("location", "in", ("local",))))
        service = self.with_values(self.value("audience", "teen"), self.value("location", "cloud"))
        self.assertDecision(self.decide(plan, service), "deny", "Location")

    def test_10_values_within_row_are_or(self):
        plan = replace(self.plan, require=(Row("audience", "in", ("general", "teen")),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "teen"))), "allow")

    def test_11_person_and_app_intersect(self):
        self.app = replace(self.app, grants=())
        self.assertDecision(self.decide(), "deny", "app")

    def test_12_app_deny_wins_over_person_grant(self):
        self.app = replace(self.app, deny=(Row("location", "in", ("cloud",)),))
        self.assertDecision(self.decide(service=self.with_values(self.value("location", "cloud"))),
                            "deny", "app")

    def test_13_unknown_category_is_operational_error(self):
        plan = replace(self.plan, require=(Row("unknown", "exists"),))
        self.assertDecision(self.decide(plan), "deny", "policy could not be evaluated")

    def test_14_type_mismatch_is_operational_error(self):
        self.assertDecision(self.decide(service=self.with_values(self.value("cost", True))),
                            "deny", "policy could not be evaluated")

    def test_15_broken_bundle_is_operational_error(self):
        self.assertDecision(self.decide(service=replace(self.service, members=())),
                            "deny", "policy could not be evaluated")

    def test_16_unaccepted_evidence_never_grants(self):
        plan = replace(self.plan, require=(Row("audience", "exists"),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "general", "import"))),
                            "deny", "not reviewed")

    def test_17_unaccepted_evidence_never_denies(self):
        plan = replace(self.plan, deny=(Row("audience", "in", ("adult",)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "adult", "import"))),
                            "allow")

    def test_18_standing_rule_accepts_and_records_source(self):
        rule = StandingRule("accept-source", "manifest", "licence", ("release",))
        assertion = self.value("licence", "commercial", "manifest", (rule,))
        self.assertEqual((assertion.source, assertion.rule, assertion.accepted),
                         ("manifest", "accept-source", True))
        plan = replace(self.plan, require=(Row("licence", "in", ("commercial",)),))
        self.assertDecision(self.decide(plan, self.with_values(assertion)), "allow")

    def test_19_standing_rule_must_name_source(self):
        rule = StandingRule("accept-source", "manifest", "licence", ("release",))
        self.assertFalse(self.value("licence", "commercial", "classifier", (rule,)).accepted)

    def test_20_request_attribute_cannot_grant(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("request",))
        rule = StandingRule("attribute-rule", "request attribute", "audience", ("request",))
        assertion = self.value("audience", "general", "request attribute", (rule,), "request")
        service = Service("choice", (Subject("request-1", "request", (assertion,)),))
        plan = replace(self.plan, require=(Row("audience", "in", ("general",)),))
        self.assertDecision(self.decide(plan, service), "deny", "not reviewed")

    def output_case(self, **changes):
        classifier = Classifier("1", ("image",), ("en",), "whole image", 0.5, "hold", "local")
        check = OutputCheck("image", "en", "whole image", "pass", classifier)
        return self.decide(replace(self.plan, outputs=("generate",)), phase="release",
                           output=Subject("result", "output"), check=replace(check, **changes))

    def test_21_classifier_modality_gap_holds(self):
        self.assertDecision(self.output_case(modality="audio"), "deny", "cover", "held")

    def test_22_classifier_language_gap_holds(self):
        self.assertDecision(self.output_case(language="fa"), "deny", "cover", "held")

    def test_23_classifier_timeout_holds(self):
        self.assertDecision(self.output_case(result="timeout"), "deny", "timeout", "held")

    def test_24_classifier_pending_holds(self):
        self.assertDecision(self.output_case(result="pending"), "deny", "pending", "held")

    def test_25_classifier_pass_releases(self):
        self.assertDecision(self.output_case(), "allow")

    def test_26_classifier_failure_denies(self):
        self.assertDecision(self.output_case(result="fail"), "deny", "failed")

    def test_27_streaming_route_is_not_offered(self):
        plan = replace(self.plan, outputs=("generate",))
        self.assertDecision(self.decide(plan, replace(self.service, checkable=False)),
                            "deny", "not offered")

    def test_28_derived_licence_needs_review_not_audience(self):
        parent = Subject("parent@1", "release",
                         (self.value("licence", "non-commercial"), self.value("audience", "adult")))
        child = derive(parent, "child@1", self.categories)
        self.assertEqual(len(child.assertions), 1)
        self.assertEqual(child.assertions[0].category, "licence")
        self.assertTrue(child.assertions[0].needs_review)
        self.assertEqual(child.assertions[0].rule, "owner edit")
        self.assertDecision(self.decide(service=replace(self.service, members=(child,))),
                            "deny", "needs review")

    def test_29_child_grant_never_cancels_parent_deny(self):
        parent = inherit_owner_deny(self.service.members[0], Row("licence", "in", ("non-commercial",)))
        child = derive(parent, "child@1", self.categories)
        plan = replace(self.plan, grants=({"release": "child@1"},))
        self.assertDecision(self.decide(plan, replace(self.service, members=(child,))),
                            "deny", "inherited owner deny")

    def test_30_change_preview_lists_gainers_and_losers(self):
        before = PolicyVersion("old", self.categories,
                               {"one": self.plan, "two": replace(self.plan, grants=())},
                               {"app": self.app}, {"choice": self.service})
        after = replace(before, id="new", people={
            "one": replace(self.plan, grants=()), "two": self.plan})
        requests = [dict(person=p, app="app", service="choice", capability="generate")
                    for p in ("one", "two")]
        preview = change_preview(before, after, requests)
        self.assertEqual(preview, {"gains": [requests[1]], "losses": [requests[0]]})

    def test_31_reset_to_default_removes_override(self):
        registry = Registry({"a": "default"}, {"a": "override"})
        registry.reset("a")
        self.assertEqual(registry.effective(), {"a": "default"})

    def test_32_deleted_defaults_stay_deleted_after_reset_and_update(self):
        registry = Registry({"a": "default"}, {"a": "override"})
        registry.delete("a")
        registry.reset("a")
        registry.defaults = {"a": "new"}
        self.assertEqual(registry.effective(), {})

    def test_33_timer_never_removes_a_deny(self):
        plan = replace(self.plan, deny=(Row("audience", "in", ("adult",)),))
        registry = Registry({"rule": {"checked_on": "2020-01-01", "plan": plan}})
        self.assertEqual(registry.tick("2030-01-01"), {"rule": True})
        unchanged = registry.effective()["rule"]["plan"]
        self.assertEqual(unchanged, plan)
        self.assertDecision(self.decide(unchanged, self.with_values(self.value("audience", "adult"))),
                            "deny", "denied by")

    def test_34_any_tag_migration_does_not_allow_cloud_teen(self):
        # Legacy any(["local", "teen"]) would wrongly accept this cloud route.
        service = Service("choice", (Subject("route", "route", (
            self.value("audience", "teen", kind="route"),
            self.value("location", "cloud", kind="route"))),))
        plan = replace(self.plan, require=(Row("location", "in", ("local",)),
                                          Row("audience", "in", ("teen",))))
        self.assertDecision(self.decide(plan, service), "deny", "Location")

    def test_35_at_most_uses_owner_order(self):
        plan = replace(self.plan, require=(Row("audience", "at_most", ("teen",)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "general"))), "allow")

    def test_36_at_most_rejects_more_restricted_value(self):
        plan = replace(self.plan, require=(Row("audience", "at_most", ("teen",)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "adult"))), "deny")

    def test_37_at_most_on_number_is_error(self):
        self.assertDecision(self.decide(replace(self.plan, require=(Row("cost", "at_most", (5,)),))),
                            "deny", "policy could not be evaluated")

    def test_38_boolean_membership(self):
        plan = replace(self.plan, require=(Row("training", "in", (False,)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("training", False))), "allow")

    def test_39_number_membership(self):
        plan = replace(self.plan, require=(Row("cost", "in", (2.5, 3)),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("cost", 2.5))), "allow")

    def test_40_exists_requires_accepted_value(self):
        plan = replace(self.plan, require=(Row("cost", "exists"),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("cost", 0))), "allow")

    def test_41_missing_operator_explicitly_matches_absence(self):
        plan = replace(self.plan, require=(Row("cost", "missing"),))
        self.assertDecision(self.decide(plan), "allow")

    def test_42_missing_operator_rejects_present_value(self):
        plan = replace(self.plan, require=(Row("cost", "missing"),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("cost", 0))), "deny")

    def test_43_denies_do_not_match_unknown_values(self):
        plan = replace(self.plan, deny=(Row("cost", "in", (1,)),))
        self.assertDecision(self.decide(plan), "allow")

    def test_44_deny_missing_does_match_absence(self):
        plan = replace(self.plan, deny=(Row("cost", "missing"),))
        self.assertDecision(self.decide(plan), "deny", "denied by")

    def test_45_helper_cloud_route_is_in_bundle(self):
        root = self.with_values(self.value("location", "local"))
        root = replace(root, dependencies=("helper",))
        helper = Service("helper", (Subject("remote", "route",
                         (self.value("location", "cloud", kind="route"),)),))
        plan = replace(self.plan, require=(Row("location", "in", ("local",)),))
        self.assertDecision(self.decide(plan, root, registry={"helper": helper}), "deny", "Location")

    def test_46_bundle_deny_any_member_wins(self):
        service = replace(self.service, members=(
            Subject("a", "release", (self.value("audience", "general"),)),
            Subject("b", "release", (self.value("audience", "adult"),))))
        plan = replace(self.plan, deny=(Row("audience", "in", ("adult",)),))
        self.assertDecision(self.decide(plan, service), "deny")

    def test_47_conflicting_accepted_values_require_all_to_pass(self):
        service = self.with_values(self.value("audience", "general"), self.value("audience", "adult"))
        plan = replace(self.plan, require=(Row("audience", "in", ("general",)),))
        self.assertDecision(self.decide(plan, service), "deny")

    def test_48_dependency_cycle_denies_operationally(self):
        root = replace(self.service, dependencies=("choice",))
        self.assertDecision(self.decide(service=root, registry={"choice": root}),
                            "deny", "policy could not be evaluated")

    def test_49_policy_rechecked_at_retrieval(self):
        self.assertDecision(self.decide(), "allow")
        self.app = replace(self.app, grants=())
        self.assertDecision(self.decide(phase="retrieval", output=Subject("result", "output")), "deny", "app")

    def test_50_policy_rechecked_at_share(self):
        self.app = replace(self.app, grants=())
        self.assertDecision(self.decide(phase="share", output=Subject("result", "output")), "deny", "app")

    def test_51_history_rollback_restores_snapshot(self):
        history = History([{"plan": self.plan}])
        history.activate({"plan": replace(self.plan, grants=())})
        self.assertEqual(history.current()["plan"].grants, ())
        history.rollback()
        self.assertEqual(history.current()["plan"], self.plan)

    def test_52_standing_rule_kind_is_scoped(self):
        rule = StandingRule("accept", "manifest", "licence", ("output",))
        self.assertFalse(self.value("licence", "commercial", "manifest", (rule,)).accepted)

    def test_53_unaccepted_broken_data_is_inert(self):
        self.assertDecision(self.decide(service=self.with_values(Assertion("unknown", object(), "import"))),
                            "allow")

    def test_54_bad_provenance_is_operational_error(self):
        assertion = Assertion("cost", 1, "manifest", accepted=True)
        self.assertDecision(self.decide(service=self.with_values(assertion)),
                            "deny", "policy could not be evaluated")

    def test_55_classifier_unit_coverage_gap_holds(self):
        self.assertDecision(self.output_case(unit="text chunk"), "deny", "cover", "held")

    def test_56_missing_classifier_holds(self):
        self.assertDecision(self.output_case(classifier=None), "deny", "coverage", "held")

    def test_57_no_output_at_release_is_error(self):
        self.assertDecision(self.decide(phase="release"), "deny", "policy could not be evaluated")

    def test_58_empty_in_row_matches_nothing(self):
        plan = replace(self.plan, require=(Row("audience", "in"),))
        self.assertDecision(self.decide(plan, self.with_values(self.value("audience", "general"))), "deny")

    def test_59_first_failing_requirement_is_stable(self):
        plan = replace(self.plan, require=(Row("audience", "exists"), Row("cost", "exists")))
        result = self.decide(plan)
        self.assertEqual((result.rule, result.category), ("person:require:1", "audience"))

    def test_60_expired_held_item_never_releases(self):
        item = HeldItem(10, ("owner",))
        decision = Decision("deny", "Output held", "check", status="held")
        self.assertDecision(item.release(decision, 10, owner_action=True), "deny", "expired")

    def test_61_owner_can_release_a_current_hold(self):
        decision = Decision("deny", "Output held", "check", status="held")
        self.assertDecision(HeldItem(10, ("owner",)).release(decision, 9, owner_action=True), "allow")

    def test_62_owner_hold_release_cannot_override_hard_deny(self):
        decision = Decision("deny", "Denied by policy", "rule")
        self.assertDecision(HeldItem(10, ("owner",)).release(decision, 9, owner_action=True), "deny")

    def test_63_reviewer_visibility_is_explicit(self):
        item = HeldItem(10, ("reviewer",))
        self.assertTrue(item.visible_to("reviewer"))
        self.assertFalse(item.visible_to("stranger"))

    def test_64_output_evidence_needs_its_standing_rule(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        rule = StandingRule("classify", "classifier", "audience", ("output",))
        assertion = self.value("audience", "general", "classifier", (rule,), "output")
        plan = replace(self.plan, require=(Row("audience", "in", ("general",)),))
        self.assertDecision(self.decide(plan, phase="release",
                            output=Subject("result", "output", (assertion,))), "allow")

    def test_65_output_requirements_defer_at_dispatch(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        plan = replace(self.plan, require=(Row("audience", "exists"),), outputs=("generate",))
        self.assertDecision(self.decide(plan), "allow")

    def test_66_request_attribute_can_restrict(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("request",))
        rule = StandingRule("restrict", "request attribute", "audience", ("request",))
        assertion = self.value("audience", "adult", "request attribute", (rule,), "request")
        service = Service("choice", (Subject("request", "request", (assertion,)),))
        plan = replace(self.plan, deny=(Row("audience", "in", ("adult",)),))
        self.assertDecision(self.decide(plan, service), "deny")

    def test_67_missing_exception_does_not_override_deny(self):
        plan = replace(self.plan, deny=(Row("audience", "missing"),),
                       require=(Row("audience", "exists"),), missing={"audience": "allow"})
        self.assertDecision(self.decide(plan), "deny", "denied by")

    def test_68_classifier_timeout_precedes_missing_output_hold(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("output",))
        self.plan = replace(self.plan, require=(Row("audience", "exists"),),
                            missing={"audience": "hold"})
        self.assertDecision(self.output_case(result="timeout"), "deny", "timeout", "held")

    def test_69_request_attribute_cannot_remove_a_missing_deny(self):
        self.categories["audience"] = replace(self.categories["audience"], applies_to=("request",))
        rule = StandingRule("attribute", "request attribute", "audience", ("request",))
        assertion = self.value("audience", "general", "request attribute", (rule,), "request")
        service = Service("choice", (Subject("request", "request", (assertion,)),))
        plan = replace(self.plan, deny=(Row("audience", "missing"),))
        self.assertDecision(self.decide(plan, service), "deny")

    def test_70_hold_does_not_mask_another_hard_requirement(self):
        self.plan = replace(self.plan, require=(Row("cost", "exists"),))
        self.assertDecision(self.output_case(result="timeout"), "deny", "not reviewed")

    def test_71_unbuffered_helper_is_not_offered(self):
        helper = Service("helper", (Subject("route", "route"),), checkable=False)
        service = replace(self.service, dependencies=("helper",))
        plan = replace(self.plan, outputs=("generate",))
        self.assertDecision(self.decide(plan, service, registry={"helper": helper}),
                            "deny", "not offered")

    def test_72_missing_member_cannot_borrow_another_members_value(self):
        service = replace(self.service, members=(
            Subject("a", "release", (self.value("audience", "general"),)),
            Subject("b", "release")))
        plan = replace(self.plan, require=(Row("audience", "exists"),))
        self.assertDecision(self.decide(plan, service), "deny", "not reviewed")


if __name__ == "__main__":
    unittest.main()
