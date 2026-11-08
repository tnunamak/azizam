"""The live, versioned workflow registry store (CODE_TO_DATA_S4_S8, I2 and 5.1 to 5.7).

One workflow is one record in the ``workflows`` section of the service-registry document. Every write here is one
compare-and-swap on that document's ``os-N`` generation, made by ``admin_store.write_service_registry_with_audit``; the
one name check every write of the document runs sits in the same transaction (``service_registry.refuse_new_name_clashes``).

What a stored record holds beside its contract (``manifest``): the policy facts (``policy``), ``overrides`` (the bundled id
it replaces), and the run control the owner sets: ``state`` (active or disabled), ``published``, ``incarnation`` (set when
the id is created, new after a delete and create) and ``record_revision`` (one more for each accepted change of this one
workflow). The record digest and the contract digest of an entry are computed once, by the snapshot builder, and read back
from the snapshot: this module never computes a second copy.

History: each accepted change writes the whole record as a blob in the definition store, held by the holder
``registry:<workflow id>`` for the last ``workflow_execution.history_depth`` changes. The blob holds its own revision,
digests, actor and kind of change, so the holder is the only index. The history write is part of the accepted change:
the holder changes inside the registry transaction (it fails the change if it fails) and is put back if the registry
commit then fails. The two databases share no transaction, so a crash between them can leave one row for a revision
that did not happen; the next change drops it. Deleting a workflow releases its holder.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway import admin_store, binding_descriptors, frontend_profiles, manifests, output_selectors, registry_layers, service_registry
from gateway.definition_store import DefinitionStore
from gateway.job_envelope import canonical_bytes
from gateway.manifests import ContractError, ContractErrors, ManifestError
from gateway.workflow_contract import Report, check_contract
from gateway.workflow_kernel import name_clashes
from gateway.workflow_policy import facts_from_mapping
from gateway.workflow_runtime_types import (
    BOUND_EXCEEDED, KEEP, WORKFLOW_EXISTS, WORKFLOW_NOT_FOUND, Actor, Commit, EntryState, HistoryEntry, Keep,
    NameOwner, RegistryConflict, RegistryRefused, RegistrySnapshot, WorkflowFacts,
)

log = logging.getLogger(__name__)

CONFIG_SECTION = "workflow_execution"
# The code's ceilings. The deployment's data may lower them and never raise them (as ``result_bundle.ResultLimits``).
HISTORY_DEPTH_CEILING = 20
MAX_TEMPLATE_BYTES_CEILING = 512 * 1024
MAX_NODES_CEILING = 2000
MAX_DEPTH_CEILING = 32
OWN_NAME_KINDS = frozenset({"workflow", "workflow_alias", "operation", "component"})

ID_IS_BUNDLED = "id_is_bundled"
OVERRIDES_MISMATCH = "overrides_mismatch"
FACTS_INVALID = "facts_invalid"
ID_MISMATCH = "id_mismatch"
SNAPSHOT_REFUSED = "snapshot_refused"
REVISION_NOT_FOUND = "revision_not_found"


class WorkflowIdIsBundled(Exception):
    """A write would store a workflow under the id of a bundled one without ``overrides: <id>``, or delete a bundled id
    (HTTP 409 ``id_is_bundled``). ``bundled`` is the contract the release ships, so a client can copy and edit it."""

    def __init__(self, workflow_id: str, bundled: Mapping[str, Any] | None) -> None:
        super().__init__(f"workflow {workflow_id!r} is a bundled workflow")
        self.workflow_id = workflow_id
        self.bundled = copy.deepcopy(dict(bundled)) if isinstance(bundled, Mapping) else None


class RegistryNotApplied(RuntimeError):
    """The change is saved (``commit``) but the running gateway could not apply it (a failed hot reload)."""

    def __init__(self, message: str, *, commit: Commit) -> None:
        super().__init__(message)
        self.commit = commit


@dataclass(frozen=True)
class Bounds:
    max_template_bytes: int = MAX_TEMPLATE_BYTES_CEILING
    max_nodes: int = MAX_NODES_CEILING
    max_depth: int = MAX_DEPTH_CEILING


def _lowered(value: Any, ceiling: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < ceiling else ceiling


def history_depth_from_config(config: Mapping[str, Any]) -> int:
    """``workflow_execution.history_depth``: a positive integer at most the code's ceiling; anything else is the ceiling."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    return _lowered(section.get("history_depth") if isinstance(section, Mapping) else None, HISTORY_DEPTH_CEILING)


