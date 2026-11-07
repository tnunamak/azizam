"""The equality check of the schema 1 to 2 migration (CODE_TO_DATA_S3, "The equality check"; slice S3.5a).

``diff_effective(before_root, after_root)`` compares two trees (a git ref extracted to a directory, and a work tree) on
four things and returns every difference that no named, intended change explains:

1. projections: each workflow's summaries, capability rows, schema, route inputs, mapping, OpenAPI and member catalog;
2. behaviour: the compiled engine request of every corpus case, and the multipart form fields (``workflow_effective_dump``
   runs each tree's own code). A case is the caller's values; the comparison prepares them from each tree's own document
   (defaults, then generated values from an injected random source, then the input check), so a request is a pure function
   of the tree and a change to a default, a bound or a generator shows;
3. binding content: each after-binding must equal the before-binding after the converter's edits and the named edits;
4. services and protocol descriptions, record by record.

The allowed list is data (``scripts/workflow_equality_allowed.yml``): this module names no workflow, engine or service.
A difference outside the list fails the check, which is why the list is short and each row carries its reason.

The check cannot pass by comparing nothing: a selected check that compared no record fails (unless the caller names it in
``allow_empty``), the workflows of each tree must be the workflows its files hold, and a workflow on one side only is a
failure in every selected check.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import random
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

from gateway.manifests import exact_number, validate_input_value
from gateway.workflow_contract import GENERATORS
from gateway.workflow_convert import convert_1_to_2

ALLOWED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "workflow_equality_allowed.yml"
DUMP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "workflow_effective_dump.py"
SOURCES_SCRIPT = DUMP_SCRIPT.with_name("workflow_sources.py")
ABSENT = "<absent>"  # what ``walk`` writes for a key that is not there
CHECKS = ("projections", "requests", "binding_content", "services", "protocols")


@dataclass(frozen=True)
class Difference:
    check: str
    workflow: str
    path: str
    before: Any
    after: Any
    scope: str = ""

    def line(self) -> str:
        return f"{self.check}{':' + self.scope if self.scope else ''} {self.workflow} {self.path}: {_short(self.before)} -> {_short(self.after)}"


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 90 else text[:87] + "..."


def walk(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Every leaf where two JSON values differ, as (pointer, before, after). A list index is written ``[n]``."""
    if type(before) is not type(after):
        return [(path or "/", before, after)]
    if isinstance(before, dict):
        found: list[tuple[str, Any, Any]] = []
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}/{key}"
            if key not in before:
                found.append((child, ABSENT, after[key]))
            elif key not in after:
                found.append((child, before[key], ABSENT))
            else:
                found.extend(walk(before[key], after[key], child))
        return found
    if isinstance(before, list):
        if len(before) != len(after):
            return [(path or "/", before, after)]
        found = []
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            found.extend(walk(left, right, f"{path}[{index}]"))
        return found
    return [] if before == after else [(path or "/", before, after)]


def json_equal(left: Any, right: Any) -> bool:
    """Equality of two JSON values that tells the types apart: ``True`` is not ``1``, ``0`` is not ``False`` and ``1`` is not
    ``1.0`` (the same rule ``walk`` uses to see a type change), at every depth. Every pin of the allowed list uses this."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def glob(pattern: str, text: str) -> bool:
    """``*`` matches any run of characters; every other character, ``[`` included, stands for itself."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), text, flags=re.DOTALL) is not None


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


# --------------------------------------------------------------------------------------------------- the allowed list


def _is_replacement(before: Any, after: Any, pairs: tuple[tuple[str, str], ...]) -> bool:
    """The after-text is the before-text with the pairs applied and with nothing else changed (so a changed endpoint or number
    elsewhere in the text is not covered)."""
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    applied = [(old, new) for old, new in pairs if old in before]
    text = before
    for old, new in applied:
        text = text.replace(old, new)
    return bool(applied) and text == after


def _is_drop(before: Any, after: Any, match: dict[str, Any]) -> bool:
    """The after-list is the before-list without the items that match, and at least one item was dropped. ``match`` is
    ``{value: v}`` for a list of plain values or ``{key: k, value: v}`` for a list of mappings."""
    if not isinstance(before, list) or not isinstance(after, list):
        return False

    def hit(item: Any) -> bool:
        return json_equal(item.get(match["key"]) if "key" in match and isinstance(item, dict) else item, match["value"])

    kept = [item for item in before if not hit(item)]
    return len(kept) < len(before) and json_equal(kept, after)


JSON_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool, "null": type(None)}


