"""Executable draft semantics, not production authorization. See FINDINGS.md."""
from copy import deepcopy
from dataclasses import dataclass, field, replace
from math import isfinite


KINDS = {"release", "route", "workflow", "request", "output"}
SERVICE_KINDS = {"release", "route", "workflow"}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    rule: str = ""
    category: str = ""
    status: str = "decided"


def deny(reason, rule="", category="", held=False):
    return Decision("deny", reason, rule, category, "held" if held else "decided")


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    kind: str
    values: tuple = ()
    applies_to: tuple = ("service",)
    restriction: bool = False  # Owner-supplied legal/administrative classification.

    def validate(self):
        if self.kind not in {"ordered", "set", "boolean", "number"}:
            raise PolicyError("unknown category type")
        if not self.id or not self.label or not self.applies_to:
            raise PolicyError("incomplete category")
        if any(x not in KINDS | {"service"} for x in self.applies_to):
            raise PolicyError("unknown applicability")
        if self.kind == "ordered" and not self.values:
            raise PolicyError("ordered category needs values")
        if len(set(self.values)) != len(self.values):
            raise PolicyError("duplicate category values")

    def accepts(self, value):
        if self.kind in {"ordered", "set"}:
            return isinstance(value, str) and value in self.values
        if self.kind == "boolean":
            return type(value) is bool
        return type(value) in (int, float) and isfinite(value)

    def applies(self, subject):
        return subject.kind in self.applies_to or (
            "service" in self.applies_to and subject.kind in SERVICE_KINDS)


@dataclass(frozen=True)
class Assertion:
    category: str
    value: object
    source: str
    rule: str | None = None
    accepted: bool = False
    needs_review: bool = False
    checked_on: str | None = None


@dataclass(frozen=True)
class StandingRule:
    id: str
    source: str
    category: str
    subject_kinds: tuple


def apply_evidence(evidence, subject_kind, standing=()):
    """Owner actions apply immediately. Only exact, named standing rules accept evidence."""
    if evidence.source == "owner":
        return replace(evidence, accepted=True, rule="owner edit")
    for rule in standing:
        if (rule.source == evidence.source and rule.category == evidence.category
                and subject_kind in rule.subject_kinds and rule.id):
            return replace(evidence, accepted=True, rule=rule.id)
    return replace(evidence, accepted=False, rule=None)


@dataclass(frozen=True)
class Subject:
    id: str
    kind: str
    assertions: tuple = ()
    inherited_denies: tuple = ()


@dataclass(frozen=True)
class Service:
    id: str
    members: tuple
    dependencies: tuple = ()
    checkable: bool = True


def resolve(card, registry, stack=()):
    """Resolve the actual selected routes and all helper/dependency calls."""
    if card.id in stack:
        raise PolicyError("dependency cycle")
    if not card.members:
        raise PolicyError("empty resolved service")
    result = list(card.members)
    for key in card.dependencies:
        if key not in registry:
            raise PolicyError("unresolved dependency")
        result.extend(resolve(registry[key], registry, stack + (card.id,)))
    return tuple(result)


@dataclass(frozen=True)
class Row:
    category: str
    op: str
    values: tuple = ()


@dataclass(frozen=True)
class Plan:
    id: str
    grants: tuple = ()  # OR of selector dictionaries; AND within a selector.
    require: tuple = ()
    deny: tuple = ()
    missing: dict = field(default_factory=dict)
    outputs: tuple = ()  # Capabilities requiring check_before_release.


@dataclass(frozen=True)
class Classifier:
    version: str
    modalities: tuple
    languages: tuple
    unit: str
    threshold: float
    timeout_behavior: str
    sends_to: str


@dataclass(frozen=True)
class OutputCheck:
    modality: str
    language: str
    unit: str
    result: str = "pending"  # pass | fail | pending | timeout
    classifier: Classifier | None = None


def _validate_row(row, categories):
    if row.category not in categories:
        raise PolicyError("unknown category " + row.category)
    category = categories[row.category]
    if row.op not in {"in", "at_most", "exists", "missing"}:
        raise PolicyError("unknown operator")
    if row.op == "at_most" and (category.kind != "ordered" or len(row.values) != 1):
        raise PolicyError("at_most requires one ordered value")
    if row.op in {"exists", "missing"} and row.values:
        raise PolicyError("presence operator cannot have values")
    if any(not category.accepts(x) for x in row.values):
        raise PolicyError("rule value type mismatch")


def _values(subject, category, positive):
    return [a.value for a in subject.assertions
            if a.accepted and a.category == category.id
            and not a.needs_review
            and not (positive and a.source == "request attribute")]


