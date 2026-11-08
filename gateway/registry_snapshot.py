"""One immutable view of what can run: the registry snapshot, its name view and its provider (CODE_TO_DATA_S4_S8, I1, S5).

``build`` reads the service-registry document at one generation, plus the bundled release, and returns a
``RegistrySnapshot``: the workflow entries, the frontend rows and every name a request can address. The build is strict
(a bad document raises ``SnapshotBuildError`` and the caller keeps the snapshot it has). Every mapping in the result is
a read-only view. ``SnapshotProvider.current()`` is the only way code reaches the registry: a request takes it once and
every read of that request uses it; a reload builds a new snapshot and swaps the reference in one assignment.

This module names no intent, workflow, model or engine. Names come from the document, the bundled workflows and the
frontend rows; the shared rules (canonical tokens, generated names, digests, clashes) are ``gateway/workflow_kernel.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from gateway import binding_descriptors
from gateway.alias_policy import service_model_records
from gateway.binding_descriptors import BindingDescriptor
from gateway.discovery import WorkflowRecord
from gateway.manifests import BUNDLED_PROVENANCE, ContractError, Provenance
from gateway.workflow_kernel import (
    canonical_token,
    digest,
    execution_digest,
    generated_names,
    name_clashes,
    route_key,
    selector_problems,
)
from gateway.workflow_policy import PolicyFacts
from gateway.workflow_runtime_types import (
    BindingDef,
    FrontendRow,
    NameKind,
    NameOwner,
    RouteSpec,
    TemplateRef,
    WorkflowEntry,
    WorkflowFacts,
)

ROOT = Path(__file__).resolve().parent.parent
FRONTENDS_PATH = ROOT / "registry" / "protocol_frontends.yml"
BUNDLED_STATE = "active"
BUNDLED_INCARNATION = "bundled"
# The stable alias tables of the model-alias config section: operation -> {stable: {name: target}, openai: [{match, to}]}.
_GLOB_CHARS = "*?["


class SnapshotBuildError(ValueError):
    """The document or the release cannot form one valid revision. ``problems`` are the reasons with their paths."""

    def __init__(self, problems: Iterable[ContractError]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(str(problem) for problem in self.problems) or "the registry does not build")


def _problem(path: tuple[str | int, ...], code: str, message: str) -> ContractError:
    return ContractError(tuple(path), code, message)


# ------------------------------------------------------------------------------------------------ read-only values


def frozen(value: Any) -> Any:
    """``value`` as read-only views: mappings become ``MappingProxyType`` and lists become tuples, recursively."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): frozen(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(frozen(child) for child in value)
    return value


# --------------------------------------------------------------------------------------------------- bundled release


@dataclass(frozen=True)
class BundledRelease:
    """What the release ships: the workflow records (``discovery.WorkflowRecord``, with the facts the release policy
    states for them) and the frontend rows."""

    workflows: Mapping[str, WorkflowRecord]
    frontends: tuple[FrontendRow, ...] = ()


def bundled_release(*, workflows_dir: Path | None = None, frontends_path: Path | None = None) -> BundledRelease:
    """The release read from its files: ``manifests/workflows`` with ``registry/workflow_policy.yml``, and
    ``registry/protocol_frontends.yml``. A workflow file that does not load raises (the release must be whole)."""
    from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRegistry

    registry = WorkflowRegistry.from_directory(workflows_dir or BUNDLED_WORKFLOWS_DIR)
    if registry.errors:
        raise SnapshotBuildError(_problem(("workflows",), "release_workflow", f"{e.source_path.name}: {e.message}")
                                 for e in registry.errors)
    return BundledRelease({record.id: record for record in registry.records()},
                          load_frontend_rows(frontends_path or FRONTENDS_PATH))


# --------------------------------------------------------------------------------------------------- frontend rows