@dataclass(frozen=True)
class Rule:
    checks: tuple[str, ...]
    scopes: tuple[str, ...]  # the consumer (a projection name) or "" for the checks that have none
    workflows: tuple[str, ...] | None  # None: every workflow
    paths: tuple[str, ...]
    reason: str
    before: Any = None
    has_before: bool = False
    after: Any = None
    has_after: bool = False
    replace: tuple[tuple[str, str], ...] = ()  # text: the after-value is the before-value with these (from, to) pairs applied
    drops: dict[str, Any] | None = None  # list: the after-value is the before-list without its items that match
    kept_type: str | None = None  # both values are present and of this JSON type (a value that may change but not vanish)
    kept_pattern: str | None = None  # both values are text that this regular expression matches as a whole

    def covers(self, diff: Difference, scope: str) -> bool:
        if diff.check not in self.checks:
            return False
        if self.workflows is not None and diff.workflow not in self.workflows:
            return False
        if self.scopes and not any(glob(pattern, scope) for pattern in self.scopes):
            return False
        if self.has_before and not json_equal(diff.before, self.before):
            return False
        if self.has_after and not json_equal(diff.after, self.after):
            return False
        if self.kept_type is not None and not all(type(value) is JSON_TYPES[self.kept_type] and value != ABSENT
                                                  for value in (diff.before, diff.after)):
            return False
        if self.kept_pattern is not None and not all(isinstance(value, str) and re.fullmatch(self.kept_pattern, value)
                                                     for value in (diff.before, diff.after)):
            return False
        if self.replace and not _is_replacement(diff.before, diff.after, self.replace):
            return False
        if self.drops is not None and not _is_drop(diff.before, diff.after, self.drops):
            return False
        return any(glob(pattern, _generic(diff.path)) for pattern in self.paths)


@dataclass(frozen=True)
class Allowed:
    rules: tuple[Rule, ...]
    binding_edits: tuple[dict[str, Any], ...]

    def reason_for(self, diff: Difference, scope: str) -> str | None:
        return next((rule.reason for rule in self.rules if rule.covers(diff, scope)), None)


def load_allowed(path: Path = ALLOWED_PATH) -> Allowed:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = []
    for row in raw.get("allowed", []):
        if not row.get("reason"):
            raise ValueError(f"an allowed difference needs a reason: {row}")
        workflows = row.get("workflows", "all")
        rules.append(Rule(
            checks=tuple(row["checks"]), scopes=tuple(row.get("scopes", ())),
            workflows=None if workflows == "all" else tuple(workflows), paths=tuple(row["paths"]), reason=row["reason"],
            before=row.get("before"), has_before="before" in row, after=row.get("after"), has_after="after" in row,
            replace=tuple((pair["from"], pair["to"]) for pair in row.get("replace", ())), drops=row.get("drops"),
            kept_type=row.get("kept_type"), kept_pattern=row.get("kept_pattern")))
        if rules[-1].kept_pattern is not None:
            re.compile(rules[-1].kept_pattern)   # a pattern that does not compile fails the load, not a difference
        if rules[-1].kept_type is not None and rules[-1].kept_type not in JSON_TYPES:
            raise ValueError(f"an allowed difference names an unknown kept_type: {row}")
    return Allowed(rules=tuple(rules), binding_edits=tuple(raw.get("binding_edits", [])))


# ------------------------------------------------------------------------------------------------------------ the trees