def _match(row, values, category, universal):
    if row.op == "missing":
        return not values
    if row.op == "exists":
        return bool(values)
    if not values:
        return False
    matches = [v in row.values if row.op == "in" else
               category.values.index(v) <= category.values.index(row.values[0])
               for v in values]
    return all(matches) if universal else any(matches)


def _validate(categories, plans, subjects):
    for key, category in categories.items():
        category.validate()
        if key != category.id:
            raise PolicyError("category key mismatch")
    for subject in subjects:
        if not subject.id or subject.kind not in KINDS:
            raise PolicyError("broken subject")
        for assertion in subject.assertions:
            if not assertion.accepted:
                continue  # Evidence is inert, even if malformed.
            category = categories.get(assertion.category)
            if category is None or not category.accepts(assertion.value):
                raise PolicyError("assertion type or category mismatch")
            if not category.applies(subject) or not assertion.rule:
                raise PolicyError("assertion applicability or provenance mismatch")
        for row in subject.inherited_denies:
            _validate_row(row, categories)
    for plan in plans:
        if not plan.id:
            raise PolicyError("plan has no identity")
        for selector in plan.grants:
            if (not isinstance(selector, dict) or not selector
                    or any(k not in {"capability", "service", "release", "workflow"}
                           or not isinstance(v, str) for k, v in selector.items())):
                raise PolicyError("broken grant selector")
        for row in plan.require + plan.deny:
            _validate_row(row, categories)
        for key, mode in plan.missing.items():
            if key not in categories or mode not in {"allow", "hold"}:
                raise PolicyError("invalid missing exception")


def _release_check(check):
    classifier = check.classifier if check else None
    if classifier is None:
        return deny("Output held: no classifier coverage record",
                    "check_before_release", held=True)
    if (not classifier.version or not classifier.unit or not classifier.sends_to
            or not classifier.timeout_behavior
            or type(classifier.threshold) not in (int, float)
            or not isfinite(classifier.threshold)):
        raise PolicyError("incomplete classifier coverage record")
    if (check.modality not in classifier.modalities
            or check.language not in classifier.languages
            or check.unit != classifier.unit):
        return deny("Output held: classifier does not cover this unit",
                    "check_before_release", held=True)
    if check.result in {"timeout", "pending"}:
        return deny("Output held: classifier " + check.result,
                    "check_before_release", held=True)
    if check.result == "fail":
        return deny("Output failed the release check", "check_before_release")
    if check.result != "pass":
        raise PolicyError("unknown classifier result")
    return None


def evaluate(categories, plans, service, capability, registry=None,
             phase="dispatch", output=None, check=None):
    """Re-evaluate with current policy at dispatch/release/retrieval/share.
    Output checks are caller-supplied trusted runner results; this model runs no classifier.
    A hold is deny + status=held, rather than an implicit third access outcome.
    """
    try:
        if phase not in {"dispatch", "release", "retrieval", "share"}:
            raise PolicyError("unknown enforcement point")
        registry = registry or {}
        subjects = resolve(service, registry)
        if output is not None:
            if output.kind != "output":
                raise PolicyError("release target is not an output")
            subjects += (output,)
        if phase != "dispatch" and output is None:
            raise PolicyError("output required at release boundary")
        _validate(categories, plans, subjects)
        if not plans:
            return deny("No person and app policy supplied", "intersection")
        # Validate everything first; explicit denies precede grants and requirements.
        for subject in subjects:
            if subject.inherited_denies:
                row = subject.inherited_denies[0]
                return deny("Inherited owner deny needs review on " + subject.id,
                            "parent deny", row.category)
        for plan in plans:
            for index, row in enumerate(plan.deny):
                category = categories[row.category]
                if any(_match(row, _values(s, category, row.op == "missing"), category, False)
                       for s in subjects if category.applies(s)):
                    return deny("Denied by " + plan.id + " rule " + str(index + 1),
                                plan.id + ":deny:" + str(index + 1), category.id)
        for subject in subjects:
            for assertion in subject.assertions:
                if assertion.accepted and assertion.needs_review:
                    return deny("Inherited restriction needs review on " + subject.id,
                                assertion.rule or "lineage", assertion.category)
        for plan in plans:
            selectors = {"capability": {capability}, "service": {service.id},
                         "release": {s.id for s in subjects if s.kind == "release"},
                         "workflow": {s.id for s in subjects if s.kind == "workflow"}}
            if not any(all(v in selectors[k] for k, v in selector.items())
                       for selector in plan.grants):
                return deny("No matching grant in " + plan.id, plan.id + ":grant")
        needs_check = any(capability in plan.outputs for plan in plans)
        if needs_check:
            def checkable(card, stack=()):
                if card.id in stack:
                    raise PolicyError("dependency cycle")
                return card.checkable and all(
                    checkable(registry[k], stack + (card.id,)) for k in card.dependencies)
            if not checkable(service):
                return deny("Route cannot be checked before release; not offered",
                            "check_before_release")
        held = None
        if needs_check and phase != "dispatch":
            release_result = _release_check(check)
            if release_result is not None:
                if release_result.status != "held":
                    return release_result
                held = release_result
        for plan in plans:
            for index, row in enumerate(plan.require):
                category = categories[row.category]
                targets = [s for s in subjects if category.applies(s)]
                # Output-only rules are deferred until a release boundary.
                if not targets and phase == "dispatch" and category.applies_to == ("output",):
                    continue
                if not targets:
                    return deny("No applicable subject for " + category.label,
                                plan.id + ":require:" + str(index + 1), category.id)
                for subject in targets:
                    values = _values(subject, category, True)
                    if _match(row, values, category, True):
                        continue
                    rule_id = plan.id + ":require:" + str(index + 1)
                    if not values and row.op != "missing":
                        mode = plan.missing.get(category.id)
                        if mode == "allow":
                            continue
                        if mode == "hold" and subject.kind == "output":
                            if held is None:
                                held = deny("Output held: not reviewed for " + category.label,
                                            rule_id, category.id, True)
                            continue
                        return deny("Not reviewed for " + category.label,
                                    rule_id, category.id)
                    return deny("Required " + category.label + " rule did not match",
                                rule_id, category.id)
        return held or Decision("allow", "All grants, requirements and release checks passed")
    except (PolicyError, TypeError, ValueError, KeyError, AttributeError, IndexError) as error:
        return deny("Policy could not be evaluated: " + str(error), "evaluation error")