def _route_specs(raw: Mapping[str, Any]) -> tuple[RouteSpec, ...]:
    """The routes of one row. A row states them as ``routes`` (mappings) or, in the file's present form, as
    ``registered_routes`` (mappings with a singular ``alias``). Prefix strings under ``routes`` are mount prefixes, not
    addressable routes."""
    specs: list[RouteSpec] = []
    for key in ("routes", "registered_routes"):
        for item in raw.get(key) or ():
            if not isinstance(item, Mapping) or not isinstance(item.get("canonical"), str):
                continue
            aliases = item.get("aliases") if item.get("aliases") is not None else item.get("alias")
            if isinstance(aliases, str):
                aliases = [aliases]
            specs.append(RouteSpec(
                methods=tuple(str(m).upper() for m in (item.get("methods") or ())),
                canonical=item["canonical"],
                aliases=tuple(str(a) for a in (aliases or ())),
                runtime_policy=frozen(item.get("runtime_policy") or {}),
            ))
    return tuple(specs)


def frontend_row(raw: Mapping[str, Any]) -> FrontendRow:
    """One ``FrontendRow`` from a file record. ``selects`` is kept as the row wrote it and checked with the kernel's
    ``selector_problems`` when it is present."""
    row_id = raw.get("id")
    if not isinstance(row_id, str) or not row_id:
        raise SnapshotBuildError([_problem(("frontends",), "frontend_id", "a frontend row has no id")])
    selects = raw.get("selects") or {}
    if selects and not isinstance(selects, Mapping):
        raise SnapshotBuildError([_problem(("frontends", row_id, "selects"), "selector_shape", "selects is a mapping")])
    problems = selector_problems(selects) if selects else ()
    if problems:
        raise SnapshotBuildError(_problem(("frontends", row_id, *p.path), p.code, p.message) for p in problems)
    receipt_protocols = raw.get("receipt_protocols") or ()
    receipt = raw.get("receipt_protocol") or (receipt_protocols[0] if receipt_protocols else raw.get("protocol") or "")
    mode = raw.get("mode") or "job"
    if mode not in ("sync", "job"):
        raise SnapshotBuildError([_problem(("frontends", row_id, "mode"), "frontend_mode", "mode is sync or job")])
    return FrontendRow(
        id=row_id, shape=str(raw.get("shape") or ""), routes=_route_specs(raw), selects=frozen(selects),
        mode=mode, receipt_protocol=str(receipt),
        strictness="lenient" if raw.get("strictness") == "lenient" else "exact",
        lists_as_model=bool(raw.get("lists_as_model", False)),
    )


@lru_cache(maxsize=4)
def _frontend_rows(path: str, mtime_ns: int, size: int) -> tuple[FrontendRow, ...]:
    del mtime_ns, size   # part of the key: a changed file loads again
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    records = document.get("records") if isinstance(document, Mapping) else None
    if not isinstance(records, list):
        raise SnapshotBuildError([_problem(("frontends",), "frontend_file", f"{Path(path).name} holds no records list")])
    return tuple(frontend_row(record) for record in records if isinstance(record, Mapping))


def load_frontend_rows(path: Path = FRONTENDS_PATH) -> tuple[FrontendRow, ...]:
    stat = Path(path).stat()
    return _frontend_rows(str(path), stat.st_mtime_ns, stat.st_size)


# ---------------------------------------------------------------------------------------------------------- entries