def bounds_from_config(config: Mapping[str, Any]) -> Bounds:
    """``workflow_execution.bounds``: each key a positive integer at most its ceiling; a missing or bad key keeps the ceiling."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    raw = section.get("bounds") if isinstance(section, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    return Bounds(
        max_template_bytes=_lowered(raw.get("max_template_bytes"), MAX_TEMPLATE_BYTES_CEILING),
        max_nodes=_lowered(raw.get("max_nodes"), MAX_NODES_CEILING),
        max_depth=_lowered(raw.get("max_depth"), MAX_DEPTH_CEILING),
    )


# -- structure of an inline template -----------------------------------------------------------------------------------


def inline_templates(contract: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """``(binding key, template field, inline template)`` for each binding that carries its template inline (5.7). The
    field is the one the binding's description declares as a template; any adapter, and nothing here reads what the
    template means."""
    found: list[tuple[str, str, Any]] = []
    bindings = contract.get("bindings")
    descriptors = binding_descriptors.cached_shipped_binding_descriptors()
    for key, block in (bindings.items() if isinstance(bindings, Mapping) else ()):
        descriptor = descriptors.get(str(key))
        carried = binding_descriptors.inline_template(descriptor, block) if descriptor and isinstance(block, Mapping) else None
        if carried is not None:
            found.append((str(key), carried[0], carried[1]))
    return found


def template_depth(template: Any) -> int:
    """The deepest nesting of objects and lists in a JSON value, counted without recursion (a hostile template must not
    be the thing that breaks the check). A scalar is depth 0, an empty object or list depth 1."""
    deepest, stack = 0, [(template, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, Mapping):
            children: Iterable[Any] = value.values()
        elif isinstance(value, (list, tuple)):
            children = value
        else:
            deepest = max(deepest, depth)
            continue
        deepest = max(deepest, depth + 1)
        stack.extend((child, depth + 1) for child in children)
    return deepest


def bound_problems(contract: Mapping[str, Any], bounds: Bounds) -> list[ContractError]:
    """Every inline template that breaks a bound. Bytes are the canonical JSON; a node is one entry of the template's
    top level (the graph's nodes); depth is the nesting of objects and lists."""
    problems: list[ContractError] = []
    for key, field, template in inline_templates(contract):
        at = ("bindings", key, field, "inline")
        size = len(canonical_bytes(template))
        if size > bounds.max_template_bytes:
            problems.append(ContractError(at, BOUND_EXCEEDED, f"the template is {size} bytes; the limit is {bounds.max_template_bytes}"))
        if len(template) > bounds.max_nodes:
            problems.append(ContractError(at, BOUND_EXCEEDED, f"the template has {len(template)} nodes; the limit is {bounds.max_nodes}"))
        depth = template_depth(template)
        if depth > bounds.max_depth:
            problems.append(ContractError(at, BOUND_EXCEEDED, f"the template nests {depth} levels; the limit is {bounds.max_depth}"))
    return problems


# -- the save pipeline ---------------------------------------------------------------------------------------------------


def _collect(problems: list[ContractError], run: Callable[[], Any]) -> None:
    """Run one check that raises; add every error it raises to ``problems``."""
    try:
        run()
    except ContractErrors as error:
        problems.extend(error.errors)
    except ManifestError as error:
        problems.append(ContractError((), "manifest_invalid", str(error)))
    except (TypeError, ValueError, KeyError, AttributeError) as error:   # a hostile shape a check cannot read
        problems.append(ContractError((), "manifest_invalid", f"the document cannot be checked: {type(error).__name__}"))


def contract_problems(contract: Mapping[str, Any]) -> list[ContractError]:
    """Everything the loaders refuse a workflow document for, found in one pass: the manifest checks with the binding
    closure and the output selectors (``validate_manifest``), the schema 2 contract (``check_contract``), the profile of
    the frontend that serves its intent, and the stricter save rules for selectors (``check_for_save``). The loaders'
    ``enforce_contract`` raises at the first stage that fails; this returns the errors of all stages."""
    problems: list[ContractError] = []
    document = copy.deepcopy(dict(contract))
    descriptors = binding_descriptors.cached_shipped_binding_descriptors()
    _collect(problems, lambda: manifests.validate_manifest(copy.deepcopy(document), binding_descriptors=descriptors))
    problems.extend(check_contract(document).errors)
    _collect(problems, lambda: frontend_profiles.check(document))
    problems.extend(_selector_save_problems(document, descriptors))
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for problem in problems:
        key = (problem.path, problem.code, problem.message)
        if key not in seen:
            seen.add(key)
            unique.append(problem)
    return unique


def _selector_save_problems(contract: Mapping[str, Any], descriptors: Mapping[str, Any]) -> list[ContractError]:
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping) or not isinstance(contract.get("outputs"), Mapping):
        return []
    shipped = output_selectors._cached_shipped()
    cardinalities = manifests._cardinalities(bindings, descriptors)
    inline = {key: template for key, _field, template in inline_templates(contract)}
    problems: list[ContractError] = []
    for key, block in bindings.items():
        if not isinstance(block, Mapping):
            continue
        form = output_selectors.selector_form(key, block)
        template = inline.get(str(key))
        try:
            output_selectors.check_for_save(shipped.get(form), contract, key,
                                            template=template if isinstance(template, Mapping) else None,
                                            cardinality=cardinalities.get(form))
        except ContractErrors as error:
            problems.extend(error.errors)
        except output_selectors.SelectorError as error:
            problems.append(ContractError(("bindings", key, "selectors"), getattr(error, "code", "selector_malformed"), str(error)))
    return problems


