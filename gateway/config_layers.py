"""Layered config sections: release defaults, sparse owner overrides, and what comes out (pure).

docs/design/CONFIG_HOMES.md sections 3 to 6. A *section* is a read-only release defaults document plus a closed JSON
Schema (Draft 2020-12) with five ``x-azizam-*`` annotations. The owner's changes are sparse *overrides*, keyed by
``(section, JSON Pointer)``, each one ``set`` (replace an entry) or ``delete`` (a tombstone). Resetting an entry to
inherit removes its override. Nothing here reads a file, a database or the network.

This module owns

- the entry index: where in a section an entry may sit, computed from the schema (``EntryIndex``);
- the merge: release defaults plus overrides give the effective section (``apply_overrides``);
- validation of the *candidate* effective section: structure by the schema, semantics by an optional reviewed function
  that receives the whole candidate context (``validate_section``);
- the load path: a deterministic greedy rebuild that rejects only the bad overrides and never raises
  (``build_section``);
- the per-entry source view (``entry_views``), the drift report (``drift_report``) and the export format.

The store (``gateway/admin_store.py``) keeps the rows and the runtime glue (``gateway/config_runtime.py``) calls this.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import jsonschema

OP_SET = "set"
OP_DELETE = "delete"
OPS = (OP_SET, OP_DELETE)

SOURCE_RELEASE = "release"
SOURCE_OWNER = "owner"
SOURCE_OWNER_ADDED = "owner-added"
SOURCE_OWNER_DELETED = "owner-deleted"
SOURCE_UNCONFIGURED = "unconfigured"  # a fail-closed entry whose override was rejected: absent, never the release value

INVALID = "invalid"
DANGLING = "dangling"
CODE_OVERRIDE_REJECTED = "override_rejected"
CODE_VALIDATOR_ERROR = "validator_error"  # a section's rule raised instead of returning problems

DRIFT_RELEASE_CHANGED = "release_changed"
DRIFT_RELEASE_REMOVED = "release_removed"

EXPORT_FORMAT = "azizam-config/v1"

ENTRY_KEY = "x-azizam-entry"
APPLY_KEY = "x-azizam-apply"
FAIL_KEY = "x-azizam-fail"
SECRET_KEY = "x-azizam-secret"
# Root only: keys of the section's old startup-file block that the section does not own. They stay in the startup file
# (a later slice moves them), so the one-time import leaves them out instead of refusing the block.
STARTUP_ONLY_KEY = "x-azizam-startup-only"
_ENTRY_VALUES = ("collection", "atomic")
_APPLY_VALUES = ("live", "restart")
_FAIL_VALUES = ("inherit", "closed")
_KNOWN_KEYS = {ENTRY_KEY, APPLY_KEY, FAIL_KEY, SECRET_KEY, STARTUP_ONLY_KEY}
_MAX_REF_DEPTH = 16


class ConfigLayerError(ValueError):
    """Base of the errors this module raises; the load path never lets one out."""


class SectionDataError(ConfigLayerError):
    """A section's release files break a rule. A packaging fault, not an owner fault: the caller stops with the rule."""


class PointerError(ConfigLayerError):
    """A JSON Pointer that is not RFC 6901 or that names the whole document."""


class MergeError(ConfigLayerError):
    """An override cannot be applied to the document (its path crosses a value that is not an object)."""


class ExportFormatError(ConfigLayerError):
    """An export document that this release cannot read."""


class _Absent:
    """Marks "no value here", which is not the same as a stored ``null``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> "_Absent":
        return self

    def __deepcopy__(self, memo: Any) -> "_Absent":
        return self

    def __reduce__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


# ---------------------------------------------------------------------------------------------------------------------
# JSON values and pointers
# ---------------------------------------------------------------------------------------------------------------------

def canonical_json(value: Any) -> str:
    """Stable text for comparing and digesting a JSON value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def same_value(left: Any, right: Any) -> bool:
    if left is ABSENT or right is ABSENT:
        return left is right
    return canonical_json(left) == canonical_json(right)