@lru_cache(maxsize=512)
def _path_template_digest(path: str) -> str:
    """The digest of a bundled template file (its parsed JSON, in the kernel's encoding) when the release holds it, else
    of the reference. The definition store keys the same bytes."""
    target = ROOT / path
    try:
        if target.is_file():
            return digest(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return digest({"template_path": path})


def _template_of(descriptor: BindingDescriptor | None, body: Mapping[str, Any]) -> TemplateRef | None:
    """The template a binding names (a file) or carries (inline), found by the description's template field."""
    if descriptor is None:
        return None
    carried = binding_descriptors.inline_template(descriptor, body)
    if carried is not None:
        return TemplateRef(digest=digest(carried[1]), inline=frozen(carried[1]))
    path = binding_descriptors.template_path(descriptor, body)
    return TemplateRef(digest=_path_template_digest(path), path=path) if path else None


def _bindings_of(contract: Mapping[str, Any]) -> Mapping[str, BindingDef]:
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        return MappingProxyType({})
    descriptors = binding_descriptors.cached_shipped_binding_descriptors()
    return MappingProxyType({
        str(key): BindingDef(adapter_id=str(key), binding=frozen(body), binding_digest=digest(body),
                             template=_template_of(descriptors.get(str(key)), body))
        for key, body in bindings.items() if isinstance(body, Mapping)})


def _facts_of(facts: PolicyFacts | None) -> WorkflowFacts | None:
    if facts is None:
        return None
    return WorkflowFacts(exposure=facts.exposure, trust_tier=facts.trust_tier, resource_tier=facts.resource_tier,
                         resource_class=facts.resource_class, tags=tuple(facts.tags))


def _record_digest(contract: Mapping[str, Any], facts: WorkflowFacts | None, state: str, published: bool,
                   overrides: str | None) -> str:
    """The digest of the whole stored record: contract with annotations, facts, state, published, overrides."""
    return digest({"contract": contract, "facts": asdict(facts) if facts else None, "state": state,
                   "published": published, "overrides": overrides})


def _entry(contract: Mapping[str, Any], *, facts: WorkflowFacts | None, provenance: Provenance, state: str,
           published: bool, incarnation: str, record_revision: int, overrides: str | None) -> WorkflowEntry:
    return WorkflowEntry(
        id=str(contract["id"]), contract=frozen(contract), contract_digest=execution_digest(contract),
        record_digest=_record_digest(contract, facts, state, published, overrides), incarnation=incarnation,
        bindings=_bindings_of(contract), facts=facts, provenance=provenance,
        state=state, published=published, record_revision=record_revision, overrides=overrides)  # type: ignore[arg-type]


def _bundled_entry(record: WorkflowRecord, generation: int) -> WorkflowEntry:
    return _entry(record.manifest, facts=_facts_of(record.facts),
                  provenance=Provenance(record.identity.trusted, record.identity.source) if record.identity else BUNDLED_PROVENANCE,
                  state=BUNDLED_STATE, published=True, incarnation=BUNDLED_INCARNATION, record_revision=generation, overrides=None)


def _stored_facts(raw: Any) -> WorkflowFacts | None:
    from gateway.workflow_policy import facts_from_mapping

    return _facts_of(facts_from_mapping(raw))


def _stored_entry(workflow_id: str, record: Mapping[str, Any], generation: int,
                  bundled: Mapping[str, WorkflowRecord]) -> WorkflowEntry:
    """A stored record as an entry. The keys the store adds (``state``, ``published``, ``incarnation``,
    ``record_revision``, ``overrides``) are read when present; an absent key reads as the store's migration writes it:
    ``state`` follows ``enabled``, ``published`` is false, the incarnation is the id's first, the revision is the
    generation."""
    manifest = record.get("manifest")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("id"), str) or not manifest["id"]:
        raise SnapshotBuildError([_problem(("workflows", workflow_id, "manifest"), "record_manifest",
                                           "a stored workflow holds a manifest with an id")])
    if manifest["id"] != workflow_id:
        raise SnapshotBuildError([_problem(("workflows", workflow_id, "manifest", "id"), "record_id",
                                           "the record key and the manifest id differ")])
    state = record.get("state") or ("active" if record.get("enabled", True) else "disabled")
    if state not in ("active", "disabled"):
        raise SnapshotBuildError([_problem(("workflows", workflow_id, "state"), "record_state", "state is active or disabled")])
    provenance: Mapping[str, Any] = record["provenance"] if isinstance(record.get("provenance"), Mapping) else {}
    identity: Mapping[str, Any] = record["identity"] if isinstance(record.get("identity"), Mapping) else {}
    facts = _stored_facts(record.get("policy"))
    if facts is None and workflow_id in bundled and record.get("policy") is None:
        facts = _facts_of(bundled[workflow_id].facts)
    overrides = record.get("overrides")
    return _entry(
        manifest, facts=facts,
        provenance=Provenance(bool(identity.get("trusted", False)), identity.get("source") or provenance.get("source")),
        state=state, published=bool(record.get("published", False)),
        incarnation=str(record.get("incarnation") or "stored"),
        record_revision=int(record.get("record_revision") or generation),
        overrides=str(overrides) if isinstance(overrides, str) and overrides else None)