def facts_record(facts: WorkflowFacts) -> dict[str, Any]:
    """The ``policy`` mapping of a stored record."""
    return {"exposure": facts.exposure, "trust_tier": facts.trust_tier, "resource_tier": facts.resource_tier,
            "tags": list(facts.tags), service_registry.RESOURCE_CLASS_KEY: facts.resource_class}


def facts_problems(facts: Any) -> list[ContractError]:
    if facts is None or isinstance(facts, Keep):
        return []
    if not isinstance(facts, WorkflowFacts):
        return [ContractError(("facts",), FACTS_INVALID, "facts is a set of policy facts or null")]
    stated = facts_record(facts)
    if facts_from_mapping(stated) is None or not isinstance(facts.resource_class, str) or not facts.resource_class.strip():
        return [ContractError(("facts",), FACTS_INVALID,
                              "each fact is non-empty text, and tags are a list of distinct text")]
    return []


class _StagedHistory:
    """The holder change of one registry commit: ``apply`` makes it (inside the registry transaction), ``undo`` takes it
    back if the commit does not happen. ``after`` None releases the holder (a delete)."""

    def __init__(self, definitions: DefinitionStore, holder: str, before: tuple[str, ...],
                 after: tuple[str, ...] | None) -> None:
        self._definitions, self._holder, self._before, self._after = definitions, holder, before, after
        self._applied = False

    def apply(self, _conn: Any, _now: int) -> None:
        self._applied = True
        if self._after is None:
            self._definitions.release_holder(self._holder)
        else:
            self._definitions.replace_holder(self._holder, list(self._after))

    def undo(self) -> None:
        if not self._applied:
            return
        try:
            if self._before:
                self._definitions.replace_holder(self._holder, list(self._before))
            else:
                self._definitions.release_holder(self._holder)
        except Exception:   # noqa: BLE001 - the stale row is dropped by the next change; do not hide the real failure
            log.exception("a workflow history change could not be taken back")


