"""The equality check of the schema 1 to 2 migration (CODE_TO_DATA_S3, "The equality check"; slice S3.5a).

``diff_effective(before_root, after_root)`` compares two trees (a git ref extracted to a directory, and a work tree) on
four things and returns every difference that no named, intended change explains:

1. projections: each workflow's summaries, capability rows, schema, route inputs, mapping, OpenAPI and member catalog;
2. behaviour: the compiled engine request of every corpus case, and the multipart form fields (``workflow_effective_dump``
   runs each tree's own code; generated values come from an injected source, so a request is a pure function);
3. binding content: each after-binding must equal the before-binding after the converter's edits and the named edits;
4. services and protocol descriptions, record by record.

The allowed list is data (``scripts/workflow_equality_allowed.yml``): this module names no workflow, engine or service.
A difference outside the list fails the check, which is why the list is short and each row carries its reason.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gateway.workflow_convert import convert_1_to_2

ALLOWED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "workflow_equality_allowed.yml"
DUMP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "workflow_effective_dump.py"
CHECKS = ("projections", "requests", "binding_content", "services", "protocols")
GENERATED_SOURCE_OFFSET = 1  # the injected source gives the generator's minimum plus this: fixed, inside the range


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
                found.append((child, "<absent>", after[key]))
            elif key not in after:
                found.append((child, before[key], "<absent>"))
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


def glob(pattern: str, text: str) -> bool:
    """``*`` matches any run of characters; every other character, ``[`` included, stands for itself."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), text, flags=re.DOTALL) is not None


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


# --------------------------------------------------------------------------------------------------- the allowed list


@dataclass(frozen=True)
class Rule:
    checks: tuple[str, ...]
    scopes: tuple[str, ...]  # the consumer (a projection name) or "" for the checks that have none
    workflows: tuple[str, ...] | None  # None: every workflow
    paths: tuple[str, ...]
    reason: str
    before: Any = None
    has_before: bool = False

    def covers(self, diff: Difference, scope: str) -> bool:
        if diff.check not in self.checks:
            return False
        if self.workflows is not None and diff.workflow not in self.workflows:
            return False
        if self.scopes and not any(glob(pattern, scope) for pattern in self.scopes):
            return False
        if self.has_before and diff.before != self.before:
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
            before=row.get("before"), has_before="before" in row))
    return Allowed(rules=tuple(rules), binding_edits=tuple(raw.get("binding_edits", [])))


# ------------------------------------------------------------------------------------------------------------ the trees