def _sources() -> Any:
    spec = importlib.util.spec_from_file_location("workflow_sources", SOURCES_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_workflows(root: Path) -> dict[str, dict[str, Any]]:
    """Workflow id -> document, for every workflow file of a tree: the bundled directory (``*.yml`` and ``*.yaml``, as
    discovery reads it) and the workflow objects of the bundled extension packages."""
    return {workflow_id: document for workflow_id, (_path, document, _package) in _sources().read_documents(root).items()}


def read_services(root: Path) -> dict[str, Any]:
    path = root / "registry" / "services.yml"
    if not path.exists():
        return {}
    records = yaml.safe_load(path.read_text(encoding="utf-8")).get("records", [])
    return {str(record["id"]): record for record in records if isinstance(record, dict) and record.get("id")}


def read_protocols(root: Path) -> dict[str, Any]:
    found = {}
    for path in sorted((root / "registry" / "protocols").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("id"):
            found[str(document["id"])] = document
    return found


def as_schema_two(document: dict[str, Any]) -> dict[str, Any]:
    """The schema 2 form of a document: the converter's output for schema 1, the document itself for schema 2."""
    return convert_1_to_2(document)[0]


# --------------------------------------------------------------------------------------------------------- the corpus


def representative(spec: dict[str, Any]) -> Any:
    """A valid value for a required input that has no default: the first enum value, else a value of its type inside its bounds."""
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        return spec.get("minimum", spec.get("maximum", 1))
    if kind == "string":
        return "a representative value"
    return None  # asset, object and array inputs: a case does not invent one


def _specs(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: spec for name, spec in (as_schema_two(document).get("inputs") or {}).items() if isinstance(spec, dict)}


def build_corpus(documents: dict[str, dict[str, Any]], limit: int = 48) -> dict[str, list[dict[str, Any]]]:
    """Per workflow, the cases as the caller would supply them: nothing, a valid value for each required input, and then each
    numeric bound, enum value and boolean value on top of that. Defaults and generated values are not here: ``prepare_corpus``
    adds them from each tree's own document. Built from the schema 2 form of the before documents."""
    corpus: dict[str, list[dict[str, Any]]] = {}
    for workflow_id, raw in sorted(documents.items()):
        inputs = _specs(raw)
        required = {name: representative(spec) for name, spec in inputs.items()
                    if spec.get("required") and "default" not in spec and representative(spec) is not None}
        cases = [{"name": "empty", "values": {}}, {"name": "required", "values": dict(required)}]
        for name, spec in inputs.items():
            if isinstance(spec.get("generate"), dict) or spec.get("type") in ("asset", "object", "array"):
                continue
            variants: list[tuple[str, Any]] = []
            if spec.get("type") in ("integer", "number"):
                variants += [(f"{bound}:{name}", spec[bound]) for bound in ("minimum", "maximum") if bound in spec]
            if isinstance(spec.get("enum"), list):
                variants += [(f"enum:{name}={value}", value) for value in spec["enum"]]
            if spec.get("type") == "boolean":
                variants += [(f"boolean:{name}={value}", value) for value in (True, False)]
            cases += [{"name": label, "values": {**required, name: value}} for label, value in variants]
        corpus[workflow_id] = cases[:limit]
    return corpus


GENERATED_CONSTRAINTS = ("type", "enum", "minimum", "maximum", "step", "offset")  # what the input check reads of a generated input


def _exact_bound(workflow_id: str, name: str, what: str, value: Any) -> Fraction:
    exact = exact_number(value)
    if exact is None:
        raise ValueError(f"{workflow_id} input {name}: the {what} {value!r} is not a number the equality gate can use")
    return exact


def generator_values(workflow_id: str, name: str, spec: dict[str, Any]) -> Sequence[int] | None:
    """The whole numbers a generate rule can give that the input check accepts, in order (a ``range``, or a list when the
    input has an ``enum``): the rule's range cut by the input's bounds, laid on the input's grid (``step`` from ``offset``,
    both exact fractions; the check reads the offset only with a step) and cut by the enum. ``None`` when no value is left.
    The generator must be a reviewed one (``GENERATORS``). A form this function does not handle (a type that is not
    integer or number, a bound, step or offset that is not a number, a zero or negative step) raises, naming the input."""
    rule = spec["generate"]
    if rule.get("generator") not in GENERATORS:
        raise ValueError(f"{workflow_id} input {name}: no reviewed generator {rule.get('generator')!r}")
    if spec.get("type") not in ("integer", "number"):
        raise ValueError(f"{workflow_id} input {name}: a generated input of type {spec.get('type')!r} is not supported by the equality gate")
    low = _exact_bound(workflow_id, name, "generator minimum", rule.get("minimum", 0))
    high = _exact_bound(workflow_id, name, "generator maximum", rule["maximum"])
    if spec.get("minimum") is not None:
        low = max(low, _exact_bound(workflow_id, name, "input minimum", spec["minimum"]))
    if spec.get("maximum") is not None:
        high = min(high, _exact_bound(workflow_id, name, "input maximum", spec["maximum"]))
    low, high = math.ceil(low), math.floor(high)
    step, offset = Fraction(1), Fraction(0)
    if spec.get("step") is not None:
        step = _exact_bound(workflow_id, name, "step", spec["step"])
        offset = _exact_bound(workflow_id, name, "offset", spec.get("offset", 0))
        if step <= 0:
            raise ValueError(f"{workflow_id} input {name}: the step {spec['step']!r} is not positive")
    # Whole numbers x with (x - offset) / step whole: x = x0 + p * t, with step = p / q in lowest terms.
    p, q = step.numerator, step.denominator
    if q % offset.denominator:
        return None   # offset + k * step is never whole
    k0 = (-offset.numerator * (q // offset.denominator) * pow(p, -1, q)) % q
    x0 = int(offset + k0 * step)
    first = x0 + p * -((x0 - low) // p)   # the least x0 + p * t that is >= low
    values: Sequence[int] = range(first, high + 1, p)
    if isinstance(spec.get("enum"), list):
        # The check tests membership with Python equality, so an integral member such as 7.0 (or True) admits the whole
        # number equal to it; a fractional or non-finite member admits none.
        members = {int(m) for m in spec["enum"] if isinstance(m, (int, float)) and math.isfinite(m) and m == int(m)}
        values = sorted(v for v in members if v in values)
    if not values:
        return None
    if any(validate_input_value(name, value, spec) for value in (values[0], values[-1])):
        raise ValueError(f"{workflow_id} input {name}: the equality gate computed a generated value the input check refuses")
    return values


def generator_range(workflow_id: str, name: str, spec: dict[str, Any]) -> tuple[int, int] | None:
    """The least and the greatest value a generate rule can give (both accepted by the input check), or ``None`` when it can give none."""
    values = generator_values(workflow_id, name, spec)
    return None if values is None else (values[0], values[-1])


def generated_value(workflow_id: str, name: str, spec: dict[str, Any]) -> Any:
    """What a generate rule gives: a draw from a random source seeded by the workflow and input, from the values the
    input check accepts. A rule with no valid value gives none."""
    values = generator_values(workflow_id, name, spec)
    if values is None:
        return None
    # len() of a range is limited to a machine word; count with whole numbers so a range of 2**63 values or more draws too.
    count = (values[-1] - values[0]) // values.step + 1 if isinstance(values, range) else len(values)
    return values[random.Random(f"{workflow_id}:{name}").randrange(count)]


def generator_rules(documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per workflow, the generation rule of each generated input with the bounds of that input: the rule itself, which a
    single draw cannot show (a range of 0..3 and one of 0..4 can draw the same value), and every constraint the input check
    applies to the drawn value."""
    found: dict[str, dict[str, Any]] = {}
    for workflow_id, document in documents.items():
        for name, spec in _specs(document).items():
            if isinstance(spec.get("generate"), dict):
                rule = {"generate": spec["generate"], **{key: spec.get(key, ABSENT) for key in GENERATED_CONSTRAINTS}}
                found.setdefault(workflow_id, {})[name] = rule
    return found


def prepare_inputs(workflow_id: str, document: dict[str, Any], supplied: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The values a route hands to the binding: the caller's, else the input's default, else a generated one; then the input
    check on every value. Returns the values and the reason for each one the check refused. Uses the schema 2 form of the
    document, so a schema 1 file is prepared as its conversion is."""
    values: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    specs = _specs(document)
    for name in sorted(specs):
        spec = specs[name]
        if name in supplied:
            values[name] = copy.deepcopy(supplied[name])
        elif "default" in spec:
            values[name] = copy.deepcopy(spec["default"])
        elif isinstance(spec.get("generate"), dict):
            drawn = generated_value(workflow_id, name, spec)
            if drawn is None:
                rejected[name] = "the generator's range and the input's bounds share no value"
                continue
            values[name] = drawn
        else:
            continue
        reason = validate_input_value(name, values[name], spec)
        if reason is not None:
            rejected[name] = reason
    for name in set(supplied) - set(specs):
        rejected[name] = "unknown input"
    return values, rejected


def prepare_corpus(documents: dict[str, dict[str, Any]], corpus: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The corpus for one tree: each case's values prepared from that tree's own document, refused values kept apart."""
    out: dict[str, list[dict[str, Any]]] = {}
    for workflow_id, cases in corpus.items():
        if workflow_id not in documents:
            continue
        prepared = []
        for case in cases:
            values, rejected = prepare_inputs(workflow_id, documents[workflow_id], case["values"])
            prepared.append({"name": case["name"], "values": {k: v for k, v in values.items() if k not in rejected},
                             "rejected": rejected})
        # The least and the greatest value each generate rule of this tree can give, on top of the required values: a range
        # that grows or shrinks moves one of them.
        required = next((case["values"] for case in cases if case["name"] == "required"), {})
        for name, spec in sorted(_specs(documents[workflow_id]).items()):
            bounds = generator_range(workflow_id, name, spec) if isinstance(spec.get("generate"), dict) else None
            for label, value in zip(("generated-minimum", "generated-maximum"), bounds or (), strict=False):
                values, rejected = prepare_inputs(workflow_id, documents[workflow_id], {**required, name: value})
                prepared.append({"name": f"{label}:{name}", "values": {k: v for k, v in values.items() if k not in rejected},
                                 "rejected": rejected})
        out[workflow_id] = prepared
    return out


# ------------------------------------------------------------------------------------------------------------- compare


def dump_tree(root: Path, corpus_file: Path, python: str | None = None) -> dict[str, Any]:
    done = subprocess.run([python or sys.executable, str(DUMP_SCRIPT), str(root), str(corpus_file)],
                          capture_output=True, text=True, check=False, timeout=900)
    if done.returncode != 0:
        raise RuntimeError(f"the dump of {root} failed:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout)


def apply_binding_edits(workflow_id: str, bindings: dict[str, Any], edits: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Replay the named edits that are not the converter's: today only ``remove`` of one key."""
    out = copy.deepcopy(bindings)
    for edit in edits:
        if edit["workflow"] != workflow_id:
            continue
        if edit["operation"] != "remove":
            raise ValueError(f"unknown binding edit operation: {edit['operation']}")
        node: Any = out
        *parents, last = edit["path"]
        for part in parents:
            node = node[part]
        del node[last]
    return out


def binding_content_differences(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]], allowed: Allowed) -> list[Difference]:
    """Each after-binding against the before-binding with the converter's edits and the named edits applied."""
    found: list[Difference] = []
    for workflow_id in sorted(set(before) | set(after)):
        if workflow_id not in before or workflow_id not in after:
            found.append(Difference("binding_content", workflow_id, "/", "present" if workflow_id in before else ABSENT,
                                    "present" if workflow_id in after else ABSENT))
            continue
        # The named edits belong to the migration: a document that is schema 2 already has had them.
        edits = allowed.binding_edits if before[workflow_id].get("schema_version") == 1 else ()
        expected = apply_binding_edits(workflow_id, as_schema_two(before[workflow_id]).get("bindings") or {}, edits)
        for path, was, now in walk(expected, after[workflow_id].get("bindings") or {}):
            found.append(Difference("binding_content", workflow_id, path, was, now))
    return found


def _record_differences(check: str, before: dict[str, Any], after: dict[str, Any]) -> list[Difference]:
    found = []
    for record_id in sorted(set(before) | set(after)):
        left, right = before.get(record_id, ABSENT), after.get(record_id, ABSENT)
        for path, was, now in walk(left, right):
            found.append(Difference(check, record_id, path, was, now))
    return found


def _presence(check: str, workflow_id: str, left: Any, right: Any) -> Difference:
    return Difference(check, workflow_id, "/", "present" if left is not None else ABSENT, "present" if right is not None else ABSENT)


def _projection_differences(before: dict[str, Any], after: dict[str, Any]) -> tuple[list[tuple[str, Difference]], list[Difference]]:
    scoped: list[tuple[str, Difference]] = []
    requests: list[Difference] = []
    for workflow_id in sorted(set(before) | set(after)):
        left, right = before.get(workflow_id), after.get(workflow_id)
        if left is None or right is None:
            # A workflow on one side only is a failure of each check that reads workflows, not of one of them.
            scoped.append(("", _presence("projections", workflow_id, left, right)))
            requests.append(_presence("requests", workflow_id, left, right))
            continue
        for consumer in sorted(set(left["projections"]) | set(right["projections"])):
            for path, was, now in walk(left["projections"].get(consumer), right["projections"].get(consumer)):
                scoped.append((consumer, Difference("projections", workflow_id, path, was, now, consumer)))
        for name in ("requests", "form"):
            for path, was, now in walk(left[name], right[name]):
                requests.append(Difference("requests", workflow_id, f"/{name}{path}", was, now))
    return scoped, requests


@dataclass
class Result:
    unexplained: list[Difference]
    explained: list[tuple[Difference, str]]
    compared: dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.unexplained


def _assert_dump_covers(side: str, dump: dict[str, Any], documents: dict[str, dict[str, Any]]) -> None:
    """The dump of a tree holds exactly the workflows its files hold: a workflow the dump skipped would go unchecked."""
    if dump["errors"]:
        raise RuntimeError(f"the {side} tree reports load errors: {dump['errors']}")
    dumped, expected = set(dump["workflows"]), set(documents)
    if dumped != expected:
        raise RuntimeError(f"the {side} dump does not hold the workflows of the files: missing {sorted(expected - dumped)}, "
                           f"extra {sorted(dumped - expected)}")


def _count(before: dict[str, Any], after: dict[str, Any], measure) -> int:
    """How many records a check looked at: those of the after side, and those that exist only on the before side."""
    return sum(measure(after[key] if key in after else before[key]) for key in set(before) | set(after))


def diff_effective(before_root: Path, after_root: Path, *, checks: tuple[str, ...] = CHECKS, allowed: Allowed | None = None,
                   scratch: Path, python: str | None = None, allow_empty: tuple[str, ...] = ()) -> Result:
    allowed = allowed or load_allowed()
    before_docs, after_docs = read_workflows(before_root), read_workflows(after_root)
    unexplained: list[Difference] = []
    explained: list[tuple[Difference, str]] = []
    compared: dict[str, int] = {}

    def sort(diff: Difference, scope: str = "") -> None:
        reason = allowed.reason_for(diff, scope)
        (explained.append((diff, reason)) if reason else unexplained.append(diff))

    if {"projections", "requests"} & set(checks):
        corpus = build_corpus(before_docs)
        dumps = {}
        for side, root, documents in (("before", before_root, before_docs), ("after", after_root, after_docs)):
            corpus_file = scratch / f"corpus-{side}.json"
            corpus_file.write_text(json.dumps(prepare_corpus(documents, corpus)), encoding="utf-8")
            dumps[side] = dump_tree(root, corpus_file, python)
            _assert_dump_covers(side, dumps[side], documents)
        before_dump, after_dump = dumps["before"]["workflows"], dumps["after"]["workflows"]
        scoped, requests = _projection_differences(before_dump, after_dump)
        compared["projections"] = _count(before_dump, after_dump, lambda v: len(v["projections"]))
        compared["requests"] = _count(before_dump, after_dump, lambda v: len(v["requests"]) + len(v["form"]))
        if "projections" in checks:
            for scope, diff in scoped:
                sort(diff, scope)
        if "requests" in checks:
            rules = {side: generator_rules(docs) for side, docs in (("before", before_docs), ("after", after_docs))}
            compared["requests"] += sum(len(inputs) for inputs in rules["before"].values())
            for workflow_id in sorted(set(rules["before"]) | set(rules["after"])):
                for path, was, now in walk(rules["before"].get(workflow_id, {}), rules["after"].get(workflow_id, {})):
                    requests.append(Difference("requests", workflow_id, f"/generate{path}", was, now))
            for diff in requests:
                sort(diff)
    if "binding_content" in checks:
        compared["binding_content"] = len(set(before_docs) | set(after_docs))
        for diff in binding_content_differences(before_docs, after_docs, allowed):
            sort(diff)
    if "services" in checks:
        before_services, after_services = read_services(before_root), read_services(after_root)
        compared["services"] = len(set(before_services) | set(after_services))
        for diff in _record_differences("services", before_services, after_services):
            sort(diff)
    if "protocols" in checks:
        before_protocols, after_protocols = read_protocols(before_root), read_protocols(after_root)
        compared["protocols"] = len(set(before_protocols) | set(after_protocols))
        for diff in _record_differences("protocols", before_protocols, after_protocols):
            sort(diff)
    for check in checks:
        # A check that looked at nothing proves nothing; an empty comparison has to be asked for by name.
        if compared.get(check, 0) == 0 and check not in allow_empty:
            unexplained.append(Difference(check, "<none>", "/", "compared nothing", "compared nothing"))
    return Result(unexplained, explained, compared)