def derive(parent, child_id, categories):
    """Do not infer legal meaning from names; owner classifies restrictions."""
    carried = tuple(replace(a, needs_review=True)
                    for a in parent.assertions
                    if a.accepted and categories[a.category].restriction)
    return Subject(child_id, "release", carried, parent.inherited_denies)


def inherit_owner_deny(subject, row):
    return replace(subject, inherited_denies=subject.inherited_denies + (row,))


@dataclass(frozen=True)
class PolicyVersion:
    id: str
    categories: dict
    people: dict
    apps: dict
    services: dict


def change_preview(before, after, requests):
    """Requests are plain {person, app, service, capability} values."""
    result = {"gains": [], "losses": []}
    for request in requests:
        def decision(version):
            try:
                plans = (version.people[request["person"]], version.apps[request["app"]])
                return evaluate(version.categories, plans,
                                version.services[request["service"]], request["capability"],
                                version.services)
            except KeyError:
                return deny("Policy could not be evaluated: missing identity", "evaluation error")
        old, new = decision(before), decision(after)
        if old.outcome != new.outcome:
            result["gains" if new.outcome == "allow" else "losses"].append(dict(request))
    return result


@dataclass
class Registry:
    defaults: dict
    overrides: dict = field(default_factory=dict)
    deleted: set = field(default_factory=set)

    def effective(self):
        return {k: deepcopy(self.overrides.get(k, value))
                for k, value in (self.defaults | self.overrides).items()
                if k not in self.deleted}

    def reset(self, key):
        self.overrides.pop(key, None)  # Reset never clears a tombstone.

    def delete(self, key):
        self.deleted.add(key)

    def tick(self, today):
        """Time only annotates staleness; it does not mutate access or remove denies."""
        return {key: bool(isinstance(value, dict) and value.get("checked_on")
                          and value["checked_on"] < today)
                for key, value in self.effective().items()}


@dataclass
class History:
    versions: list
    active: int = -1

    def current(self):
        return deepcopy(self.versions[self.active])

    def activate(self, snapshot):
        self.versions.append(deepcopy(snapshot))
        self.active = len(self.versions) - 1

    def rollback(self):
        index = self.active if self.active >= 0 else len(self.versions) - 1
        if index == 0:
            raise PolicyError("no previous version")
        self.active = index - 1


@dataclass(frozen=True)
class HeldItem:
    expires_at: float
    reviewers: tuple

    def visible_to(self, person):
        return person in self.reviewers

    def release(self, current_decision, now, owner_action=False):
        """Caller must establish owner identity and re-evaluate current policy first."""
        if now >= self.expires_at:
            return deny("Held output expired", "hold expiry")
        if current_decision.status != "held":
            return current_decision
        if owner_action:
            return Decision("allow", "Owner explicitly released the held output", "owner release")
        return current_decision