# ----------------------------------------------------------------------------------------------------------- names


class SnapshotNames:
    """The ``NameView``: every (name, owner) pair of one revision, looked up by canonical token. Immutable."""

    __slots__ = ("_pairs", "_by_kind", "_by_token")

    def __init__(self, pairs: Iterable[tuple[str, NameOwner]]) -> None:
        self._pairs = tuple(pairs)
        by_kind: dict[tuple[str, str], NameOwner] = {}
        by_token: dict[str, list[NameOwner]] = {}
        for name, owner in self._pairs:
            token = name if owner.kind == "route" else canonical_token(name)
            by_kind.setdefault((owner.kind, token), owner)
            if owner.kind != "route" and owner not in by_token.setdefault(token, []):
                by_token[token].append(owner)
        self._by_kind = MappingProxyType(by_kind)
        self._by_token = MappingProxyType({token: tuple(owners) for token, owners in by_token.items()})

    def owner(self, kind: NameKind, name: str) -> NameOwner | None:
        token = name if kind == "route" else canonical_token(name)
        return self._by_kind.get((kind, token))

    def owners(self, name: str) -> tuple[NameOwner, ...]:
        return self._by_token.get(canonical_token(name), ())

    def items(self) -> tuple[tuple[str, NameOwner], ...]:
        return self._pairs


def workflow_names(entry: WorkflowEntry) -> list[tuple[str, NameOwner]]:
    """The names one workflow holds: its id, its declared aliases and the kernel's generated names."""
    pairs = [(entry.id, NameOwner("workflow", entry.id))]
    pairs += [(str(alias), NameOwner("workflow_alias", entry.id)) for alias in (entry.contract.get("aliases") or ())]
    pairs += [(name, NameOwner(kind, entry.id)) for kind, name in generated_names(entry.id).items()]  # type: ignore[arg-type]
    return pairs


def _row_value(row: FrontendRow) -> dict[str, Any]:
    """A frontend row as plain JSON values, for the digest."""
    return {"id": row.id, "shape": row.shape, "mode": row.mode, "receipt_protocol": row.receipt_protocol,
            "strictness": row.strictness, "lists_as_model": row.lists_as_model, "selects": row.selects,
            "routes": [{"methods": list(r.methods), "canonical": r.canonical, "aliases": list(r.aliases),
                        "runtime_policy": r.runtime_policy} for r in row.routes]}


def frontend_names(row: FrontendRow) -> list[tuple[str, NameOwner]]:
    pairs = [(row.id, NameOwner("frontend", row.id))]
    for spec in row.routes:
        for path in (spec.canonical, *spec.aliases):
            pairs += [(route_key(method, path), NameOwner("route", row.id)) for method in spec.methods]
    return pairs


def service_model_names(services: Mapping[str, Any]) -> list[tuple[str, NameOwner]]:
    """Model ids and model aliases the services section declares. A record with a ``base_model_id`` is an alias and
    names the model it stands for; its ``model_group`` is a public name of the same alias."""
    pairs: list[tuple[str, NameOwner]] = []
    for service in services.values():
        config = service.get("config") if isinstance(service, Mapping) else None
        for model in service_model_records(config if isinstance(config, Mapping) else {}):
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            base = str(model.get("base_model_id") or "").strip()
            if not base:
                pairs.append((model_id, NameOwner("model", model_id)))
                continue
            pairs.append((model_id, NameOwner("model_alias", base)))
            group = str(model.get("model_group") or "").strip()
            if group:
                pairs.append((group, NameOwner("model_alias", base)))
    return pairs