def read_workflows(root: Path) -> dict[str, dict[str, Any]]:
    """Workflow id -> document, for every file under ``manifests/workflows`` of a tree."""
    found = {}
    for path in sorted((root / "manifests" / "workflows").glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        found[str(document["id"])] = document
    return found


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


def injected_value(spec: dict[str, Any]) -> Any:
    """The injected source of generated values: the generator's minimum plus one, so a request never depends on chance."""
    rule = spec["generate"]
    low = int(rule.get("minimum", 0)) + GENERATED_SOURCE_OFFSET
    return min(low, int(rule["maximum"])) if "maximum" in rule else low


def build_corpus(documents: dict[str, dict[str, Any]], limit: int = 48) -> dict[str, list[dict[str, Any]]]:
    """Per workflow: the declared defaults, each numeric bound, each enum value, each boolean value and the empty request,
    with every generated input fixed by the injected source. Built from the schema 2 form of the before documents."""
    corpus: dict[str, list[dict[str, Any]]] = {}
    for workflow_id, raw in sorted(documents.items()):
        document = as_schema_two(raw)
        inputs = {name: spec for name, spec in (document.get("inputs") or {}).items() if isinstance(spec, dict)}
        fixed = {name: injected_value(spec) for name, spec in inputs.items() if isinstance(spec.get("generate"), dict)}
        defaults = {name: copy.deepcopy(spec["default"]) for name, spec in inputs.items() if "default" in spec and name not in fixed}
        cases = [{"name": "empty", "values": dict(fixed)}, {"name": "defaults", "values": {**defaults, **fixed}}]
        for name, spec in inputs.items():
            if name in fixed or spec.get("type") in ("asset", "object", "array"):
                continue
            variants: list[tuple[str, Any]] = []
            if spec.get("type") in ("integer", "number"):
                variants += [(f"{bound}:{name}", spec[bound]) for bound in ("minimum", "maximum") if bound in spec]
            if isinstance(spec.get("enum"), list):
                variants += [(f"enum:{name}={value}", value) for value in spec["enum"]]
            if spec.get("type") == "boolean":
                variants += [(f"boolean:{name}={value}", value) for value in (True, False)]
            cases += [{"name": label, "values": {**defaults, **fixed, name: value}} for label, value in variants]
        corpus[workflow_id] = cases[:limit]
    return corpus


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
            found.append(Difference("binding_content", workflow_id, "/", "present" if workflow_id in before else "<absent>",
                                    "present" if workflow_id in after else "<absent>"))
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
        left, right = before.get(record_id, "<absent>"), after.get(record_id, "<absent>")
        for path, was, now in walk(left, right):
            found.append(Difference(check, record_id, path, was, now))
    return found


def _projection_differences(before: dict[str, Any], after: dict[str, Any]) -> tuple[list[tuple[str, Difference]], list[Difference]]:
    scoped: list[tuple[str, Difference]] = []
    requests: list[Difference] = []
    for workflow_id in sorted(set(before) | set(after)):
        left, right = before.get(workflow_id), after.get(workflow_id)
        if left is None or right is None:
            scoped.append(("", Difference("projections", workflow_id, "/", "present" if left else "<absent>", "present" if right else "<absent>")))
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


def diff_effective(before_root: Path, after_root: Path, *, checks: tuple[str, ...] = CHECKS, allowed: Allowed | None = None,
                   scratch: Path, python: str | None = None) -> Result:
    allowed = allowed or load_allowed()
    before_docs, after_docs = read_workflows(before_root), read_workflows(after_root)
    unexplained: list[Difference] = []
    explained: list[tuple[Difference, str]] = []
    compared: dict[str, int] = {}

    def sort(diff: Difference, scope: str = "") -> None:
        reason = allowed.reason_for(diff, scope)
        (explained.append((diff, reason)) if reason else unexplained.append(diff))

    if {"projections", "requests"} & set(checks):
        corpus_file = scratch / "corpus.json"
        corpus_file.write_text(json.dumps(build_corpus(before_docs)), encoding="utf-8")
        before_dump, after_dump = dump_tree(before_root, corpus_file, python), dump_tree(after_root, corpus_file, python)
        for side, dump in (("before", before_dump), ("after", after_dump)):
            if dump["errors"]:
                raise RuntimeError(f"the {side} tree reports load errors: {dump['errors']}")
        scoped, requests = _projection_differences(before_dump["workflows"], after_dump["workflows"])
        compared["projections"] = sum(len(v["projections"]) for v in after_dump["workflows"].values())
        compared["requests"] = sum(len(v["requests"]) + len(v["form"]) for v in after_dump["workflows"].values())
        if "projections" in checks:
            for scope, diff in scoped:
                sort(diff, scope)
        if "requests" in checks:
            for diff in requests:
                sort(diff)
    if "binding_content" in checks:
        compared["binding_content"] = len(after_docs)
        for diff in binding_content_differences(before_docs, after_docs, allowed):
            sort(diff)
    if "services" in checks:
        before_services, after_services = read_services(before_root), read_services(after_root)
        compared["services"] = len(after_services)
        for diff in _record_differences("services", before_services, after_services):
            sort(diff)
    if "protocols" in checks:
        before_protocols, after_protocols = read_protocols(before_root), read_protocols(after_root)
        compared["protocols"] = len(after_protocols)
        for diff in _record_differences("protocols", before_protocols, after_protocols):
            sort(diff)
    return Result(unexplained, explained, compared)