# -- the store -----------------------------------------------------------------------------------------------------------

SnapshotBuilder = Callable[..., RegistrySnapshot]


class WorkflowRegistryStore:
    """The production ``RegistryStore``. ``build_snapshot(document, bundled, generation=N)`` is
    ``registry_snapshot.build``: ``document`` is the effective service-registry document and ``bundled`` the release
    layer (``service_registry.release_registry()``). ``on_changed`` is the gateway's hot reload
    (``reload_runtime_config``); ``config`` returns the runtime config the history depth and bounds are read from."""

    def __init__(self, *, path: Path, definitions: DefinitionStore, build_snapshot: SnapshotBuilder,
                 config: Callable[[], Mapping[str, Any]] = dict, on_changed: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._definitions = definitions
        self._build = build_snapshot
        self._config = config
        self._on_changed = on_changed
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[Callable[[RegistrySnapshot, RegistrySnapshot], None]] = []
        self._cached: tuple[int, str, RegistrySnapshot] | None = None

    # -- reading ---------------------------------------------------------------------------------------------------------

    def _read(self) -> tuple[dict[str, Any], int]:
        """The stored (owner-data) document and its generation. No stored registry is an empty document at 0."""
        record = admin_store.get_service_registry_json_with_generation(path=self._path)
        if record is None or not service_registry._parses_as_object(record["value_json"]):
            return registry_layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), (
                int(record["operator_settings_generation"]) if record else 0)
        return service_registry._stored_document(record["value_json"]), int(record["operator_settings_generation"])

    def _snapshot_of(self, stored: Mapping[str, Any], generation: int) -> RegistrySnapshot:
        release = service_registry.release_registry()
        document, _excluded = service_registry.effective_registry(stored)
        return self._build(document, release, generation=generation)

    def snapshot(self) -> RegistrySnapshot:
        stored, generation = self._read()
        label = service_registry.release_digest()
        with self._lock:
            if self._cached is not None and self._cached[0] == generation and self._cached[1] == label:
                return self._cached[2]
        built = self._snapshot_of(stored, generation)
        with self._lock:
            self._cached = (generation, label, built)
        return built

    def get_record(self, workflow_id: str) -> dict[str, Any] | None:
        """The stored record of one workflow after layering (a copy), or None."""
        stored, _generation = self._read()
        record = registry_layers.layer(stored, service_registry.release_registry()).get("workflows", {}).get(workflow_id)
        return copy.deepcopy(dict(record)) if isinstance(record, Mapping) else None

    # -- check -----------------------------------------------------------------------------------------------------------

    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, facts: WorkflowFacts | None | Keep = KEEP,
              overrides: str | None = None, snapshot: RegistrySnapshot) -> Report:
        """The whole save pipeline, every problem with its path: the contract (``contract_problems``), the facts, the
        bundled-id rule, the name check against ``snapshot`` and the bounds. Writes nothing."""
        if not isinstance(doc, Mapping):
            error = ContractError((), "type_mismatch", "a workflow document must be a mapping")
            return Report(errors=(error,), primary=error)
        problems = list(contract_problems(doc))
        wid = doc.get("id") if isinstance(doc.get("id"), str) else None
        if wid is None:
            problems.append(ContractError(("id",), "required_field_missing", "a workflow document needs an id"))
        elif workflow_id is not None and wid != workflow_id:
            problems.append(ContractError(("id",), ID_MISMATCH, f"the document is for {wid!r}, not {workflow_id!r}"))
        problems.extend(facts_problems(facts))
        if overrides is not None and (not isinstance(overrides, str) or overrides != wid):
            problems.append(ContractError(("overrides",), OVERRIDES_MISMATCH, "overrides names the id of the bundled workflow it replaces"))
        elif wid is not None and overrides is None and wid in self._bundled_ids():
            problems.append(ContractError(("id",), ID_IS_BUNDLED,
                                          f"workflow {wid!r} is a bundled workflow; to replace it, save with overrides: {wid!r}"))
        if wid is not None:
            problems.extend(self._clashes(doc, wid, snapshot))
            problems.extend(bound_problems(doc, bounds_from_config(self._config())))
        ordered = tuple(problems)
        return Report(errors=ordered, primary=ordered[0] if ordered else None)

    def _bundled_ids(self) -> frozenset[str]:
        return frozenset(str(workflow_id) for workflow_id in service_registry.bundled_workflow_manifests())

    def _clashes(self, doc: Mapping[str, Any], wid: str, snapshot: RegistrySnapshot) -> list[ContractError]:
        """The names this document would hold against the revision's names without the workflow's own."""
        mine = list(service_registry.workflow_name_pairs(wid))
        mine.extend((str(alias), NameOwner("workflow_alias", wid)) for alias in (doc.get("aliases") or ()) if isinstance(alias, str))
        others = [(name, owner) for name, owner in snapshot.names.items()
                  if not (owner.id == wid and owner.kind in OWN_NAME_KINDS)]
        return list(name_clashes([*others, *mine]))

    # -- writing ---------------------------------------------------------------------------------------------------------

    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor,
                     facts: WorkflowFacts | None | Keep = KEEP, overrides: str | None = None) -> Commit:
        wid = str(doc.get("id")) if isinstance(doc, Mapping) and isinstance(doc.get("id"), str) else None
        if wid is None:
            raise RegistryRefused((ContractError(("id",), "required_field_missing", "a workflow document needs an id"),))

        def build(stored: Mapping[str, Any], generation: int, base: RegistrySnapshot) -> tuple[dict[str, Any] | None, str]:
            old = self._effective_record(stored, wid)
            bundled = wid in self._bundled_ids()
            if if_match is None:
                if self._stored_has(stored, wid) and not service_registry.is_run_control_patch(old):
                    raise RegistryConflict(generation, WORKFLOW_EXISTS)
                if bundled and overrides != wid:
                    raise WorkflowIdIsBundled(wid, service_registry.bundled_workflow_manifests().get(wid))
            elif old is None:
                raise RegistryRefused((ContractError(("id",), WORKFLOW_NOT_FOUND, f"no workflow {wid}"),))
            elif bundled and overrides != wid:
                raise WorkflowIdIsBundled(wid, service_registry.bundled_workflow_manifests().get(wid))
            report = self.check(doc, workflow_id=wid, facts=facts, overrides=overrides, snapshot=base)
            if report.errors:
                raise RegistryRefused(tuple(report.errors))
            existed = self._stored_has(stored, wid)
            record = self._new_record(old, existed, copy.deepcopy(dict(doc)), facts, overrides)
            return record, "update" if existed else "create"

        return self._apply(wid, actor, if_match, build)

    def set_state(self, workflow_id: str, state: EntryState, *, if_match: int, actor: Actor) -> Commit:
        if state not in service_registry.WORKFLOW_STATES:
            raise RegistryRefused((ContractError(("state",), "state_invalid", "state is active or disabled"),))
        return self._apply(workflow_id, actor, if_match, self._run_control(workflow_id, "state", state))

    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit:
        if not isinstance(published, bool):
            raise RegistryRefused((ContractError(("published",), "published_invalid", "published is true or false"),))
        return self._apply(workflow_id, actor, if_match, self._run_control(workflow_id, "published", published))

    def _run_control(self, workflow_id: str, key: str, value: Any):
        change = "state" if key == "state" else "publish"

        def build(stored: Mapping[str, Any], generation: int, base: RegistrySnapshot) -> tuple[dict[str, Any] | None, str]:
            old = self._effective_record(stored, workflow_id)
            entry = base.get(workflow_id)
            if old is not None and not service_registry.is_run_control_patch(old):
                record = self._carry(old, self._stored_has(stored, workflow_id))
            elif workflow_id in self._bundled_ids() and entry is not None:
                record = self._run_control_patch(entry)
            else:
                raise RegistryRefused((ContractError(("id",), WORKFLOW_NOT_FOUND, f"no workflow {workflow_id}"),))
            record[key] = value
            return record, change

        return build

    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit:
        def build(stored: Mapping[str, Any], generation: int, base: RegistrySnapshot) -> tuple[dict[str, Any] | None, str]:
            if workflow_id in self._bundled_ids() and not self._stored_has(stored, workflow_id):
                raise WorkflowIdIsBundled(workflow_id, service_registry.bundled_workflow_manifests().get(workflow_id))
            if not self._stored_has(stored, workflow_id):
                raise RegistryRefused((ContractError(("id",), WORKFLOW_NOT_FOUND, f"no workflow {workflow_id}"),))
            return None, "delete"

        return self._apply(workflow_id, actor, if_match, build)

    def restore(self, workflow_id: str, record_revision: int, *, if_match: int, actor: Actor) -> Commit:
        """The record as it was at ``record_revision``, written as a new revision. The current state and incarnation stay."""
        def build(stored: Mapping[str, Any], generation: int, base: RegistrySnapshot) -> tuple[dict[str, Any] | None, str]:
            old = self._effective_record(stored, workflow_id)
            if old is None:
                raise RegistryRefused((ContractError(("id",), WORKFLOW_NOT_FOUND, f"no workflow {workflow_id}"),))
            past = next((row for row in self._history_rows(workflow_id) if row["record_revision"] == record_revision), None)
            if past is None:
                raise RegistryRefused((ContractError(("record_revision",), REVISION_NOT_FOUND,
                                                     f"there is no saved revision {record_revision} of {workflow_id!r}"),))
            then = past["record"]
            if service_registry.is_run_control_patch(then):
                # A past revision that held run control only is written back as that patch.
                record = self._run_control_patch(base.get(workflow_id))
                record["published"] = bool(then.get("published", True))
                return record, "restore"
            manifest = copy.deepcopy(then["manifest"])
            overrides = then.get("overrides")
            report = self.check(manifest, workflow_id=workflow_id, facts=KEEP, overrides=overrides, snapshot=base)
            if report.errors:
                raise RegistryRefused(tuple(report.errors))
            existed = self._stored_has(stored, workflow_id)
            record = self._carry(old, existed)
            record["manifest"] = manifest
            record.pop("policy", None)
            if isinstance(then.get("policy"), Mapping):
                record["policy"] = copy.deepcopy(dict(then["policy"]))
            record.pop("overrides", None)
            if isinstance(overrides, str) and overrides:
                record["overrides"] = overrides
            record["published"] = bool(then.get("published", False))
            return record, "restore"

        return self._apply(workflow_id, actor, if_match, build)

    # -- the shape of a record -------------------------------------------------------------------------------------------

    def _effective_record(self, stored: Mapping[str, Any], workflow_id: str) -> dict[str, Any] | None:
        record = registry_layers.layer(stored, service_registry.release_registry()).get("workflows", {}).get(workflow_id)
        return dict(record) if isinstance(record, Mapping) else None

    @staticmethod
    def _stored_has(stored: Mapping[str, Any], workflow_id: str) -> bool:
        return isinstance((stored.get("workflows") or {}).get(workflow_id), Mapping)

    _KEPT_BESIDE = ("source", "editable", "enabled", "provenance", "registry_source", "source_path", "manifest_path")

    def _carry(self, old: Mapping[str, Any], existed: bool) -> dict[str, Any]:
        """The record as it stands, ready to change one thing: contract, facts, overrides, run control and the fields
        beside them. ``existed`` is false for a bundled id the owner has not stored a record for."""
        manifest = old.get("manifest") if isinstance(old.get("manifest"), Mapping) else {
            key: value for key, value in old.items() if key not in service_registry.RUN_CONTROL_KEYS
            and key not in {"editable", "enabled", "identity", "manifest_hash", "migration", "overrides", "policy",
                            "provenance", "registry_source", "source_path", "manifest_path", "summary", "source"}}
        record: dict[str, Any] = {} if service_registry.is_run_control_patch(old) else {"manifest": copy.deepcopy(dict(manifest))}
        for key in self._KEPT_BESIDE:
            if key in old:
                record[key] = copy.deepcopy(old[key])
        if isinstance(old.get("policy"), Mapping):
            record["policy"] = copy.deepcopy(dict(old["policy"]))
        if isinstance(old.get("overrides"), str) and old["overrides"]:
            record["overrides"] = old["overrides"]
        control = service_registry.run_control_of(old)
        record["state"] = control.get("state", "active")
        record["published"] = control.get("published", True)
        record["incarnation"] = control.get("incarnation") or uuid.uuid4().hex
        record["record_revision"] = control.get("record_revision", 1 if existed else 0) + 1
        return record

    @staticmethod
    def _run_control_patch(entry: Any) -> dict[str, Any]:
        """The stored record of a bundled workflow whose run control the owner changes: state, publication and revision
        only, naming the bundled id. It holds no contract and no facts, so a later release shows through; the snapshot
        builds the entry from the release's own record, with its provenance and incarnation."""
        return {registry_layers.RUN_CONTROL_PATCH_KEY: entry.id, "state": entry.state, "published": entry.published,
                "record_revision": entry.record_revision + 1}

    def _new_record(self, old: Mapping[str, Any] | None, existed: bool, contract: dict[str, Any],
                    facts: WorkflowFacts | None | Keep, overrides: str | None) -> dict[str, Any]:
        record = self._carry(old, existed) if old is not None else {
            "state": "active", "published": False, "incarnation": uuid.uuid4().hex, "record_revision": 1}
        record["manifest"] = contract
        record.setdefault("source", "ui")
        record.setdefault("editable", True)
        record.setdefault("enabled", True)
        if isinstance(facts, WorkflowFacts):
            record["policy"] = facts_record(facts)
        elif facts is None:
            record.pop("policy", None)
        record.pop("overrides", None)
        if overrides:
            record["overrides"] = overrides
        return record

    # -- the one write path ----------------------------------------------------------------------------------------------

    def _apply(self, wid: str, actor: Actor, if_match: int | None,
               build: Callable[[Mapping[str, Any], int, RegistrySnapshot], tuple[dict[str, Any] | None, str]]) -> Commit:
        with self._lock:
            for _attempt in range(3):
                stored, generation = self._read()
                if if_match is not None and if_match != generation:
                    raise RegistryConflict(generation)
                base = self.snapshot()
                record, change = build(stored, generation, base)
                expected = generation if if_match is None else if_match
                new_stored = copy.deepcopy(stored)
                workflows = dict(new_stored.get("workflows") or {})
                if record is None:
                    workflows.pop(wid, None)
                else:
                    workflows[wid] = {registry_layers.REPLACE_KEY: record} if wid in self._bundled_ids() else record
                new_stored["workflows"] = workflows
                try:
                    prospective = self._snapshot_of(new_stored, expected + 1)
                except Exception as error:   # noqa: BLE001 - a revision the builder refuses is never stored
                    raise RegistryRefused((ContractError((), SNAPSHOT_REFUSED,
                                                         f"the registry would not build: {type(error).__name__}"),)) from error
                if record is not None and wid in prospective.held_out:   # a save never stores a workflow that cannot serve
                    raise RegistryRefused(prospective.held_out[wid])
                entry = prospective.get(wid)
                commit = Commit(revision=expected + 1, workflow_id=wid,
                                record_revision=record["record_revision"] if record else 0,
                                contract_digest=entry.contract_digest if entry is not None else None,
                                record_digest=entry.record_digest if entry is not None else None)
                history = self._stage_history(wid, actor, change, record, commit)
                try:
                    written = admin_store.write_service_registry_with_audit(
                        new_stored,
                        expected_generation=expected,
                        updated_by=actor.id,
                        audit_action=f"operator.workflow.{change}",
                        audit_actor_kind=actor.kind,
                        audit_actor_id=actor.id,
                        audit_actor_label=actor.label,
                        audit_target_kind="workflow",
                        audit_target_id=wid,
                        audit_metadata={"workflow_id": wid, "change": change,
                                        "record_revision": record["record_revision"] if record else None},
                        session=actor.browser_session,
                        path=self._path,
                        stored_form=True,
                        also_in_transaction=history.apply,
                    )
                except admin_store.OperatorSettingsGenerationConflict:
                    history.undo()
                    if if_match is not None:
                        raise RegistryConflict(self._read()[1]) from None
                    continue   # a create retries on the new revision
                except service_registry.NameClash as clash:
                    history.undo()
                    raise RegistryRefused(tuple(clash.problems)) from None
                except BaseException:
                    history.undo()
                    raise
                break
            else:
                raise RegistryConflict(self._read()[1])
            revision = int(written["operator_settings_generation"])
            self._cached = (revision, service_registry.release_digest(), prospective)
            self._publish(base, prospective, commit)
            return commit

    def _publish(self, old: RegistrySnapshot, new: RegistrySnapshot, commit: Commit) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception as error:   # noqa: BLE001
                raise RegistryNotApplied(type(error).__name__, commit=commit) from error
        for listener in list(self._listeners):
            try:
                listener(old, new)
            except Exception:   # noqa: BLE001 - a listener never stops the swap or the other listeners
                log.exception("registry listener failed")

    def subscribe(self, listener: Callable[[RegistrySnapshot, RegistrySnapshot], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    # -- history ---------------------------------------------------------------------------------------------------------

    @staticmethod
    def _holder(workflow_id: str) -> str:
        return f"registry:{workflow_id}"

    def _rows_with_sha(self, workflow_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = []
        for sha in self._definitions.holder_shas(self._holder(workflow_id)):
            try:
                rows.append((sha, json.loads(self._definitions.read_blob(sha))))
            except Exception:   # noqa: BLE001 - a row that cannot be read is left out, not fatal
                log.warning("a history row of a workflow could not be read")
        rows.sort(key=lambda pair: pair[1]["record_revision"], reverse=True)
        return rows

    def _history_rows(self, workflow_id: str) -> list[dict[str, Any]]:
        return [row for _sha, row in self._rows_with_sha(workflow_id)]

    def _stage_history(self, workflow_id: str, actor: Actor, change: str, record: Mapping[str, Any] | None,
                       commit: Commit) -> _StagedHistory:
        """The history write of one accepted change, made part of its registry commit. The blob is put first (it holds
        nothing until the holder names it); ``apply`` runs inside the registry transaction, after the registry write and
        before its commit, so a history store that fails rolls the change back; ``undo`` puts the holder back when the
        registry commit then fails. A row left by a crash between the two has a revision the next real change reuses, and
        that change drops it."""
        holder = self._holder(workflow_id)
        before = tuple(self._definitions.holder_shas(holder))
        if record is None:
            return _StagedHistory(self._definitions, holder, before, None)
        body = {"record_revision": commit.record_revision, "record_digest": commit.record_digest,
                "contract_digest": commit.contract_digest, "saved_at_s": int(self._clock()),
                "actor": {"kind": actor.kind, "id": actor.id, "label": actor.label}, "change": change,
                "record": record}
        sha = self._definitions.put_blob(canonical_bytes(body))
        depth = history_depth_from_config(self._config())
        older = [held for held, row in self._rows_with_sha(workflow_id)
                 if held != sha and row["record_revision"] < commit.record_revision][: max(depth - 1, 0)]
        return _StagedHistory(self._definitions, holder, before, (sha, *older))

    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]:
        return tuple(
            HistoryEntry(record_revision=row["record_revision"], record_digest=row["record_digest"],
                         contract_digest=row["contract_digest"], saved_at_s=row["saved_at_s"],
                         actor=Actor(**row["actor"]), change=row["change"])
            for row in self._history_rows(workflow_id)[: max(int(limit), 0)]
        )


__all__ = ["Bounds", "RegistryNotApplied", "WorkflowIdIsBundled", "WorkflowRegistryStore", "bound_problems",
           "bounds_from_config", "contract_problems", "facts_record", "history_depth_from_config", "inline_templates",
           "template_depth"]