def split_pointer(pointer: Any) -> list[str]:
    """The segments of an RFC 6901 pointer. The empty pointer (the whole document) is never an entry."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PointerError(f"{pointer!r} is not a JSON Pointer that starts with '/'")
    return [segment.replace("~1", "/").replace("~0", "~") for segment in pointer[1:].split("/")]


def join_pointer(segments: Iterable[Any]) -> str:
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in segments)


def get_at(document: Any, pointer: str) -> Any:
    """The value at ``pointer`` in ``document``, or ABSENT."""
    node = document
    for segment in split_pointer(pointer):
        if isinstance(node, Mapping) and segment in node:
            node = node[segment]
        else:
            return ABSENT
    return node


# ---------------------------------------------------------------------------------------------------------------------
# Overrides, problems
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Override:
    """One owner override. ``base`` is the release value it was written over (ABSENT when the release had none)."""

    pointer: str
    op: str
    value: Any = None
    base: Any = ABSENT
    origin: str = "api"
    note: str | None = None
    change_id: str | None = None
    updated_at: int | None = None
    updated_by: str | None = None

    def as_row(self) -> dict[str, Any]:
        """The fields a person sees: no value for a tombstone, no base when the release had none."""
        row: dict[str, Any] = {"pointer": self.pointer, "op": self.op}
        if self.op == OP_SET:
            row["value"] = self.value
        if self.base is not ABSENT:
            row["base"] = self.base
        return row


@dataclass(frozen=True)
class Problem:
    """A fault found in a candidate section. ``pointer`` is None or "" when the rule cannot say where."""

    pointer: str | None
    code: str
    severity: str
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"pointer": self.pointer, "code": self.code, "severity": self.severity, "message": self.message}


# A reviewed rule across entries. It receives the candidate section and a read-only context (every other candidate
# section and the registry), and returns problems. It must not raise for bad data: report a problem.
SemanticValidator = Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[Problem]]
MergeFn = Callable[[Mapping[str, Any], Sequence[Override]], dict]


# ---------------------------------------------------------------------------------------------------------------------
# The entry index
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryRule:
    kind: str  # "scalar" | "list" | "atomic" | "member"
    apply: str = "live"
    fail: str = "inherit"
    secret: bool = False


@dataclass(frozen=True)
class _Inherited:
    apply: str = "live"
    fail: str = "inherit"
    secret: bool = False

    def over(self, node: Mapping[str, Any]) -> "_Inherited":
        return _Inherited(
            apply=str(node.get(APPLY_KEY, self.apply)),
            fail=str(node.get(FAIL_KEY, self.fail)),
            secret=bool(node.get(SECRET_KEY, self.secret)),
        )

    def rule(self, kind: str) -> EntryRule:
        return EntryRule(kind, self.apply, self.fail, self.secret)


def _is_list(node: Mapping[str, Any]) -> bool:
    return node.get("type") == "array" or "items" in node or "prefixItems" in node


def _is_object(node: Mapping[str, Any]) -> bool:
    return node.get("type") == "object" or node.get(ENTRY_KEY) == "collection" or any(
        key in node for key in ("properties", "patternProperties", "additionalProperties")
    )


class EntryIndex:
    """Where entries may sit in a section, from its schema.

    Rules (design 3.1 and 4.2): a scalar or a list is an entry; an object without annotation recurses into its
    properties; an object marked ``collection`` has one entry per member; an object marked ``atomic`` is one entry.
    ``apply``, ``fail`` and ``secret`` are inherited from the nearest annotated ancestor.
    """

    def __init__(self, schema: Mapping[str, Any]):
        self._root = schema

    def _resolve(self, node: Any, depth: int = 0) -> Mapping[str, Any]:
        if not isinstance(node, Mapping):
            return {}
        reference = node.get("$ref")
        if reference is None:
            return node
        if depth > _MAX_REF_DEPTH:
            raise SectionDataError(f"$ref chain deeper than {_MAX_REF_DEPTH} at {reference!r}")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise SectionDataError(f"only local $ref is supported, got {reference!r}")
        target: Any = self._root
        for segment in split_pointer(reference[1:]):
            if not isinstance(target, Mapping) or segment not in target:
                raise SectionDataError(f"$ref {reference!r} does not resolve")
            target = target[segment]
        merged = dict(self._resolve(target, depth + 1))
        merged.update({key: value for key, value in node.items() if key != "$ref"})
        return merged

    def _child(self, node: Mapping[str, Any], segment: str) -> Mapping[str, Any] | None:
        properties = node.get("properties")
        if isinstance(properties, Mapping) and segment in properties:
            return self._resolve(properties[segment])
        patterns = node.get("patternProperties")
        if isinstance(patterns, Mapping):
            for pattern, sub_schema in patterns.items():
                if re.search(pattern, segment):
                    return self._resolve(sub_schema)
        extra = node.get("additionalProperties")
        if isinstance(extra, Mapping):
            return self._resolve(extra)
        if extra is False:
            return None
        return {}  # the schema leaves it open

    def rule_for(self, pointer: str) -> EntryRule | None:
        """The rule when ``pointer`` is exactly an entry, else None."""
        segments = split_pointer(pointer)
        node = self._resolve(self._root)
        state = _Inherited()
        for position, segment in enumerate(segments):
            state = state.over(node)
            kind = node.get(ENTRY_KEY)
            if kind == "atomic" or _is_list(node) or not _is_object(node):
                return None  # inside an entry, or below a scalar: entries never nest
            child = self._child(node, segment)
            if child is None:
                return None
            if kind == "collection":
                if position != len(segments) - 1:
                    return None  # a member is replaced whole
                return state.over(child).rule("member")
            node = child
        state = state.over(node)
        kind = node.get(ENTRY_KEY)
        if kind == "collection":
            return None  # the members are entries, not the collection
        if kind == "atomic":
            return state.rule("atomic")
        if _is_list(node):
            return state.rule("list")
        if _is_object(node):
            return None  # an object recurses into its properties
        return state.rule("scalar")

    def is_required(self, pointer: str) -> bool:
        """Whether the schema lists the last segment of ``pointer`` as ``required`` in its parent object."""
        segments = split_pointer(pointer)
        node = self._resolve(self._root)
        for segment in segments[:-1]:
            child = self._child(node, segment)
            if child is None:
                return False
            node = child
        required = node.get("required")
        return isinstance(required, list) and segments[-1] in required

    def entry_containing(self, pointer: str | None) -> str | None:
        """The longest entry pointer that is ``pointer`` or a prefix of it (a problem maps to its entry)."""
        if not pointer:
            return None
        try:
            segments = split_pointer(pointer)
        except PointerError:
            return None
        for length in range(len(segments), 0, -1):
            candidate = join_pointer(segments[:length])
            if self.rule_for(candidate) is not None:
                return candidate
        return None

    def declared(self) -> list[tuple[str, EntryRule]]:
        """Every entry the schema declares, as a pointer template and its rule, in schema order.

        A member of a ``collection`` is one template ending in ``/*``. Unlike ``entries_in`` this reads the schema
        alone, so an entry the release data does not set yet is still listed.
        """
        found: list[tuple[str, EntryRule]] = []
        self._declare(self._resolve(self._root), [], _Inherited(), found)
        return found

    def _declare(self, node: Mapping[str, Any], segments: list[str], state: _Inherited,
                 found: list[tuple[str, EntryRule]]) -> None:
        state = state.over(node)
        kind = node.get(ENTRY_KEY)
        if kind == "collection":
            extra = node.get("additionalProperties")
            patterns = node.get("patternProperties")
            member = (self._resolve(extra) if isinstance(extra, Mapping)
                      else self._resolve(next(iter(patterns.values()))) if isinstance(patterns, Mapping) and patterns else {})
            found.append((join_pointer([*segments, "*"]), state.over(member).rule("member")))
        elif kind == "atomic":
            found.append((join_pointer(segments), state.rule("atomic")))
        elif _is_list(node):
            found.append((join_pointer(segments), state.rule("list")))
        elif not _is_object(node):
            found.append((join_pointer(segments), state.rule("scalar")))
        else:
            for name, child in (node.get("properties") or {}).items():
                self._declare(self._resolve(child), [*segments, name], state, found)
            # Keys the schema does not name (an operation id, an engine id) stand for ``*``.
            open_keys = [*(node.get("patternProperties") or {}).values()]
            if isinstance(node.get("additionalProperties"), Mapping):
                open_keys.append(node["additionalProperties"])
            for child in open_keys:
                self._declare(self._resolve(child), [*segments, "*"], state, found)

    def entries_in(self, document: Any) -> list[str]:
        """Every entry pointer present in ``document``, sorted."""
        found: list[str] = []
        self._walk(self._resolve(self._root), document, [], _Inherited(), found)
        return sorted(found)

    def _walk(self, node: Mapping[str, Any], value: Any, segments: list[str], state: _Inherited, found: list[str]) -> None:
        state = state.over(node)
        kind = node.get(ENTRY_KEY)
        if kind == "atomic" or _is_list(node) or not _is_object(node):
            if segments:
                found.append(join_pointer(segments))
            return
        if not isinstance(value, Mapping):
            return
        for key, item in value.items():
            child = self._child(node, str(key))
            if child is None:
                continue
            if kind == "collection":
                found.append(join_pointer([*segments, str(key)]))
            else:
                self._walk(child, item, [*segments, str(key)], state, found)

    def lint(self) -> list[str]:
        """Faults in the annotations themselves: unknown keys, bad values, a fail-closed entry that is required."""
        errors: list[str] = []
        self._lint(self._resolve(self._root), "", _Inherited(), errors, 0, False)
        return errors

    def _lint(
        self, node: Mapping[str, Any], where: str, state: _Inherited, errors: list[str], depth: int, inside_entry: bool,
    ) -> None:
        if depth > _MAX_REF_DEPTH:
            return
        for key in node:
            if isinstance(key, str) and key.startswith("x-azizam-") and key not in _KNOWN_KEYS:
                errors.append(f"{where or '/'}: unknown annotation {key}")
        checks = ((ENTRY_KEY, _ENTRY_VALUES), (APPLY_KEY, _APPLY_VALUES), (FAIL_KEY, _FAIL_VALUES))
        for key, allowed in checks:
            if key in node and node[key] not in allowed:
                errors.append(f"{where or '/'}: {key} must be one of {', '.join(allowed)}")
        if SECRET_KEY in node and not isinstance(node[SECRET_KEY], bool):
            errors.append(f"{where or '/'}: {SECRET_KEY} must be true or false")
        if STARTUP_ONLY_KEY in node:
            keys = node[STARTUP_ONLY_KEY]
            owned = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
            if where:
                errors.append(f"{where}: {STARTUP_ONLY_KEY} is allowed only at the root")
            elif not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
                errors.append(f"/: {STARTUP_ONLY_KEY} must be a list of key names")
            elif any(key in owned for key in keys):
                errors.append(f"/: {STARTUP_ONLY_KEY} names a key the section owns")
        kind = node.get(ENTRY_KEY)
        if (not inside_entry and _is_object(node) and kind not in _ENTRY_VALUES
                and node.get("additionalProperties") is not False):
            errors.append(
                f"{where or '/'}: an object must set additionalProperties to false or be marked {ENTRY_KEY} collection "
                "or atomic, so that entries cannot nest"
            )
        inside_entry = inside_entry or kind in _ENTRY_VALUES or _is_list(node)
        state = state.over(node)
        required = set(node.get("required") or []) if isinstance(node.get("required"), list) else set()
        properties = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
        for name, sub_schema in properties.items():
            child = self._resolve(sub_schema)
            if name in required and state.over(child).fail == "closed" and node.get(ENTRY_KEY) != "collection":
                errors.append(f"{where}/{name}: a fail-closed entry must be optional, so that its absence is valid")
            self._lint(child, f"{where}/{name}", state, errors, depth + 1, inside_entry)
        for group in ("patternProperties",):
            for name, sub_schema in (node.get(group) or {}).items():
                self._lint(self._resolve(sub_schema), f"{where}/{name}", state, errors, depth + 1, inside_entry)
        extra = node.get("additionalProperties")
        if isinstance(extra, Mapping):
            self._lint(self._resolve(extra), f"{where}/*", state, errors, depth + 1, inside_entry)


# ---------------------------------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """A section's release data. Build one with ``Section.create`` so that the files are checked once."""

    id: str
    defaults: Mapping[str, Any]
    schema: Mapping[str, Any]
    index: EntryIndex
    validator: SemanticValidator | None = None
    release_digest: str = ""
    _schema_validator: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def create(
        cls, section_id: str, defaults: Mapping[str, Any], schema: Mapping[str, Any],
        *, validator: SemanticValidator | None = None,
    ) -> "Section":
        """Check the release files and return the section. Raises ``SectionDataError`` with every rule broken."""
        errors: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9_]*", section_id):
            errors.append(f"section id {section_id!r} must be lower-case words joined by '_'")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as error:
            raise SectionDataError(f"section {section_id}: the schema is not a valid Draft 2020-12 schema: {error.message}") from error
        index = EntryIndex(schema)
        try:
            errors += index.lint()
        except SectionDataError as error:
            errors.append(str(error))
        schema_validator = jsonschema.Draft202012Validator(schema)
        if not errors:
            errors += [f"{problem.pointer or '/'}: {problem.message}" for problem in _structure_problems(schema_validator, defaults)]
        if errors:
            raise SectionDataError(f"section {section_id}: the release files break their rules:\n  " + "\n  ".join(errors[:20]))
        return cls(section_id, copy.deepcopy(dict(defaults)), copy.deepcopy(dict(schema)), index, validator,
                   digest(defaults), schema_validator)


def owned_part(section: Section, block: Any) -> Any:
    """A startup-file block without the root keys the section leaves in the startup file (``x-azizam-startup-only``)."""
    keys = section.schema.get(STARTUP_ONLY_KEY) or ()
    if not keys or not isinstance(block, Mapping):
        return block
    return {key: value for key, value in block.items() if key not in keys}


def _structure_problems(schema_validator: Any, candidate: Any) -> list[Problem]:
    problems = []
    for error in sorted(schema_validator.iter_errors(candidate), key=lambda item: [str(part) for part in item.absolute_path]):
        problems.append(Problem(
            join_pointer(error.absolute_path), f"schema.{error.validator}", INVALID, str(error.message)[:300],
        ))
    return problems


def validate_section(section: Section, candidate: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> list[Problem]:
    """Structure by the schema, then the section's semantic rule. Never raises for bad data."""
    problems = _structure_problems(section._schema_validator, candidate)
    if section.validator is not None:
        try:
            problems += list(section.validator(candidate, context if context is not None else {}))
        except Exception as error:  # noqa: BLE001 - a broken rule must not stop a boot; it is a problem to read
            problems.append(Problem(None, CODE_VALIDATOR_ERROR, INVALID, f"{type(error).__name__} in the section's rule"))
    return problems


def check_override(section: Section, override: Override) -> list[Problem]:
    """Faults in one override's own shape: the write path refuses the change when this is not empty."""
    problems: list[Problem] = []
    if override.op not in OPS:
        return [Problem(override.pointer, "bad_op", INVALID, f"op must be one of {', '.join(OPS)}")]
    try:
        rule = section.index.rule_for(override.pointer)
    except PointerError as error:
        return [Problem(override.pointer, "bad_pointer", INVALID, str(error))]
    if rule is None:
        return [Problem(override.pointer, "not_an_entry", INVALID, "this pointer is not an entry of the section")]
    if override.op == OP_DELETE and rule.kind != "member" and section.index.is_required(override.pointer):
        problems.append(Problem(
            override.pointer, "delete_required", INVALID,
            "a required entry cannot be deleted; reset it to inherit the release value",
        ))
    if override.op == OP_SET and rule.secret:
        from gateway import secret_refs  # lazy: the store behind it is not needed for the pure rules

        if not (override.value is None or secret_refs.is_ref(override.value)):
            problems.append(Problem(override.pointer, "secret_value", INVALID, "this entry holds a secret reference, not a value"))
    return problems


# ---------------------------------------------------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------------------------------------------------

def _set_at(document: dict, segments: list[str], value: Any) -> None:
    node: Any = document
    for segment in segments[:-1]:
        child = node.get(segment, ABSENT) if isinstance(node, dict) else ABSENT
        if child is ABSENT:
            child = {}
            node[segment] = child
        elif not isinstance(child, dict):
            raise MergeError(f"{join_pointer(segments)} crosses a value that is not an object")
        node = child
    if not isinstance(node, dict):
        raise MergeError(f"{join_pointer(segments)} crosses a value that is not an object")
    node[segments[-1]] = copy.deepcopy(value)


def _delete_at(document: dict, segments: list[str]) -> None:
    node: Any = document
    for segment in segments[:-1]:
        node = node.get(segment) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(segments[-1], None)


def apply_overrides(base: Mapping[str, Any], overrides: Sequence[Override]) -> dict:
    """``base`` with the overrides applied: tombstones first, then the replacements in pointer order.

    The unit of change is the entry (design 4.2): ``set`` replaces the entry whole, so there is no deep merge, and a
    list is replaced, never appended to. Raises ``MergeError`` when a path crosses a value that is not an object.
    """
    document = copy.deepcopy(dict(base))
    for override in sorted((o for o in overrides if o.op == OP_DELETE), key=lambda o: o.pointer):
        _delete_at(document, split_pointer(override.pointer))
    for override in sorted((o for o in overrides if o.op == OP_SET), key=lambda o: o.pointer):
        _set_at(document, split_pointer(override.pointer), override.value)
    return document


# ---------------------------------------------------------------------------------------------------------------------
# The load path
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rejected:
    override: Override
    reasons: tuple[Problem, ...]


@dataclass(frozen=True)
class SectionResult:
    section_id: str
    effective: dict
    problems: tuple[Problem, ...]
    applied: tuple[Override, ...]
    rejected: tuple[Rejected, ...]

    @property
    def invalid(self) -> tuple[Problem, ...]:
        return tuple(problem for problem in self.problems if problem.severity == INVALID)


def invalid_counter(problems: Iterable[Problem]) -> Counter:
    """The ``invalid`` problems as a multiset, so two candidates can be compared for what one adds."""
    return Counter((p.pointer, p.code, p.message) for p in problems if p.severity == INVALID)


_invalid = invalid_counter


def build_section(
    section: Section,
    overrides: Sequence[Override],
    context: Mapping[str, Any] | None = None,
    *,
    merge: MergeFn = apply_overrides,
) -> SectionResult:
    """The effective section for the stored overrides. Deterministic, and it never raises for an override fault.

    Normal case: apply every override and validate; no ``invalid`` problem means done. Otherwise rebuild greedily
    (design 3.5): start from the release defaults with every ``closed`` entry that has an override removed, then try the
    ``delete`` ops and then the ``set`` ops, each in pointer order, keeping one only when it adds no ``invalid``
    problem. Repeat over the rejected ones until a pass keeps none, so an override that needs a later one
    is not lost. A rejected override follows its entry's ``x-azizam-fail``: ``inherit`` leaves the release value,
    ``closed`` leaves the entry absent.

    A problem need not carry a pointer: the test is whether the set of ``invalid`` problems grew.
    """
    ordered = sorted(overrides, key=lambda override: override.pointer)
    shape_rejected: list[Rejected] = []
    usable: list[Override] = []
    for override in ordered:
        faults = check_override(section, override)
        if faults and _retired_delete(section, override, faults):
            # A stored delete of an entry the release no longer has asks for an absence that already holds: it is
            # inert, never a boot problem (a retired setting's owner delete, train 35's compile_from_pin).
            continue
        if faults:
            shape_rejected.append(Rejected(override, tuple(faults)))
        else:
            usable.append(override)

    if not shape_rejected:
        try:
            candidate = merge(section.defaults, usable)
        except MergeError:
            candidate = None
        if candidate is not None:
            problems = validate_section(section, candidate, context)
            if not _invalid(problems):
                return SectionResult(section.id, candidate, tuple(problems), tuple(usable), ())

    return _greedy(section, ordered, usable, shape_rejected, context, merge)


def _retired_delete(section: Section, override: Override, faults: Sequence[Problem]) -> bool:
    """A delete whose only fault is that its pointer is no longer an entry, and that names nothing in the release.

    Only a plain key path counts: every segment non-empty, walked through objects alone. A pointer into a list, a
    container the release still holds, or a malformed pointer stays a rejected override.
    """
    if override.op != OP_DELETE or not all(fault.code == "not_an_entry" for fault in faults):
        return False
    try:
        segments = split_pointer(override.pointer)
    except PointerError:
        return False
    if any(segment == "" for segment in segments):
        return False
    node: Any = section.defaults
    for segment in segments:
        if not isinstance(node, Mapping):
            return False
        if segment not in node:
            return True
        node = node[segment]
    return False


def _fail_closed(section: Section, pointer: str) -> bool:
    try:
        rule = section.index.rule_for(pointer)
    except PointerError:
        return False
    return rule is not None and rule.fail == "closed"


def _greedy(
    section: Section, ordered: Sequence[Override], usable: Sequence[Override], shape_rejected: Sequence[Rejected],
    context: Mapping[str, Any] | None, merge: MergeFn,
) -> SectionResult:
    # Every override whose pointer is a fail-closed entry leaves that entry absent unless it is kept below, whether or
    # not its own shape was accepted (a secret entry with a plain value, say).
    closed = [override for override in ordered if _fail_closed(section, override.pointer)]
    current = merge(section.defaults, [Override(o.pointer, OP_DELETE) for o in closed])
    standing = _invalid(validate_section(section, current, context))
    kept: list[Override] = []
    rejected: list[Rejected] = list(shape_rejected)
    # Deletes first, then sets, each in pointer order: the order of the merge. A delete is tried like a set.
    pending = [o for o in usable if o.op == OP_DELETE] + [o for o in usable if o.op == OP_SET]
    reasons: dict[str, tuple[Problem, ...]] = {}
    while pending:
        progressed = False
        still: list[Override] = []
        for override in pending:
            try:
                trial = merge(current, [override])
            except MergeError as error:
                reasons[override.pointer] = (Problem(override.pointer, "path_conflict", INVALID, str(error)),)
                still.append(override)
                continue
            found = validate_section(section, trial, context)
            added = _invalid(found) - standing
            if added:
                reasons[override.pointer] = tuple(
                    p for p in found if p.severity == INVALID and (p.pointer, p.code, p.message) in added
                )
                still.append(override)
                continue
            current, standing = trial, _invalid(found)
            kept.append(override)
            progressed = True
        pending = still
        if not progressed:
            break
    rejected += [Rejected(o, reasons.get(o.pointer, ())) for o in pending]
    problems = list(validate_section(section, current, context))
    for item in rejected:
        problems.append(Problem(item.override.pointer, CODE_OVERRIDE_REJECTED, INVALID, _rejection_text(item)))
    return SectionResult(section.id, current, tuple(problems), tuple(kept), tuple(rejected))


def _rejection_text(item: Rejected) -> str:
    why = "; ".join(sorted({f"{p.code}" for p in item.reasons})) or "the section is not valid with it"
    return f"the owner's {item.override.op} was not applied: {why}"


# ---------------------------------------------------------------------------------------------------------------------
# The legacy import: the fewest overrides that make a section equal a startup-file block (design 8.1)
# ---------------------------------------------------------------------------------------------------------------------

ORIGIN_LEGACY = "legacy-import"
CODE_LEGACY_UNKNOWN = "legacy_key_unknown"


def _lost_keys(target: Any, merged: Any, segments: list[str]) -> list[str]:
    """Pointers of keys that ``target`` holds and ``merged`` does not, or holds with another value (a leaf or a list)."""
    if isinstance(target, Mapping) and isinstance(merged, Mapping):
        lost: list[str] = []
        for key, item in target.items():
            if key not in merged:
                lost.append(join_pointer([*segments, str(key)]))
            else:
                lost += _lost_keys(item, merged[key], [*segments, str(key)])
        return lost
    return [] if same_value(target, merged) else [join_pointer(segments) or "/"]


def overrides_for_target(
    section: Section, target: Any, context: Mapping[str, Any] | None = None,
) -> tuple[list[Override], list[Problem]]:
    """The fewest overrides that make the effective section equal ``target`` (design 8.1 step 1), and why it cannot.

    A differing entry, or one only in ``target``, is a ``set``. A collection member that only the release ships is a
    ``delete`` (a tombstone). A scalar that ``target`` leaves out gives no row: for a settings-shaped section the code
    fallback stands, and the release default is written to equal it. The second value holds the faults that stop the
    import: a key the section does not know, a value the schema refuses, a path that cannot merge. When it is not empty
    the caller must not write the first (the import does not run for this section).
    """
    if not isinstance(target, Mapping):
        return [], [Problem("", "not_an_object", INVALID, "the startup block must be a mapping")]
    wanted = section.index.entries_in(target)
    in_release = set(section.index.entries_in(section.defaults))
    overrides: list[Override] = []
    for pointer in wanted:
        value, release = get_at(target, pointer), get_at(section.defaults, pointer)
        if not same_value(value, release):
            overrides.append(Override(pointer, OP_SET, copy.deepcopy(value), base=release, origin=ORIGIN_LEGACY))
    for pointer in sorted(in_release - set(wanted)):
        rule = section.index.rule_for(pointer)
        if rule is not None and rule.kind == "member":
            overrides.append(Override(pointer, OP_DELETE, None, base=get_at(section.defaults, pointer), origin=ORIGIN_LEGACY))
    blocked: list[Problem] = []
    for override in overrides:
        blocked += check_override(section, override)
    if blocked:
        return overrides, blocked
    try:
        merged = apply_overrides(section.defaults, overrides)
    except MergeError as error:
        return overrides, [Problem("", "path_conflict", INVALID, str(error))]
    blocked += [
        Problem(pointer, CODE_LEGACY_UNKNOWN, INVALID, "the section does not take this key, so the import would lose it")
        for pointer in _lost_keys(target, merged, [])
    ]
    blocked += [problem for problem in validate_section(section, merged, context) if problem.severity == INVALID]
    return overrides, blocked


# ---------------------------------------------------------------------------------------------------------------------
# The source view and the drift report
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Drift:
    pointer: str
    kind: str
    base: Any
    release: Any
    owner: Any


def drift_for(section: Section, override: Override) -> Drift | None:
    release = get_at(section.defaults, override.pointer)
    if same_value(override.base, release):
        return None
    kind = DRIFT_RELEASE_REMOVED if release is ABSENT else DRIFT_RELEASE_CHANGED
    owner = override.value if override.op == OP_SET else ABSENT
    return Drift(override.pointer, kind, override.base, release, owner)


def drift_report(section: Section, overrides: Sequence[Override]) -> list[Drift]:
    """Overrides whose release value moved since they were written (design 4.4). Information, never a prompt."""
    found = (drift_for(section, override) for override in sorted(overrides, key=lambda o: o.pointer))
    return [item for item in found if item is not None]


@dataclass(frozen=True)
class EntryView:
    pointer: str
    value: Any  # ABSENT when the entry is not in the effective section
    source: str
    release_value: Any
    override: Override | None
    apply: str
    fail: str
    secret: bool
    drift: Drift | None
    problems: tuple[Problem, ...]


def entry_views(section: Section, result: SectionResult, overrides: Sequence[Override]) -> list[EntryView]:
    """One row per entry the section, its release or the owner has: the effective-source view (design 4.3)."""
    by_pointer = {override.pointer: override for override in overrides}
    rejected = {item.override.pointer for item in result.rejected}
    release_entries = set(section.index.entries_in(section.defaults))
    pointers = sorted(release_entries | set(section.index.entries_in(result.effective)) | set(by_pointer))
    grouped: dict[str | None, list[Problem]] = {}
    for problem in result.problems:
        grouped.setdefault(section.index.entry_containing(problem.pointer), []).append(problem)
    views = []
    for pointer in pointers:
        override = by_pointer.get(pointer)
        rule = section.index.rule_for(pointer) or EntryRule("scalar")
        in_release = pointer in release_entries
        if override is None:
            source = SOURCE_RELEASE
        elif pointer in rejected:
            source = SOURCE_UNCONFIGURED if rule.fail == "closed" else SOURCE_RELEASE
        elif override.op == OP_DELETE:
            source = SOURCE_OWNER_DELETED
        else:
            source = SOURCE_OWNER if in_release else SOURCE_OWNER_ADDED
        views.append(EntryView(
            pointer=pointer, value=get_at(result.effective, pointer), source=source,
            release_value=get_at(section.defaults, pointer), override=override,
            apply=rule.apply, fail=rule.fail, secret=rule.secret,
            drift=drift_for(section, override) if override is not None else None,
            problems=tuple(grouped.get(pointer, ())),
        ))
    return views


# ---------------------------------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------------------------------

def export_document(
    overrides_by_section: Mapping[str, Sequence[Override]],
    sections: Mapping[str, Section],
    *,
    exported_at: str,
    gateway_version: str,
    registry: Mapping[str, Any] | None = None,
    secrets: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """The owner layer as an ``azizam-config/v1`` document (design 6.1): overrides only, references and no secret values."""
    document: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "exported_at": exported_at,
        "gateway_version": gateway_version,
        "release_digests": {name: sections[name].release_digest for name in sorted(overrides_by_section) if name in sections},
        "sections": {
            name: [override.as_row() for override in sorted(rows, key=lambda o: o.pointer)]
            for name, rows in sorted(overrides_by_section.items()) if rows
        },
    }
    if registry is not None:
        document["registry"] = copy.deepcopy(dict(registry))
    if secrets:
        document["secrets"] = [dict(item) for item in secrets]
    return document


def parse_export(document: Any) -> dict[str, list[Override]]:
    """The overrides an export document holds, by section. Raises ``ExportFormatError`` for a format it cannot read."""
    if not isinstance(document, Mapping) or document.get("format") != EXPORT_FORMAT:
        raise ExportFormatError(f"format must be {EXPORT_FORMAT}")
    sections = document.get("sections", {})
    if not isinstance(sections, Mapping):
        raise ExportFormatError("sections must be a mapping of section id to a list of rows")
    parsed: dict[str, list[Override]] = {}
    for name, rows in sections.items():
        if not isinstance(rows, list):
            raise ExportFormatError(f"sections.{name} must be a list of rows")
        parsed[str(name)] = []
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping) or row.get("op") not in OPS or not isinstance(row.get("pointer"), str):
                raise ExportFormatError(f"sections.{name}[{position}] needs a pointer and an op of {', '.join(OPS)}")
            if row["op"] == OP_SET and "value" not in row:
                raise ExportFormatError(f"sections.{name}[{position}]: a set row needs a value")
            parsed[str(name)].append(Override(
                row["pointer"], row["op"], row.get("value") if row["op"] == OP_SET else None,
                row["base"] if "base" in row else ABSENT, origin="import",
            ))
    return parsed