def _model_of(services: Mapping[str, Any], service_id: str | None, model: str) -> str:
    """The model a target name ends at: a services alias stands for its base model; an engine entry's (service, upstream
    model) is the services-section model declaring that pair, when exactly one does. Otherwise the name itself."""
    found: list[str] = []
    for sid, service in services.items():
        if service_id is not None and str(sid) != service_id:
            continue
        config = service.get("config") if isinstance(service, Mapping) else None
        for record in service_model_records(config if isinstance(config, Mapping) else {}):
            model_id = str(record.get("id") or "").strip()
            upstream = str(record.get("upstream_model") or "").strip()
            declared = model_id if service_id is None else (upstream or model_id)
            if declared == model:
                found.append(str(record.get("base_model_id") or "").strip() or model_id)
    return found[0] if len(set(found)) == 1 else model


def dynamic_alias_names(model_aliases: Mapping[str, Any] | None,
                        services: Mapping[str, Any] | None = None) -> list[tuple[str, NameOwner]]:
    """The names of the ``model_aliases`` config section: each operation's stable names and its exact (non-glob) matches.
    A dynamic alias resolves per operation, so one name in two operations is one name. Its owner is the model it ends at
    (``_model_of``; an engine table's entry goes through its service and upstream model) when every operation's stable
    entry ends at that one model, so the services section may declare the same name for that model without a clash.
    Otherwise, or when it points nowhere, or when a pattern names it, it is its own entity."""
    services = services or {}
    names: list[str] = []
    targets: dict[str, set[str]] = {}
    for table in (model_aliases or {}).values():
        if not isinstance(table, Mapping):
            continue
        engines = table.get("engines") if isinstance(table.get("engines"), Mapping) else {}
        stable = table.get("stable") or {}
        for name, target in (stable.items() if isinstance(stable, Mapping) else ()):
            names.append(str(name))
            for value in (target if isinstance(target, list) else [target]):
                if not (isinstance(value, str) and value.strip()):
                    continue
                engine = engines.get(value.strip())
                if isinstance(engine, Mapping) and engine.get("service") and engine.get("model"):
                    ends_at = _model_of(services, str(engine["service"]), str(engine["model"]))
                else:
                    ends_at = _model_of(services, None, value.strip())
                targets.setdefault(str(name), set()).add(ends_at)
        for pattern in table.get("openai") or ():
            if isinstance(pattern, Mapping):
                match = str(pattern.get("match") or "")
                names += [match] if match and not any(c in match for c in _GLOB_CHARS) else []
                names += [str(n) for n in (pattern.get("names") or ()) if isinstance(n, str)]
                # An OpenAI-name pattern points at a stable name, not a model: its names stay their own entity.
                for name in (n for n in [match, *(pattern.get("names") or ())] if isinstance(n, str)):
                    targets.setdefault(name, set()).add("\0pattern")
    pairs: list[tuple[str, NameOwner]] = []
    for name in names:
        if not name.strip():
            continue
        named = targets.get(name, set())
        owner = next(iter(named)) if len(named) == 1 and "\0pattern" not in named else name
        pairs.append((name, NameOwner("model_alias", owner)))
    return pairs


# ------------------------------------------------------------------------------------------------------- the snapshot


@dataclass(frozen=True)
class Snapshot:
    """The ``RegistrySnapshot``. Built only by ``build``; all of its mappings are read-only views."""

    revision: int
    digest: str
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow]
    names: SnapshotNames
    _intents: Mapping[str, tuple[WorkflowEntry, ...]] = field(repr=False, compare=False, default_factory=dict)
    # Where a bundled workflow was loaded from, for the admin view only; never part of the digest or a decision.
    source_paths: Mapping[str, str] = field(repr=False, compare=False, default_factory=dict)

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        return self.workflows.get(workflow_id)

    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]:
        return self._intents.get(intent, ())


def build(document: Mapping[str, Any] | None, bundled: BundledRelease | Mapping[str, WorkflowRecord], *, generation: int,
          model_aliases: Mapping[str, Any] | None = None) -> Snapshot:
    """The snapshot of ``document`` (the service-registry document at ``generation``) over the bundled release.

    A stored workflow replaces the bundled one of its id; the replaced entry's names leave the view. Strict: a record that
    is not a record, a frontend row that does not load, or a name two owning entities hold raises ``SnapshotBuildError``.
    ``model_aliases`` is the ``model_aliases`` config section (the dynamic aliases), when the caller has it."""
    release = bundled if isinstance(bundled, BundledRelease) else BundledRelease(bundled, load_frontend_rows())
    document = document or {}
    stored = document.get("workflows", {})
    if not isinstance(stored, Mapping):
        raise SnapshotBuildError([_problem(("workflows",), "section", "the workflows section is a mapping")])
    services = document.get("services", {})
    if not isinstance(services, Mapping):
        raise SnapshotBuildError([_problem(("services",), "section", "the services section is a mapping")])
    entries: dict[str, WorkflowEntry] = {wid: _bundled_entry(record, generation) for wid, record in release.workflows.items()}
    for workflow_id, record in stored.items():
        if not isinstance(record, Mapping):
            raise SnapshotBuildError([_problem(("workflows", str(workflow_id)), "record", "a stored workflow is an object")])
        if record.get("migration"):   # a record the schema step quarantined never runs; the bundled version of its id serves
            continue
        entries[str(workflow_id)] = _stored_entry(str(workflow_id), record, generation, release.workflows)
    frontends = {row.id: row for row in release.frontends}
    if len(frontends) != len(release.frontends):
        raise SnapshotBuildError([_problem(("frontends",), "frontend_id", "two frontend rows share an id")])
    pairs: list[tuple[str, NameOwner]] = []
    for entry in entries.values():
        pairs += workflow_names(entry)
    for row in frontends.values():
        pairs += frontend_names(row)
    pairs += service_model_names(services)
    pairs += dynamic_alias_names(model_aliases, services)
    problems = name_clashes(pairs)
    if problems:
        raise SnapshotBuildError(problems)
    names = SnapshotNames(pairs)
    intents: dict[str, list[WorkflowEntry]] = {}
    for entry in entries.values():
        intents.setdefault(entry.intent, []).append(entry)
    ordered = MappingProxyType(dict(sorted(entries.items())))
    snapshot_digest = digest({
        "workflows": [[e.id, e.record_digest, e.incarnation] for e in ordered.values()],
        "frontends": [_row_value(row) for row in frontends.values()],
        "names": sorted([name, owner.kind, owner.id] for name, owner in pairs),
    })
    return Snapshot(revision=generation, digest=snapshot_digest, workflows=ordered, frontends=MappingProxyType(frontends),
                    names=names, _intents=MappingProxyType({k: tuple(v) for k, v in intents.items()}),
                    source_paths=MappingProxyType({wid: str(release.workflows[wid].source_path) for wid, entry in ordered.items()
                                                   if entry.incarnation == BUNDLED_INCARNATION and wid in release.workflows}))


def load_current(path: Any, *, model_aliases: Mapping[str, Any] | None = None) -> Snapshot:
    """The snapshot of the service-registry document stored at ``path``: the revision is its CAS generation. Raises
    ``SnapshotBuildError`` when the revision does not build, so the caller keeps the snapshot it serves."""
    from gateway import admin_store, service_registry

    record = admin_store.get_service_registry_json_with_generation(path=path)
    document = service_registry.load_service_registry(path=path) or {}
    generation = int(record["operator_settings_generation"]) if record else 0
    return build(document, bundled_release(), generation=generation, model_aliases=model_aliases)


# --------------------------------------------------------------------------------------------------------- provider


class Provider:
    """The ``SnapshotProvider``. ``swap`` replaces the held snapshot in one assignment; a reader sees the old one or
    the new one whole."""

    __slots__ = ("_snapshot",)

    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self._snapshot = snapshot

    def current(self) -> Snapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("no registry snapshot has been built yet")
        return snapshot

    def swap(self, snapshot: Snapshot) -> Snapshot | None:
        """Make ``snapshot`` the current one; the previous one is returned."""
        previous, self._snapshot = self._snapshot, snapshot
        return previous


def snapshot_of(request: Any, provider: Provider) -> Snapshot:
    """The snapshot for one request: the one its scope hook stored, else the provider's current one (kept on the request
    so every later read of that request sees the same revision)."""
    state = request.state
    held = getattr(state, "registry_snapshot", None)
    if held is None:
        held = provider.current()
        state.registry_snapshot = held
    return held
