"""DB-backed service and protocol registry helpers.

The gateway has two different kinds of configuration:

* bootstrap/provisioning inputs needed before the process can start;
* runtime product state operators manage from the dashboard/API.

The registry is layered (docs/design/CONFIG_HOMES.md, 3.4). The release layer is the bundled ``registry/`` catalogs.
``admin.db`` stores only the owner's data: records the owner made, field-level patches of release records, and
tombstones (``gateway/registry_layers.py``). ``admin_store`` layers on read and unlayers on write, so every reader sees
the one effective registry. ``ensure_service_registry`` runs the one-time ``config-homes-registry-layering`` step on an
install that stored a whole document. The startup file's ``backends`` are never read. A service row's ``source`` is provenance, not
permission: every non-managed row remains editable, enableable/disableable,
and removable in the DB/UI registry. The managed LiteLLM substrate is the sole
exception: its endpoint/protocol fields and removal are locked, while its name,
enabled state, and note remain editable.
"""

from __future__ import annotations


import copy
import hashlib
import json
import re
import time
from datetime import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from gateway import admin_store, engine_check, native_lane, projection, record_carriers, registry_layers, url_binding
from gateway import secret_refs as _refs
from gateway.secret_store import credential_origin_from_url
from gateway.capabilities import Capability, ResourceEstimate, summarize_capability
from gateway.capability_ids import MODEL_KIND_LABELS, capabilities_without_inner_endpoint, offered_model_kinds
from gateway.discovery import WorkflowRecord, bundled_workflow_manifests, summarize_workflow
from gateway.manifests import Provenance, enforce_contract, manifest_identity
from gateway.workflow_policy import FACT_NAMES, NEEDS_CONFIGURATION_CODE, release_facts, stored_facts
from gateway.transition_gate import ServicePaths, domain_violations
from gateway import frontend_profiles, transition_profiles


SERVICE_REGISTRY_SETTING_KEY = "service_registry"
SERVICE_REGISTRY_SCHEMA_VERSION = 2
ENGINE_RECORD_VIEW_VERSION = 2
MODEL_POLICY_SCHEMA_VERSION = 1

SERVICE_SOURCES = frozenset({"ui", "provisioned", "builtin", "discovered", "imported"})
PROTOCOL_FRONTEND_SOURCES = frozenset({"builtin", "ui", "provisioned", "imported"})
DATA_OBJECT_SOURCES = frozenset({"builtin", "ui", "provisioned", "discovered", "imported"})
DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent.parent / "registry"
REMOTE_PROVIDER_TYPES = frozenset({"remote", "paid-remote", "cloud"})
# Protocol dispatch accepts only these classes. Any other provider type or cost class is refused,
# so a new paid or remote name fails closed rather than needing its own entry here.
LOCAL_PROVIDER_CLASSES = frozenset({"local", "free-local"})
ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Where a plaintext credential is kept (the secret store, not the record) is decided by the name parts and
# fields below; what a view shows is decided by the projection types, by structure, not by these.
_SECRET_HINTS = ("api_key", "authorization", "client_secret", "credential", "password", "secret", "token")
# Config fields that hold a credential inline. The stored form moves these into ``secret_refs``; a record from an
# older install or a hand edit may still hold one, and a remote provider may not.
_INLINE_SECRET_FIELDS = frozenset({
    "api_key", "authorization", "x_api_key", "client_secret", "password", "secret", "token", "access_token",
    "bearer_token", "credential",
})


def ensure_service_registry(*, path: Path = admin_store.DB_PATH, actor: str = "system") -> dict[str, Any]:
    """Return the DB-owned registry, effective: the release records under the owner's data.

    A database with no registry gets the empty owner document (it inherits every release record). A registry stored
    before the layer (docs/design/CONFIG_HOMES.md, 8.3) is migrated once to the owner-data form, and the step is
    recorded in the same write. Nothing is seeded from ``proxy_config.yml`` and no release field is copied into the
    store: the release is read at every load. Bounded CAS retry, as concurrent boots are legitimate.
    """
    release = release_registry()
    for _attempt in range(3):
        record = admin_store.get_service_registry_json_with_generation(path=path)
        metadata: dict[str, Any] = {}
        step: str | None = registry_layers.LAYERING_STEP
        if record is None:
            stored = registry_layers.empty_stored(SERVICE_REGISTRY_SCHEMA_VERSION)
            action = "operator.service_registry.seed"
            expected_generation = 0
        else:
            if not _parses_as_object(record["value_json"]):
                # A damaged value would read as an empty document and migrate to an empty owner layer, replacing the
                # owner's whole registry on this start. Leave it as stored, for a restore or an import, and serve the
                # release records. The message names the setting, never its contents.
                print(f"[service_registry] ERROR: the stored {SERVICE_REGISTRY_SETTING_KEY!r} is not a JSON object; "
                      "it is left as stored and the release records are served. Restore it from a backup or import "
                      "a registry.", flush=True)
                return normalize_service_registry(
                    registry_layers.layer(registry_layers.empty_stored(SERVICE_REGISTRY_SCHEMA_VERSION), release))
            stored = _stored_document(record["value_json"])
            if registry_layers.is_layered(stored):
                if not admin_store.migration_step_recorded(registry_layers.LAYERING_STEP, path=path):
                    admin_store.record_migration_step(registry_layers.LAYERING_STEP, path=path)
                return normalize_service_registry(registry_layers.layer(_with_workflow_schema_2(stored, path, actor, release), release))
            from gateway import registry_migration

            plan = registry_migration.plan_layering(
                stored, release, load_seed_catalogs({}), owner_edited_services=registry_migration.owner_edited_services(path))
            stored = plan.stored
            action = "operator.service_registry.layering"
            expected_generation = int(record["operator_settings_generation"])
            metadata = {"migration_step": step, "patched": len(plan.patches),
                        "tombstoned": sum(len(ids) for ids in plan.tombstones.values())}
        try:
            admin_store.write_service_registry_with_audit(
                stored,
                expected_generation=expected_generation,
                updated_by=actor,
                audit_action=action,
                audit_actor_kind="admin",
                audit_actor_id=actor,
                audit_actor_label="System",
                audit_target_kind="operator_setting",
                audit_target_id=SERVICE_REGISTRY_SETTING_KEY,
                audit_metadata={
                    "service_count": len(stored.get("services") or {}),
                    "protocol_frontend_count": len(stored.get("protocol_frontends") or {}),
                    **metadata,
                },
                session=None,  # the gateway migrating its own registry at start: no browser sign-in asked
                path=path,
                migration_step=step,
                stored_form=True,
            )
        except admin_store.OperatorSettingsGenerationConflict:
            continue
        return normalize_service_registry(registry_layers.layer(_with_workflow_schema_2(stored, path, actor, release), release))

    raise RuntimeError("service registry changed during bounded startup retry")


def _with_workflow_schema_2(stored: dict[str, Any], path: Path, actor: str, release: Mapping[str, Any]) -> dict[str, Any]:
    """``stored`` after the one-time ``workflow-schema-2`` step (CODE_TO_DATA_S3, D3), which runs once, after layering and
    before anything normalizes the workflow records: a schema 1 record would not load."""
    from gateway import workflow_schema_migration

    migrated = workflow_schema_migration.ensure(path=path, actor=actor, release=release)
    stored = stored if migrated is None else migrated
    stepped = ensure_workflow_record_step(path=path, actor=actor)
    return stored if stepped is None else stepped


def _parses_as_object(document: str) -> bool:
    try:
        return isinstance(json.loads(document), dict)
    except (TypeError, ValueError):
        return False


def _stored_document(document: str) -> dict[str, Any]:
    """The stored JSON as an object; a damaged or non-object value reads as an empty one, as ``get_operator_setting`` does."""
    try:
        value = json.loads(document)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=4)
def _release_registry(directory: str, signature: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    catalogs = load_seed_catalogs({"registry": {"catalog_dir": directory}})
    document = normalize_service_registry({
        "schema_version": 1,
        **{name: catalogs.get(name) or {} for name, _kind in _SECTIONS},
        "updated_at": 1,
    })
    # The release names the host its own key is meant for, as a seed always did.
    document = bind_credential_origins(document, document["services"])
    migrate_engine_records_v2(document)
    registry, excluded = read_registry(document)
    if excluded:
        raise ValueError(f"the release records do not validate: {excluded[0].name}: {excluded[0].error}")
    return {"schema_version": registry["schema_version"], **{name: registry[name] for name, _kind in _SECTIONS}}


def _release_key(catalog_dir: Path | None) -> tuple[str, tuple[tuple[str, int, int], ...]]:
    directory = Path(catalog_dir or DEFAULT_CATALOG_DIR)
    signature: list[tuple[str, int, int]] = []
    for name in ("services.yml", "protocol_frontends.yml", "workflows.yml", "capabilities.yml"):
        try:
            stat = (directory / name).stat()
        except FileNotFoundError:
            continue
        signature.append((name, stat.st_mtime_ns, stat.st_size))
    return str(directory), tuple(signature)


def release_registry(catalog_dir: Path | None = None) -> dict[str, Any]:
    """The release layer: the records bundled in ``registry/``, normalized the way a stored record is. A fresh copy."""
    return copy.deepcopy(_release_registry(*_release_key(catalog_dir)))


@lru_cache(maxsize=4)
def _release_digest(directory: str, signature: tuple[tuple[str, int, int], ...]) -> str:
    return registry_layers.digest(_release_registry(directory, signature))


def release_digest() -> str:
    """A label of the release records, so a cache of a layered registry never outlives a change of the release."""
    return _release_digest(*_release_key(None))


def effective_registry(stored: Mapping[str, Any]) -> tuple[dict[str, Any], list["ExcludedRecord"]]:
    """The registry in use: a stored document (the owner's patches) layered over the release records, then read.

    Every reader of the stored row goes through this. The row alone holds only the owner's changes, so a
    service patched there can lack fields, such as ``base_url``, that its release record supplies.
    """
    return read_registry(registry_layers.layer(stored, release_registry()))


def load_service_registry(*, path: Path = admin_store.DB_PATH) -> dict[str, Any] | None:
    record = admin_store.get_service_registry_json_with_generation(path=path)
    if record is None:
        return None
    snapshot, _excluded, default_timestamp = _normalized_registry_snapshot(
        record["operator_settings_generation"], record["value_json"], release_digest(),
    )
    # Existing callers may edit these dictionaries before submitting a CAS
    # write. Never expose the cached object to a caller's mutations.
    registry = copy.deepcopy(snapshot)
    if default_timestamp:
        registry["updated_at"] = int(time.time())
    return registry


def load_service_registry_exclusions(*, path: Path = admin_store.DB_PATH) -> list[ExcludedRecord]:
    """The stored records ``load_service_registry`` leaves out, each with its reason; none for a healthy registry."""
    record = admin_store.get_service_registry_json_with_generation(path=path)
    if record is None:
        return []
    _registry, excluded, _default = _normalized_registry_snapshot(
        record["operator_settings_generation"], record["value_json"], release_digest(),
    )
    return [replace(item, record=copy.deepcopy(item.record)) for item in excluded]


@lru_cache(maxsize=16)
def _normalized_registry_snapshot(
    generation: int, document: str, release: str,
) -> tuple[dict[str, Any], tuple["ExcludedRecord", ...], bool]:
    # The CAS generation and JSON are read by one SELECT. Including the JSON
    # also separates databases and protects against a restored/recreated DB
    # reusing a generation. The release digest keeps a cached registry from outliving a change of the release
    # records it was layered over. No TTL or process-local writer notification.
    # Match get_operator_setting's treatment of damaged/non-object JSON.
    value = _stored_document(document)
    registry, excluded = effective_registry(value)
    for record in excluded:
        # Once per stored document, not once per request: this body runs only when the cache misses.
        print(f"[service_registry] ERROR: {record.section} record {record.id!r} is left out of the registry: {record.error}", flush=True)
    return registry, tuple(excluded), not bool(value.get("updated_at"))




def normalize_service_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    """The whole registry, normalized. Raises on the first record that does not validate (see ``read_registry``)."""
    return _read_registry(value, strict=True)[0]


# The sections of a registry document, with the word the Overview puts before a record's id.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("services", "service"),
    ("protocol_frontends", "frontend"),
    ("workflows", "workflow"),
    ("capabilities", "capability"),
)


@dataclass(frozen=True)
class ExcludedRecord:
    """One stored record the gateway does not use because it does not validate, with the reason and the stored value."""

    section: str
    key: str
    id: str
    error: str
    record: Any

    @property
    def name(self) -> str:
        """The key the Overview lists it under: a service by its id, any other record as ``kind:id``."""
        kind = dict(_SECTIONS)[self.section]
        return self.id if self.section == "services" else f"{kind}:{self.id}"


WORKFLOW_SCHEMA_2_STEP = "workflow-schema-2"
_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")  # a stored code is accepted only as a short identifier


def _left_by_workflow_migration(raw: Any) -> list[str] | None:
    """The error codes of a stored workflow record the schema 2 migration marked (a ``migration`` block that names the
    step; the record keeps the owner's own ``enabled``), or ``None`` for any other record. The codes only: the stored
    messages may quote a value."""
    if not isinstance(raw, Mapping):
        return None
    block = raw.get("migration")
    if not isinstance(block, Mapping) or block.get("step") != WORKFLOW_SCHEMA_2_STEP:
        return None
    errors = block.get("errors")
    codes = [item["code"] for item in errors
             if isinstance(item, Mapping) and isinstance(item.get("code"), str) and _ERROR_CODE.fullmatch(item["code"])] if isinstance(errors, list) else []
    return list(dict.fromkeys(codes))[:5]


def _bounded_scrubbed(message: str, record: Mapping[str, Any]) -> str:
    """The message with every value the record holds redacted, then cut to 300 characters. The scrub runs on the whole
    text, so a cut can never leave the start of a value the scrub would have redacted."""
    scrubbed = projection.scrub_text(message, [*record_carriers.legacy_secret_texts(record), *record_carriers.typed_value_texts(record)])
    return scrubbed[:300]


def read_registry(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[ExcludedRecord]]:
    """The records of a stored registry that validate, and each one that does not, with why.

    A record that is not an object, has no usable id, fails its own checks, or shares a public model
    alias with another service is left out; the rest are normalized exactly as ``normalize_service_registry``
    does. This is the read every request and operator path uses, so one bad record never stops the others.
    Nothing is written back: a writer carries the excluded records through (``gateway/admin_runtime.py``).
    """
    return _read_registry(value, strict=False)


def _service_paths(service_id: str, record: Mapping[str, Any]) -> ServicePaths:
    """The ways a request reaches ``record``, split into the hops that lease and the ones that do not.

    Leased: the model router's lifecycle hop (openai-compatible, ``config.gateway_lifecycle``), protocol
    dispatch (a declared http-v1 engine protocol) and the native lane. Unleased: the model router at the
    service's own address, and a reviewed-adapter protocol (its hops hold no lease in step T1.1).
    """
    from gateway.engine_protocol import description
    from gateway.litellm_substrate import _is_openai_compatible, reaches_engine_directly

    leased: list[str] = []
    unleased: list[str] = []
    service = normalize_service_record(service_id, record)
    config = service["config"]
    openai = _is_openai_compatible(service)
    unserved: list[str] = []
    if openai:
        if reaches_engine_directly(service_id, record):
            unleased.append("the model router at the engine's own address")
        else:
            leased.append("the gateway lifecycle hop")
            unserved = capabilities_without_inner_endpoint(service.get("capabilities") or [])
    connection = record.get("connection") if isinstance(record.get("connection"), Mapping) else config.get("connection")
    protocol = connection.get("protocol") if isinstance(connection, Mapping) else None
    if isinstance(protocol, str) and protocol.strip() and not openai:
        doc = description.load_registry().get(protocol.strip())
        if doc is None:
            unleased.append(f"unknown protocol {protocol.strip()!r}")
        elif doc.adapter_id is not None:
            unleased.append(f"reviewed-adapter protocol {protocol.strip()!r}")
        elif doc.executor == "http-v1":
            leased.append("protocol dispatch")
        else:
            unleased.append(f"protocol {protocol.strip()!r}")
    if bool_from_config(config.get("native_passthrough", record.get("native_passthrough")), False):
        leased.append("the native lane")
    return ServicePaths(leased=tuple(leased), unleased=tuple(unleased), unserved_capabilities=tuple(unserved))


def _transition_domain_errors(services: Mapping[str, Any]) -> dict[str, str]:
    """``{service_id: error}`` for each service a transition-enabled residency domain cannot hold.

    The transition gate leases at the gateway's hops only, and step T1.1 orders no
    gateway-run activation (gateway/transition_gate.py). So a member of an enabled
    domain must declare pass-through activation, no gateway-owned lifecycle and no
    gateway-run selection, and every path to it must be a leased hop.
    """
    return domain_violations(services, paths=_service_paths, profile_known=transition_profiles.known)


def _enforce_transition_domains(services: dict[str, Any], *, strict: bool) -> dict[str, str]:
    """Run the domain check on ``services`` after a step that can change them (backfill).

    ``strict`` raises on the first violation; otherwise each violating service is removed from
    ``services`` (it fails closed) and named in the returned ``{service_id: error}``.
    """
    errors = _transition_domain_errors(services)
    if strict and errors:
        raise ValueError(next(iter(errors.values())))
    for service_id in errors:
        del services[service_id]
    return errors


def runtime_transition_errors(backends: Mapping[str, Any]) -> dict[str, str]:
    """``_transition_domain_errors`` on the flat runtime backend map the registry projects.

    A flat backend is its record's ``config`` plus the promoted fields; wrap it back into a record shape.
    """
    shaped: dict[str, Any] = {}
    for backend_id, backend in backends.items():
        if not isinstance(backend, Mapping):
            continue
        record: dict[str, Any] = {"id": backend_id, "config": dict(backend)}
        for key in ("name", "enabled", "capabilities", "priority", "role", "secret_refs", "residency", "process",
                    "offerings", "observations", "lifecycle"):
            if key in backend:
                record[key] = backend[key]
        shaped[str(backend_id)] = record
    return _transition_domain_errors(shaped)


def servable_part_of_registry(value: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``read_registry`` with the exclusions as ``{name: error}`` (a service by id, any other as ``kind:id``)."""
    registry, excluded = read_registry(value)
    return registry, {record.name: record.error for record in excluded}


def registry_error_text(text: str, value: Mapping[str, Any]) -> str:
    """``text``, an error about a stored registry, without any value a left-out record of it holds."""
    secrets = [secret for item in read_registry(value)[1]
               for secret in (*record_carriers.legacy_secret_texts(item.record), *record_carriers.typed_value_texts(item.record))]
    return projection.scrub_text(text, secrets)


def _read_registry(value: Mapping[str, Any], *, strict: bool) -> tuple[dict[str, Any], list[ExcludedRecord]]:
    """Both reads in one place. ``strict`` raises where the lenient read leaves a record out."""
    schema_version = int(value.get("schema_version") or SERVICE_REGISTRY_SCHEMA_VERSION)
    excluded: list[ExcludedRecord] = []
    stored_services: dict[str, tuple[str, Any]] = {}

    def read(section: str, build: Any) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        collection = value.get(section) or {}
        if not isinstance(collection, Mapping):
            raise ValueError(f"registry {section} must be an object")
        kind = dict(_SECTIONS)[section]
        for raw_key, raw in collection.items():
            record_id = normalize_id(raw.get("id") or raw_key) if isinstance(raw, Mapping) else normalize_id(raw_key)
            try:
                if not isinstance(raw, Mapping):
                    raise ValueError(f"{kind} {raw_key!r} is not an object")
                if not record_id:
                    raise ValueError(f"{kind} {raw_key!r} has no usable id")
                if normalize_id(raw_key) != record_id:
                    # A record under one key that names another id would, once normalized, replace or be replaced by the
                    # record that has that id, and take its secrets with it: the key and the id must say the same.
                    raise ValueError(f"{kind} {raw_key!r} names a different id; a record's id must match the key it is stored under")
                key, record = build(record_id, raw)
                if key in kept:
                    raise ValueError(f"{kind} {key!r} is defined twice")
            except Exception as error:  # noqa: BLE001 - one record's refusal never decides the others
                codes = _left_by_workflow_migration(raw) if section == "workflows" else None
                if codes is not None:
                    # The migration kept a record it could not convert, disabled, so the owner can fix it. It is not an
                    # error of the read, even a strict one: it is left out of the registry and listed as needing configuration.
                    bundled = record_id in bundled_workflow_manifests()
                    message = (f"{NEEDS_CONFIGURATION_CODE}: the schema 2 migration could not convert this workflow; it stays "
                               "out of use until you save a schema 2 document for it."
                               + (" The bundled version of this workflow is served in the meantime." if bundled else "")
                               + f" Reasons: {', '.join(codes) or 'none stored'}.")
                    # The scrub reads the record without its migration block: the codes are the block's own, already bounded
                    # above; a code that equals any other value of the record is still redacted.
                    message = _bounded_scrubbed(message, {key: value for key, value in raw.items() if key != "migration"})
                    excluded.append(ExcludedRecord(section, str(raw_key), record_id or str(raw_key), message, copy.deepcopy(raw)))
                    continue
                if strict:
                    raise
                message = str(error) if isinstance(error, ValueError) else f"{type(error).__name__}: {error}"
                message = _bounded_scrubbed(message, raw)
                excluded.append(ExcludedRecord(section, str(raw_key), record_id or str(raw_key), message, copy.deepcopy(raw)))
                continue
            kept[key] = record
            if section == "services":
                stored_services[key] = (str(raw_key), raw)
        return kept

    def build_service(service_id: str, raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        check_service_shape(service_id, raw)  # before the migration below, which would rewrite a block of the wrong shape
        prepared = raw
        if schema_version >= ENGINE_RECORD_VIEW_VERSION:
            prepared = copy.deepcopy(dict(raw))
            if "process" in prepared:
                prepared.pop("lifecycle", None)
                if isinstance(prepared.get("config"), dict):
                    prepared["config"].pop("lifecycle", None)
            else:
                temporary = {"schema_version": 1, "services": {service_id: prepared}}
                migrate_engine_records_v2(temporary)
                prepared = temporary["services"][service_id]
        record = normalize_service_record(service_id, prepared, schema_version=schema_version)
        from gateway.litellm_substrate import foreign_lifecycle_base_error

        foreign = foreign_lifecycle_base_error(record)
        if foreign is not None:
            raise ValueError(foreign)
        if not strict:
            _validate_model_policy_schema_markers({service_id: record})
            _validate_model_policy_graphs({service_id: record})
        return service_id, record

    def build_with_id(normalize: Any) -> Any:
        def build(record_id: str, raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
            record = normalize(record_id, raw)
            return record["id"], record
        return build

    def build_frontend(frontend_id: str, raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return frontend_id, normalize_protocol_frontend(frontend_id, raw)

    services = read("services", build_service)
    frontends = read("protocol_frontends", build_frontend)
    workflows = read("workflows", build_with_id(normalize_workflow_record))
    capabilities = read("capabilities", build_with_id(normalize_capability_record))

    transition_errors = _transition_domain_errors(services)
    if strict and transition_errors:
        raise ValueError(next(iter(transition_errors.values())))
    for service_id, why in transition_errors.items():
        del services[service_id]
        stored_key, stored = stored_services[service_id]
        excluded.append(ExcludedRecord("services", stored_key, service_id, why, copy.deepcopy(stored)))

    if strict:
        _validate_model_policy_schema_markers(services)
        _validate_model_policy_graphs(services)
        _validate_public_alias_uniqueness(services)
    else:
        for service_id, why in _later_alias_claimants(services).items():
            del services[service_id]
            stored_key, stored = stored_services[service_id]
            excluded.append(ExcludedRecord("services", stored_key, service_id, why, copy.deepcopy(stored)))

    return {
        "schema_version": schema_version,
        "services": services,
        "protocol_frontends": frontends,
        "workflows": workflows,
        "capabilities": capabilities,
        "seeded_from": str(value.get("seeded_from") or ""),
        "seed_hash": str(value.get("seed_hash") or ""),
        "updated_at": int(value.get("updated_at") or time.time()),
    }, excluded


def _validate_model_policy_schema_markers(services: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate an explicit models-collection marker without upgrading old DB data.

    Absence deliberately remains representable: it identifies an operator-owned
    pre-policy collection, which request preparation rejects with a typed
    configuration-unavailable response until an operator migrates it.
    """
    for service_id, service in services.items():
        config = service.get("config") if isinstance(service, Mapping) else None
        if not isinstance(config, Mapping):
            continue
        marker = config.get("model_policy_schema_version")
        if marker is None:
            continue
        if not isinstance(config.get("models"), list):
            raise ValueError(
                f"service {service_id!r} model_policy_schema_version requires a models list"
            )
        if isinstance(marker, bool) or not isinstance(marker, int) or marker < 0:
            raise ValueError(
                f"service {service_id!r} model_policy_schema_version must be a non-negative integer"
            )


def _validate_model_policy_graphs(services: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject ambiguous or invalid service-local logical-model references."""
    for service_id, service in services.items():
        config = service.get("config") if isinstance(service, Mapping) else None
        raw_models = config.get("models") if isinstance(config, Mapping) else None
        if not isinstance(raw_models, list):
            continue

        records: dict[str, Mapping[str, Any]] = {}
        for index, record in enumerate(raw_models):
            if not isinstance(record, Mapping):
                raise ValueError(f"service {service_id!r} models[{index}] must be an object")
            model_id = str(record.get("id") or "").strip()
            if not model_id:
                raise ValueError(f"service {service_id!r} models[{index}] requires a stable id")
            if model_id in records:
                raise ValueError(f"service {service_id!r} has duplicate model id {model_id!r}")
            records[model_id] = record

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(model_id: str) -> None:
            if model_id in visited:
                return
            if model_id in visiting:
                raise ValueError(
                    f"service {service_id!r} model aliases contain a cycle at {model_id!r}"
                )
            visiting.add(model_id)
            record = records[model_id]
            base_model_id = record.get("base_model_id")
            if base_model_id not in (None, ""):
                if record.get("transforms") not in (None, []):
                    raise ValueError(
                        f"service {service_id!r} alias {model_id!r} must use policy_overlay, not transforms"
                    )
                base_id = str(base_model_id).strip()
                if base_id not in records:
                    raise ValueError(
                        f"service {service_id!r} model {model_id!r} references unknown "
                        f"base_model_id {base_id!r}"
                    )
                visit(base_id)
                if records[base_id].get("base_model_id") not in (None, ""):
                    raise ValueError(
                        f"service {service_id!r} model {model_id!r} must reference a "
                        f"base policy record, not alias {base_id!r}"
                    )
                overlay = record.get("policy_overlay")
                if overlay is not None and not isinstance(overlay, list):
                    raise ValueError(
                        f"service {service_id!r} model {model_id!r} policy_overlay must be a list"
                    )
            elif record.get("policy_overlay") not in (None, []):
                raise ValueError(
                    f"service {service_id!r} base model {model_id!r} cannot declare policy_overlay"
                )
            visiting.remove(model_id)
            visited.add(model_id)

        for model_id in records:
            visit(model_id)


def _public_aliases(service: Any) -> list[str]:
    """The public model names (lowercased) a service claims: each alias record's id and model group."""
    config = service.get("config") if isinstance(service, Mapping) else None
    records = config.get("models") if isinstance(config, Mapping) else None
    if not isinstance(records, list):
        return []
    names: list[str] = []
    for record in records:
        if not isinstance(record, Mapping) or record.get("base_model_id") in (None, ""):
            continue
        names.extend(str(public_name or "").strip().lower() for public_name in (record.get("id"), record.get("model_group")))
    return [name for name in names if name]


def _public_alias_conflicts(services: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """(alias, first service, other service) for each public alias two services both claim."""
    owners: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    for service_id, service in services.items():
        for alias in _public_aliases(service):
            owner = owners.get(alias)
            if owner is not None and owner != service_id:
                conflicts.append((alias, owner, service_id))
            owners[alias] = service_id
    return conflicts


def _claim_order(item: tuple[str, Any]) -> tuple[int, float, str]:
    """Which of two services stored here claimed a public alias first (decision D22).

    The creation time in the service's provenance (``created_at``: seconds, or an ISO 8601 text) comes
    first; a service with none comes after every service that has one; the id breaks every tie.
    """
    service_id, service = item
    provenance = service.get("provenance") if isinstance(service, Mapping) else None
    created = provenance.get("created_at") if isinstance(provenance, Mapping) else None
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            created = None
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return (1, 0.0, service_id)
    return (0, float(created), service_id)


def _later_alias_claimants(services: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """``{service id: why}`` for each service that claims a public alias an earlier-stored service already holds.

    Services are taken in claim order, and a service that is left out holds no alias, so a clash between it
    and a third service does not leave the third out.
    """
    held: dict[str, str] = {}
    later: dict[str, str] = {}
    for service_id, service in sorted(services.items(), key=_claim_order):
        clash = next(((alias, held[alias]) for alias in _public_aliases(service) if alias in held), None)
        if clash is not None:
            later[service_id] = (f"its public model alias {clash[0]!r} is also claimed by service {clash[1]!r}, "
                                 "which was stored first")
            continue
        held.update({alias: service_id for alias in _public_aliases(service)})
    return later


def _validate_public_alias_uniqueness(services: Mapping[str, Mapping[str, Any]]) -> None:
    """Make unscoped public-alias resolution deterministic across services."""
    for alias, owner, service_id in _public_alias_conflicts(services)[:1]:
        raise ValueError(
            f"public alias {alias!r} is ambiguous across services "
            f"{owner!r} and {service_id!r}"
        )




























# Runtime fields the registry promotes to the top of a service record but that
# the legacy execution/router paths also read off a flat backend dict. The rest
# of the flat backend shape comes verbatim from the record's ``config`` (which
# holds the full original backend declaration minus secrets), so the projection
# is lossless without enumerating every open-ended execution field
# (workflows_dir, start_command, model_dir, …).
_RUNTIME_PROMOTED_FIELDS = ("name", "enabled", "role", "capabilities", "priority")


# Where the runtime projection keeps a config's legacy ``residency`` string when a residency mapping replaces it.
LEGACY_RESIDENCY_KEY = "legacy_residency"


def runtime_backend_from_service(service: Mapping[str, Any]) -> dict[str, Any]:
    """Project ONE registry service record to the flat runtime backend dict.

    This is the single, one-directional read-only projection the router and
    legacy execution paths consume. It carries the service's ``config`` (the
    full original backend declaration) plus the few runtime fields the registry
    promotes to the top level. It deliberately omits admin/provenance fields
    (``source``, ``editable``, ``provenance``, the ``lifecycle`` wrapper): those
    are registry concerns, and admin/discovery surfaces read the service record
    directly rather than this projection.
    """
    config = service.get("config")
    out: dict[str, Any] = copy.deepcopy(dict(config)) if isinstance(config, Mapping) else {}
    for field in _RUNTIME_PROMOTED_FIELDS:
        value = service.get(field)
        if value not in (None, ""):
            out[field] = copy.deepcopy(value)
    # Legacy external-key placeholders are execution context too: readiness
    # must resolve the same inference key as the eventual request.
    if service.get("secret_refs"):
        out["secret_refs"] = copy.deepcopy(service["secret_refs"])
    if isinstance(service.get("offerings"), list):
        out["offerings"] = copy.deepcopy(service["offerings"])
    # A schema-2 record keeps residency at the top level. Request paths read it
    # from this projection (load on demand, the residency domain and its
    # transition gate), so carry a declared one.
    if isinstance(service.get("residency"), Mapping) and service["residency"]:
        # A record may also carry the legacy ``residency: on_demand`` string in its config. The mapping takes the
        # key, so keep the string beside it, where the presenter's ``starts_on_demand`` reads it.
        if isinstance(out.get("residency"), str):
            out[LEGACY_RESIDENCY_KEY] = out["residency"]
        out["residency"] = copy.deepcopy(dict(service["residency"]))
    # ``managed`` and the systemd service name live under ``lifecycle`` in the
    # record but flat on the backend dict; promote them for managed backends.
    process = service.get("process")
    lifecycle = (process.get("legacy_lifecycle") if isinstance(process, Mapping) else None) or service.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        if lifecycle.get("managed"):
            out["managed"] = True
        if lifecycle.get("service_name"):
            out["service_name"] = str(lifecycle["service_name"])
        # Carry the lifecycle WRAPPER (driver + driver_config) onto the flat
        # backend dict so driver_for_service() can resolve the operator-declared
        # LifecycleDriver from config["backends"][id]["lifecycle"]. Engine-
        # agnostic: a service declares its driver as DATA; we only project it.
        out["lifecycle"] = copy.deepcopy(dict(lifecycle))
    return {key: value for key, value in out.items() if value not in (None, "")}


def _agreed_lifecycle(service_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Use the old normalized runtime block; reject any divergent nested copy."""
    config = record.get("config")
    nested = config.get("lifecycle") if isinstance(config, Mapping) else None
    nested = dict(nested) if isinstance(nested, Mapping) else {}
    # This helper reconciles lifecycle structure, including already-projected
    # records whose credential references are configured metadata.
    top = normalize_service_record(service_id, record, schema_version=1, check_secrets=False)["lifecycle"]

    def check(dst: Mapping[str, Any], src: Mapping[str, Any], path: str) -> None:
        for key, value in src.items():
            if key in dst and isinstance(dst[key], Mapping) and isinstance(value, Mapping):
                check(dst[key], value, f"{path}.{key}")
            elif key not in dst or dst[key] != value:
                raise ValueError(
                    f"service {service_id!r} lifecycle {path}.{key} conflict between lifecycle and "
                    "config.lifecycle; remove the conflicting nested copy before retrying"
                )

    check(top, nested, "lifecycle")
    return top


def legacy_lifecycle_from_service(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read the one compatibility lifecycle source, including unmigrated rows."""
    process = record.get("process")
    if isinstance(process, Mapping) and isinstance(process.get("legacy_lifecycle"), Mapping):
        return process["legacy_lifecycle"]
    lifecycle = record.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        return lifecycle
    config = record.get("config")
    nested = config.get("lifecycle") if isinstance(config, Mapping) else None
    return nested if isinstance(nested, Mapping) else {}


def _is_argument_entry(arg: Any) -> bool:
    """A command argument that is a secret entry (stored) or its view (``{"id", "configured"}``): not text, but not unknown."""
    return isinstance(arg, Mapping) and (_refs.is_secret_entry(arg) or ("id" in arg and "configured" in arg))


def _v2_process(lifecycle: Mapping[str, Any]) -> dict[str, Any] | None:
    if not lifecycle:
        return None
    driver = str(lifecycle.get("driver") or "external")
    driver_config = lifecycle.get("driver_config") if isinstance(lifecycle.get("driver_config"), Mapping) else {}
    driver_map = {"none": "none", "external": "none", "http-load-unload": "none", "command": "command",
                  "systemd": "command", "docker": "command", "process": "process"}
    process: dict[str, Any] = {"driver": driver_map.get(driver, "none")}
    if driver in {"docker", "http-load-unload"} or driver not in driver_map:
        process["legacy_driver"] = driver
        process["needs_owner_conversion"] = True
    for key in ("start", "stop", "status", "env", "cwd"):
        if key in driver_config:
            value = driver_config[key]
            if key in {"start", "stop", "status"} and (
                not isinstance(value, list) or not all(isinstance(arg, str) or _is_argument_entry(arg) for arg in value)
            ):
                process["needs_owner_conversion"] = True
            else:
                process[key] = copy.deepcopy(value)
    if driver == "systemd":
        unit = driver_config.get("unit")
        scope = ["--user"] if driver_config.get("scope") == "user" else []
        if unit:
            process.setdefault("start", ["systemctl", *scope, "start", unit])
            process.setdefault("stop", ["systemctl", *scope, "stop", unit])
            process.setdefault("status", ["systemctl", *scope, "is-active", unit])
        if driver_config.get("dropin"):
            process["profile_apply"] = {
                "kind": "write_env_file_then_restart", "file": driver_config["dropin"],
                "reload": ["systemctl", *scope, "daemon-reload"], "restore": "on_failure",
            }
    # The existing drivers still consume this one compatibility payload. It is
    # the only persisted lifecycle source after migration; there is no second
    # config.lifecycle to silently override an owner's edit.
    process["legacy_lifecycle"] = copy.deepcopy(dict(lifecycle))
    return process


def service_record_v2(record: Mapping[str, Any], *, public: bool = True) -> dict[str, Any]:
    """Return the additive v2.1 view of a legacy or migrated service record.

    ``public`` hides the credential a scalar may hold (an address with a user name or a query token, a
    probe path with a query token); migration reads the stored values with ``public=False``.
    """
    if public:
        record = projection.project("ServiceRecord", record).tree or {}
    service_id = str(record.get("id") or "")
    config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
    system = is_litellm_service_record(service_id, record)
    address = str(config.get("address") or service_base_url(config) or "")
    origin = str(config.get("credential_origin") or origin_of(address) or "")
    inference = None
    if config.get("api_key_secret_id") or config.get("api_key_env"):
        inference = {"secret_ref": "configured" if config.get("api_key_secret_id") else None,
                     "env": config.get("api_key_env") or None, "origin": origin,
                     "header": "Authorization", "scheme": "Bearer"}
    models = config.get("models") if isinstance(config.get("models"), list) else []
    offerings = []
    for model in models:
        if not isinstance(model, Mapping):
            continue
        offering_id = str(model.get("id") or model.get("upstream_model") or "")
        if not offering_id:
            continue
        capabilities = normalize_string_list(model.get("capabilities") or record.get("capabilities"))
        offerings.append({"id": offering_id, "kind": "model", "upstream": model.get("upstream_model") or None,
                          "selection": {"mode": "request_field", "implicit_load": "unknown"},
                          "tasks": [{"task": cap, "evidence": "declared"} for cap in capabilities],
                          "inputs": {}, "options": {}, "start_profile": {},
                          "adjustments": copy.deepcopy(config.get("transforms") or [])})
    if not offerings and not system:
        offerings.append({"id": service_id, "kind": "model", "upstream": config.get("default_model") or None,
                          "selection": {"mode": "request_field", "implicit_load": "unknown"},
                          "tasks": [{"task": cap, "evidence": "declared"} for cap in normalize_string_list(record.get("capabilities"))],
                          "inputs": {}, "options": {}, "start_profile": {},
                          "adjustments": copy.deepcopy(config.get("transforms") or [])})
    # Structured declarations already describe switch selection and workflow
    # inputs. Legacy models supply defaults only when no declaration exists.
    declared = record.get("offerings")
    if not isinstance(declared, list):
        declared = config.get("offerings")
    if isinstance(declared, list):
        offerings = copy.deepcopy(declared)
    lifecycle = _agreed_lifecycle(service_id, record) if "process" not in record else {}
    process = record.get("process") if isinstance(record.get("process"), Mapping) else _v2_process(lifecycle)
    if process is not None and public:
        # The projected block narrowed to an allow-list of fields. It is never a copy of what the gateway runs.
        process = projection.public_process(process, projected=True)  # the record above is already the projection
    observations = copy.deepcopy(record.get("observations")) if isinstance(record.get("observations"), Mapping) else {}
    if not observations and isinstance(lifecycle.get("driver_config"), Mapping):
        endpoint = lifecycle["driver_config"].get("health_endpoint")
        if endpoint:
            probe: dict[str, Any] = {"path": endpoint, "expect": 200}
            held = lifecycle["driver_config"].get("health_endpoint_entries")
            if not public and held:
                # The stored credentials of the probe path go with it; a public view shows none.
                probe["path_entries"] = copy.deepcopy(held)
            observations["process_ready"] = {"http": probe}
    residency = copy.deepcopy(record.get("residency")) if isinstance(record.get("residency"), Mapping) else {}
    if lifecycle.get("driver") == "http-load-unload":
        residency.setdefault("legacy_driver", "http-load-unload")
        residency.setdefault("needs_owner_conversion", True)
    raw_resources = record.get("resources") if isinstance(record.get("resources"), Mapping) else config.get("resources")
    resources = copy.deepcopy(dict(raw_resources)) if isinstance(raw_resources, Mapping) else {}
    return {"schema_version": ENGINE_RECORD_VIEW_VERSION, "id": service_id, "name": record.get("name") or service_id,
            "enabled": bool(record.get("enabled", True)), "record_type": "system" if system else "engine",
            "derived": system, "editable": bool(record.get("editable", not system)) and not system,
            "provenance": {"source": record.get("source") or "ui", "package": (record.get("provenance") or {}).get("package_id")},
            "connection": {"transport": "http", "address": address,
                           "protocol": config.get("protocol") or None,
                           "credentials": {"inference": inference, "control": None},
                           "headers": [{"name": row.get("name"), "value": "configured"}
                                       for row in (config.get("headers") or []) if isinstance(row, Mapping)],
                           "timeouts": {"total_s": config.get("timeout") or 600}},
            "offerings": offerings, "process": process, "observations": observations,
            "residency": residency, "resources": resources}


def _declares_process(block: Any) -> bool:
    """True when a process block says more than "Azizam does not run this"."""
    if not isinstance(block, Mapping):
        return False
    return str(block.get("driver") or "none").strip().lower() != "none" or any(
        key not in {"driver", "legacy_lifecycle"} for key in block
    )


def _declares_lifecycle(lifecycle: Mapping[str, Any]) -> bool:
    """True when a normalized legacy lifecycle runs, or names, something on the host."""
    driver = str(lifecycle.get("driver") or "external").strip().lower()
    return driver not in {"external", "none"} or bool(
        lifecycle.get("driver_config") or lifecycle.get("managed") or lifecycle.get("service_name")
    )


def _native_process(service_id: str, record: dict[str, Any], lifecycle: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record's own process block when it holds one, or ``None`` when the legacy lifecycle is the source.

    Two sources that both run something are refused: Azizam does not guess which one the owner meant.
    """
    config = record.get("config") if isinstance(record.get("config"), dict) else {}
    top, nested = record.get("process"), config.get("process")
    block = top if isinstance(top, Mapping) else nested
    if isinstance(top, Mapping) and isinstance(nested, Mapping) and top != nested and (
            _declares_process(top) or _declares_process(nested)):
        raise ValueError(
            f"service {service_id!r} has both a process block and a config.process block, and they differ. "
            "Azizam cannot tell which one you mean. Keep one: delete `process` or `config.process` "
            "from the service, then save again."
        )
    if not _declares_process(block):
        return None
    if "legacy_lifecycle" not in block and _declares_lifecycle(lifecycle):
        where = "process" if isinstance(top, Mapping) else "config.process"
        raise ValueError(
            f"service {service_id!r} has both a process block and a lifecycle block that run commands "
            f"(`{where}` and `lifecycle`), and Azizam cannot tell which one you mean. "
            f"Keep one: delete `{where}` or delete `lifecycle` (and `config.lifecycle`), then save again."
        )
    return copy.deepcopy(dict(block))


MIGRATION_REFUSED_KEY = "migration_refused"


def migrate_engine_records_v2(registry: dict[str, Any], *, refused: dict[str, str] | None = None) -> list[str]:
    """Collapse duplicate lifecycle sources atomically in a registry document.

    A record that holds two sources that both run something is refused. With no ``refused`` dict the
    refusal raises ``ValueError`` and the registry is untouched. With one, the migration records the
    message under the service id and leaves that one record as stored, marked with
    ``migration_refused``: the record does not validate until the owner keeps one block, so boot serves
    the other services and reports this one as left out.
    """
    if int(registry.get("schema_version") or 1) >= ENGINE_RECORD_VIEW_VERSION:
        return []
    services = registry.get("services") or {}
    migrated = copy.deepcopy(services)
    changed: list[str] = []
    for service_id, record in sorted(migrated.items()):
        if not isinstance(record, dict):
            continue
        try:
            lifecycle = _agreed_lifecycle(service_id, record)
            stored_view = service_record_v2(record, public=False)
            observations = stored_view["observations"]
            residency = stored_view["residency"]
            native = _native_process(service_id, record, lifecycle)
        except ValueError as error:
            if refused is None:
                raise
            refused[service_id] = str(error)
            record[MIGRATION_REFUSED_KEY] = str(error)
            continue
        if native is not None:
            # The block is the owner's: keep every field. A legacy lifecycle wrapper the runtime
            # projection carried today stays beside it, so the backend the gateway builds is unchanged.
            if isinstance(record.get("lifecycle"), Mapping):
                native.setdefault("legacy_lifecycle", copy.deepcopy(dict(lifecycle)))
            record["process"] = native
            if isinstance(record.get("config"), dict):
                record["config"].pop("process", None)
        else:
            process = _v2_process(lifecycle)
            if process is not None:
                record["process"] = process
        record["observations"] = observations
        record["residency"] = residency
        if isinstance(record.get("config"), dict):
            record["config"].pop("lifecycle", None)
        record.pop("lifecycle", None)
        changed.append(service_id)
    registry["services"] = migrated
    registry["schema_version"] = ENGINE_RECORD_VIEW_VERSION
    return changed


def runtime_backends(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project the whole registry to the flat ``{id: backend}`` runtime map."""
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if not isinstance(services, Mapping):
        return {}
    return {
        service_id: runtime_backend_from_service(service)
        for service_id, service in services.items()
        if isinstance(service, Mapping)
    }


def apply_service_registry_to_config(
    proxy_config: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a config copy whose ``backends`` are projected from the registry.

    The registry (admin.db) is the source of truth; this rebuilds the flat
    ``config["backends"]`` legacy execution paths route with as a one-way
    read-only projection. Nothing writes a backend shape back into the registry.
    """
    out = copy.deepcopy(dict(proxy_config))
    backends = runtime_backends(registry)
    # Every boot and reload publishes through here, so this is where the domain check runs on the flat
    # map the request paths read. A member that fails is left out of the running config (it fails closed).
    for service_id, error in runtime_transition_errors(backends).items():
        print(f"[service_registry] ERROR: service {service_id!r} is left out of the running config: {error}", flush=True)
        del backends[service_id]
    out["backends"] = backends
    return out


def builtin_frontend_ids(proxy_config: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The ids of the protocol frontends that ship with the code. A stored record may not take one."""
    seed = load_seed_catalogs(proxy_config or {}).get("protocol_frontends", {})
    return frozenset(
        str(frontend_id) for frontend_id, record in seed.items()
        if isinstance(record, Mapping) and record.get("source") == "builtin"
    )


class ServiceRegistryConflict(ValueError):
    """Raised when imported registry records collide with existing ownership."""


def merge_imported_records(
    registry: Mapping[str, Any],
    *,
    services: Mapping[str, Any] | None = None,
    protocol_frontends: Mapping[str, Any] | None = None,
    workflows: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge imported package records into a normalized registry.

    Imported packages must not silently replace operator or bundled records.
    Re-enabling the same imported package object is idempotent, which keeps
    validation/enable retries safe.
    """

    out = normalize_service_registry(registry)
    for raw_id, raw in (services or {}).items():
        if not isinstance(raw, Mapping):
            continue
        service_id = normalize_id(raw.get("id") or raw_id)
        if not service_id:
            continue
        validate_protocol_dispatch_local_only(raw)
        validate_record_connections(raw)
        record = normalize_service_record(service_id, raw)
        refuse_unoffered_kinds_in_record(service_id, record, before=out["services"].get(service_id))
        _merge_imported_record(out["services"], service_id, record, "service")

    for raw_id, raw in (protocol_frontends or {}).items():
        if not isinstance(raw, Mapping):
            continue
        frontend_id = normalize_id(raw.get("id") or raw_id)
        if not frontend_id:
            continue
        if frontend_id in builtin_frontend_ids():
            raise ServiceRegistryConflict(
                f"protocol_frontend {frontend_id!r} is a builtin frontend; a package cannot replace it"
            )
        record = normalize_protocol_frontend(frontend_id, raw)
        _merge_imported_record(
            out["protocol_frontends"],
            frontend_id,
            record,
            "protocol_frontend",
        )

    for raw_id, raw in (workflows or {}).items():
        if not isinstance(raw, Mapping):
            continue
        workflow_id = normalize_id(raw.get("id") or raw_id)
        if not workflow_id:
            continue
        record = normalize_workflow_record(workflow_id, raw)
        if record["id"] in bundled_workflow_manifests():
            raise ServiceRegistryConflict(
                f"workflow {record['id']!r} is a bundled workflow; a package cannot replace it"
            )
        _merge_imported_record(out["workflows"], record["id"], record, "workflow")

    for raw_id, raw in (capabilities or {}).items():
        if not isinstance(raw, Mapping):
            continue
        capability_id = normalize_id(raw.get("id") or raw_id)
        if not capability_id:
            continue
        record = normalize_capability_record(capability_id, raw)
        _merge_imported_record(
            out["capabilities"],
            record["id"],
            record,
            "capability",
        )

    out["updated_at"] = int(time.time())
    # Imported legacy records enter the current registry schema through the
    # same lifecycle collapse and graph checks as operator-authored records.
    return normalize_service_registry(out)


def _merge_imported_record(
    target: dict[str, dict[str, Any]],
    record_id: str,
    record: dict[str, Any],
    kind: str,
) -> None:
    existing = target.get(record_id)
    if existing is not None and not _same_imported_package_record(existing, record):
        if _kept_after_operator_edit(existing):
            raise ServiceRegistryConflict(
                f"{kind} {record_id!r} was kept when an earlier install was removed after an operator "
                "edited it; remove or rename it, then install the package again"
            )
        raise ServiceRegistryConflict(f"{kind} {record_id!r} already exists")
    target[record_id] = record


def _kept_after_operator_edit(existing: Mapping[str, Any]) -> bool:
    """A package record whose package detached it: imported, and no longer naming a package."""
    provenance = existing.get("provenance")
    return existing.get("source") == "imported" and isinstance(provenance, Mapping) and "package_id" not in provenance


def _same_imported_package_record(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    if existing.get("source") != "imported" or candidate.get("source") != "imported":
        return False
    existing_provenance = existing.get("provenance")
    candidate_provenance = candidate.get("provenance")
    if not isinstance(existing_provenance, Mapping) or not isinstance(
        candidate_provenance,
        Mapping,
    ):
        return False
    keys = ("package_id", "package_version", "package_hash")
    return all(
        existing_provenance.get(key)
        and existing_provenance.get(key) == candidate_provenance.get(key)
        for key in keys
    )


def _priority(service_id: str, value: Any) -> int:
    """A record's priority as a whole number (100 when unset). The refusal names the record and never the value."""
    try:
        return int(value or 100)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"service {service_id!r} has a priority that is not a whole number") from None


def validate_service_numbers(service_id: str, config: Mapping[str, Any]) -> None:
    """A port that is set must be a whole number from 1 to 65535 (text of digits counts), and a priority a whole number.

    The port becomes part of an address the gateway dials and shows, so a value of another kind (a word, a secret
    typed in the wrong slot) is refused here, with text that does not repeat it.
    """
    _priority(service_id, config.get("priority"))
    port = config.get("port")
    if port is None or port == "":
        return
    if isinstance(port, str) and port.strip().isascii() and port.strip().isdigit():
        port = int(port.strip())
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"service {service_id!r} has a port that is not a whole number from 1 to 65535")


def service_from_backend_config(
    service_id: str,
    raw: Mapping[str, Any],
    *,
    source: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_config, secret_refs = split_secret_refs(raw)
    validate_service_numbers(service_id, safe_config)
    validate_provider_secret_refs(service_id, safe_config, secret_refs)
    validate_provider_native_passthrough_disabled(service_id, safe_config)
    if is_remote_provider_config(safe_config):
        safe_config["provider_id"] = canonical_provider_id(
            safe_config.get("provider_id") or service_id
        )
    source = source if source in SERVICE_SOURCES else "ui"
    name = str(raw.get("name") or service_id)
    capabilities = normalize_string_list(raw.get("capabilities"))
    # `lifecycle` comes in two shapes:
    #   - nested dict (proxy_config.yml form): {type, managed, service_name,
    #     driver, driver_config} — the operator-authored lifecycle block. This is
    #     what carries the driver + driver_config that drives swaps.
    #   - scalar string (legacy flat form): the lifecycle *type*, with managed /
    #     service_name as sibling keys on `raw`.
    # Handle both so first-boot seeding from proxy_config.yml preserves the
    # driver config (without this, a nested lifecycle was str()'d into `type`,
    # destroying driver/driver_config and silently degrading swaps to external).
    raw_lifecycle = raw.get("lifecycle")
    if isinstance(raw_lifecycle, Mapping):
        lifecycle_block = {
            "type": str(raw_lifecycle.get("type") or "external"),
            "managed": bool_from_config(
                raw_lifecycle.get("managed", raw.get("managed")), False
            ),
            "service_name": str(
                raw_lifecycle.get("service_name") or raw.get("service_name") or ""
            ),
            **({"driver": str(raw_lifecycle["driver"])} if raw_lifecycle.get("driver") else {}),
            **({"driver_config": copy.deepcopy(dict(raw_lifecycle["driver_config"]))}
               if isinstance(raw_lifecycle.get("driver_config"), Mapping) else {}),
        }
    else:
        lifecycle_block = {
            "type": str(raw_lifecycle or raw.get("lifecycle_type") or "external"),
            "managed": bool_from_config(raw.get("managed"), False),
            "service_name": str(raw.get("service_name") or ""),
        }
    record = {
        "id": service_id,
        "name": name,
        "kind": str(raw.get("kind") or "provider"),
        "source": source,
        "enabled": bool_from_config(raw.get("enabled"), True),
        "priority": _priority(service_id, raw.get("priority")),
        "role": str(raw.get("role") or "required"),
        "capabilities": capabilities,
        "config": safe_config,
        "secret_refs": secret_refs,
        "lifecycle": lifecycle_block,
        "provenance": dict(provenance or {}),
    }
    # EDITABILITY LAW: editable is true for all records except managed-substrate
    # rows (LiteLLM). The stored editable:false from old seeds is ignored here.
    record["editable"] = not is_litellm_service_record(service_id, record)
    return record


_PROTOCOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")
# Where a record of kind ``engine`` can say which address it is at.
_ADDRESS_KEYS = ("host", "url", "address", "base_url", "api_base", "default_base_url", "base_url_env")


def check_service_shape(service_id: str, raw: Mapping[str, Any]) -> None:
    """Refuse a stored service whose blocks have the wrong shape, rather than read them as empty and rewrite them on the next save.

    The protocol name is checked for its form only: which names an engine can speak is data (protocol
    descriptions, packages), and the gateway keeps no list of them in code.
    """
    where = f"service {service_id!r}"
    for field in ("config", "lifecycle", "secret_refs", "provenance"):
        if raw.get(field) is not None and not isinstance(raw[field], Mapping):
            raise ValueError(f"{where}: {field} must be an object")
    config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
    if config.get("models") is not None and not isinstance(config["models"], (list, Mapping)):
        raise ValueError(f"{where}: models must be a list")
    protocol = config.get("protocol")
    if protocol is not None and not (isinstance(protocol, str) and _PROTOCOL_NAME.fullmatch(protocol)):
        raise ValueError(f"{where}: protocol must be a plain name such as 'openai-compatible'")
    if str(raw.get("kind") or "") == "engine" and not any(str(config.get(key) or "").strip() for key in _ADDRESS_KEYS):
        connections = (raw.get("connection"), config.get("connection"))
        if any(isinstance(block, Mapping) and str(block.get("address") or "").strip() for block in connections):
            # Routing reads the address from the config; connection.address alone does not route yet.
            raise ValueError(f"{where}: an engine needs config.address (or a host); connection.address alone is "
                             "not enough")
        raise ValueError(f"{where}: an engine needs a host or an address")
    # Routing dials the address the config resolves to (engine_connection.inference_base_url), never connection.address.
    # A connection address that names another host would be edited and shown while requests went to the old one, so the
    # two must name one endpoint: the same scheme, host and port. The path may differ, because the engine form stores the
    # address and dials it with the record's own path added. An address routing cannot resolve here (one read from the
    # environment) is not compared. The message names the field, never either address.
    dialed = origin_of(service_base_url(config) or str(config.get("address") or ""))
    if dialed:
        for block in (raw.get("connection"), config.get("connection")):
            address = str(block.get("address") or "").strip() if isinstance(block, Mapping) else ""
            if address and origin_of(address) not in ("", dialed):
                raise ValueError(f"{where}: connection.address names another host than the one this service is "
                                 "dialed at (config.base_url, config.address or host and port); give one address")


def _migration_refusal_text(service_id: str, raw: Mapping[str, Any]) -> str:
    """Why a record carrying ``migration_refused`` does not validate, said again from its blocks.

    The stored field is text a hand edit may have changed, so the error is never taken from it: the check
    the migration ran is run again, and its message names the service and the two blocks and nothing else.
    """
    record = {key: copy.deepcopy(value) for key, value in raw.items() if key != MIGRATION_REFUSED_KEY}
    try:
        _native_process(service_id, record, _agreed_lifecycle(service_id, record))
        why = "Its two blocks no longer conflict."
    except ValueError as error:
        why = str(error)
    return (f"service {service_id!r} was left out by the schema 2 migration. {why} "
            f"After you keep one block, delete the `{MIGRATION_REFUSED_KEY}` field from the service.")


# Every field a normalized service record can hold. ``gateway/projection_types.verify`` fails when one is here
# and not classified there, so a new field cannot be added without deciding what it is.
SERVICE_RECORD_FIELDS = (
    "id", "name", "kind", "source", "enabled", "priority", "role", "capabilities", "config", "secret_refs",
    "lifecycle", "provenance", "process", "observations", "residency", "resources", "editable", "offerings",
)


def _fold_top_level_connection(service_id: str, raw: Mapping[str, Any], config: dict[str, Any]) -> None:
    """Store a top-level ``connection`` block (the engine contract's shape) as ``config.connection``.

    The stored record keeps one connection block, in its config. A record that gives both, and they differ, is
    refused rather than one of them being dropped.
    """
    top = raw.get("connection")
    if top is None:
        return
    if not isinstance(top, Mapping):
        raise ValueError(f"service {service_id!r}: connection must be a mapping")
    nested = config.get("connection")
    if nested is not None and not isinstance(nested, Mapping):
        raise ValueError(f"service {service_id!r}: config.connection must be a mapping")
    if nested is not None and nested != top:
        raise ValueError(f"service {service_id!r}: connection is given at the top level and in config, and they "
                         "differ; give one")
    config["connection"] = copy.deepcopy(dict(top))


def normalize_service_record(
    service_id: str, raw: Mapping[str, Any], *, schema_version: int | None = None, check_secrets: bool = True,
) -> dict[str, Any]:
    """Return the stored form of ``raw``. ``check_secrets=False`` compares rows without refusing legacy ones."""
    if raw.get(MIGRATION_REFUSED_KEY):
        raise ValueError(_migration_refusal_text(service_id, raw))
    check_service_shape(service_id, raw)
    if schema_version is None:
        schema_version = 2 if any(key in raw for key in ("process", "observations", "residency")) else 1
    source = str(raw.get("source") or "ui")
    if source not in SERVICE_SOURCES:
        source = "ui"
    config = raw.get("config")
    if not isinstance(config, Mapping):
        config = {}
    config = copy.deepcopy(dict(config))
    _fold_top_level_connection(service_id, raw, config)
    raw_models = config.get("models")
    if isinstance(raw_models, list):
        normalized_models: list[Any] = []
        for model in raw_models:
            if not isinstance(model, Mapping):
                normalized_models.append(copy.deepcopy(model))
                continue
            normalized_model = copy.deepcopy(dict(model))
            normalized_model.setdefault(
                "id",
                str(
                    normalized_model.get("model_group")
                    or normalized_model.get("upstream_model")
                    or ""
                ).strip(),
            )
            normalized_models.append(normalized_model)
        config["models"] = normalized_models
    capabilities = normalize_string_list(raw.get("capabilities") or config.get("capabilities"))
    process = raw.get("process")
    lifecycle = (process.get("legacy_lifecycle") if isinstance(process, Mapping) else None) or raw.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        lifecycle = {}
    secret_refs = raw.get("secret_refs")
    if not isinstance(secret_refs, Mapping):
        secret_refs = {}
    validate_service_numbers(service_id, config)
    if check_secrets:
        validate_provider_secret_refs(service_id, config, secret_refs)
        validate_provider_native_passthrough_disabled(service_id, config)
    if is_remote_provider_config(config):
        config["provider_id"] = canonical_provider_id(
            config.get("provider_id") or service_id
        )
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    record = {
        "id": service_id,
        "name": str(raw.get("name") or config.get("name") or service_id),
        "kind": str(raw.get("kind") or "provider"),
        "source": source,
        "enabled": bool_from_config(raw.get("enabled"), True),
        "priority": _priority(service_id, raw.get("priority")),
        "role": str(raw.get("role") or config.get("role") or "required"),
        "capabilities": capabilities,
        "config": config,
        "secret_refs": copy.deepcopy(dict(secret_refs)),
        "lifecycle": {
            "type": str(lifecycle.get("type") or "external"),
            "managed": bool_from_config(lifecycle.get("managed"), False),
            "service_name": str(lifecycle.get("service_name") or ""),
            # The operator-declared LifecycleDriver + its config are DATA: a
            # service chooses how its lifecycle is driven (external | systemd |
            # docker | http-load-unload | command) and supplies the driver's
            # config. Preserve them through normalization (engine-agnostic).
            **({"driver": str(lifecycle["driver"])} if lifecycle.get("driver") else {}),
            **({"driver_config": copy.deepcopy(dict(lifecycle["driver_config"]))}
               if isinstance(lifecycle.get("driver_config"), Mapping) else {}),
        },
        "provenance": copy.deepcopy(dict(provenance)),
    }
    for key in ("process", "observations", "residency", "resources", "offerings"):
        if key in raw:
            record[key] = copy.deepcopy(raw[key])
    if schema_version >= ENGINE_RECORD_VIEW_VERSION:
        record.pop("lifecycle", None)
        record["config"].pop("lifecycle", None)
    # EDITABILITY LAW: editable is true for all records except managed-substrate
    # rows (LiteLLM). Stored editable:false from old seeds is normalized away.
    record["editable"] = not is_litellm_service_record(service_id, record)
    return record


def validate_protocol_dispatch_registry(
    registry: Mapping[str, Any], *, before: Mapping[str, Any] | None = None,
) -> None:
    """Check mutations without blocking repair of an older refused record.

    An unchanged stored record can remain, or be disabled with no other change.
    Every new record, other edit, and re-enable must satisfy the current policy.
    Compare raw records so normalization cannot hide a newly supplied paid flag.
    """
    previous = (before or {}).get("services") or {}
    for service_id, service in (registry.get("services") or {}).items():
        if not isinstance(service, Mapping):
            continue
        old = previous.get(service_id)
        if isinstance(old, Mapping):
            # Compare what would be stored. A client may echo a stored row with defaults the
            # normalizer adds (enabled, editable, provenance); that echo is not a change.
            if _same_stored_row(service_id, service, old):
                continue
            if bool_from_config(old.get("enabled"), True) and _same_stored_row(
                    service_id, service, dict(old, enabled=False)):
                continue
        try:
            validate_protocol_dispatch_local_only(service)
            validate_record_connections(service)
        except ValueError as exc:
            raise ValueError(f"service {service_id!r}: {exc}") from exc


# Top-level keys the normalizer stores (or defaults). Any other top-level key is policy-relevant
# raw data that normalization drops, so a change to it is a change, never a normalized no-op.
_NORMALIZED_ROW_KEYS = frozenset({
    "id", "name", "kind", "source", "enabled", "priority", "role", "capabilities", "config", "secret_refs",
    "lifecycle", "provenance", "process", "observations", "residency", "resources", "editable",
})


def _same_stored_row(service_id: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Whether two rows store the same thing. Secret checks are skipped: a refused legacy row must still compare."""
    if any(left.get(key) != right.get(key) for key in (set(left) | set(right)) - _NORMALIZED_ROW_KEYS):
        return False
    try:
        return (normalize_service_record(service_id, left, check_secrets=False)
                == normalize_service_record(service_id, right, check_secrets=False))
    except (TypeError, ValueError):
        return dict(left) == dict(right)


def _paid_remote_or_substrate(source: Mapping[str, Any]) -> bool:
    """True when one level of a record (record, config, connection or offering) names a paid, remote or substrate target."""
    for key in ("provider_type", "cost_class"):
        value = str(source.get(key) or "").strip().lower()
        if value and value not in LOCAL_PROVIDER_CLASSES:
            return True
    if any(bool_from_config(source.get(key), False) for key in ("paid_remote", "remote_provider")):
        return True
    if any(str(source.get(key) or "").strip().lower() == "remote" for key in ("locality", "placement")):
        return True
    if str(source.get("execution_substrate") or "").strip().lower() == "litellm":
        return True
    return str(source.get("kind") or "").strip().lower() in {"litellm", "llm-substrate"}


def _unsafe_address(address: str) -> bool:
    """Userinfo, backslashes and control or space characters make parsers disagree about the host."""
    return any(char in address for char in "@\\") or any(char.isspace() or ord(char) < 32 for char in address)


def validate_record_connections(service: Mapping[str, Any]) -> None:
    """Check every ``connection`` block of a record: the top-level one and the one in its config.

    The top-level block is the one the engine contract reads first, so it gets the same check as the config block.
    """
    config = service.get("config")
    blocks = [service.get("connection"), config.get("connection") if isinstance(config, Mapping) else None]
    for block in blocks:
        native_lane.validate_connection(block)


def validate_protocol_dispatch_local_only(service: Mapping[str, Any]) -> None:
    """Protocol dispatch lacks paid-provider controls; accept only proven local addresses.

    Every level of the record is checked: the record, its config, each connection and each
    offering. Any paid, remote or substrate marker refuses it, and so does any provider type
    or cost class outside LOCAL_PROVIDER_CLASSES. The connection's address must classify as
    loopback, with no userinfo. This intentionally narrow rule does not resolve DNS: LAN and
    container names remain unknown. Placement labels cannot override the connection's address.
    Dispatch calls the same check at request time.
    """
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    connections = [source for source in (service.get("connection"), config.get("connection")) if isinstance(source, Mapping)]
    opted = [connection for connection in connections if connection.get("dispatch") == "protocol"]
    if not opted:
        return
    reason = ("protocol dispatch does not enforce paid-provider access or spend limits; "
              "use the existing provider route")
    offerings = service.get("offerings") if isinstance(service.get("offerings"), list) else config.get("offerings")
    levels = [service, config, *connections, *(item for item in offerings or [] if isinstance(item, Mapping))]
    if any(_paid_remote_or_substrate(level) for level in levels):
        raise ValueError(reason)
    if is_litellm_service_record(service.get("id"), service):
        raise ValueError(reason)
    # Import lazily: presentation also imports registry classifiers. Reuse its
    # address classifier with only the actual target, never placement metadata
    # or a second legacy endpoint that could mask a public connection address.
    from gateway.model_presentation import model_locality

    for connection in opted:
        address = connection.get("address")
        if (not isinstance(address, str) or _unsafe_address(address)
                or model_locality({"base_url": address}, None) != "local"):
            raise ValueError(reason)
        control = connection.get("control_address")
        if control is not None and (not isinstance(control, str) or _unsafe_address(control)
                                    or model_locality({"base_url": control}, None) != "local"):
            raise ValueError(reason)


def validate_provider_secret_refs(
    service_id: str,
    config: Mapping[str, Any],
    secret_refs: Mapping[str, Any],
) -> None:
    """Remote providers must reference env vars or encrypted secret-store ids."""

    if not is_remote_provider_config(config):
        return
    inline_keys: list[str] = []

    def inspect_secrets(value: Any, prefix: str = "") -> None:
        if isinstance(value, Mapping):
            if _refs.is_secret_entry(value):
                return  # a secret entry points at the encrypted store; it holds no inline secret
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = key.lower().replace("-", "_")
                path = f"{prefix}.{key}" if prefix else key
                secret_field = normalized in _INLINE_SECRET_FIELDS
                if secret_field:
                    inline_keys.append(path)
                elif is_secret_key(key) and is_secret_reference_key(key):
                    suffix_ok = key.lower().endswith("_env") or key.lower().endswith("_secret_id")
                    # Optional reference fields may be unset when the other
                    # custody mode supplies the required credential reference.
                    if suffix_ok and item in (None, ""):
                        continue
                    env_ok = key.lower().endswith("_env") and isinstance(item, str) and ENV_VAR_NAME_RE.fullmatch(item)
                    secret_id_ok = key.lower().endswith("_secret_id") and isinstance(item, str) and item.strip()
                    if not suffix_ok or not (env_ok or secret_id_ok):
                        inline_keys.append(path)
                inspect_secrets(item, path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                inspect_secrets(item, f"{prefix}[{index}]")

    inspect_secrets(config)
    inspect_secrets(secret_refs)
    if inline_keys:
        names = ", ".join(sorted(str(key) for key in inline_keys))
        raise ValueError(
            f"remote provider {service_id!r} must use env-var or encrypted secret-store references; "
            f"inline secret fields are not allowed: {names}"
        )
    has_env = config.get("api_key_env") not in (None, "")
    has_secret = config.get("api_key_secret_id") not in (None, "")
    if has_env and has_secret:
        raise ValueError(f"remote provider {service_id!r} must choose one credential reference")
    if not has_env and not has_secret:
        raise ValueError(
            f"remote provider {service_id!r} requires api_key_env or api_key_secret_id"
        )
    api_key_env = config.get("api_key_env")
    if has_env and (not isinstance(api_key_env, str) or not ENV_VAR_NAME_RE.fullmatch(api_key_env)):
        raise ValueError(
            f"remote provider {service_id!r} api_key_env must be an environment variable name"
        )
    api_key_secret_id = config.get("api_key_secret_id")
    if has_secret and (not isinstance(api_key_secret_id, str) or not api_key_secret_id.strip()):
        raise ValueError(
            f"remote provider {service_id!r} api_key_secret_id must be a non-empty secret id"
        )


def validate_provider_native_passthrough_disabled(
    service_id: str, config: Mapping[str, Any]
) -> None:
    if is_remote_provider_config(config) and bool_from_config(
        config.get("native_passthrough"), False
    ):
        raise ValueError(
            f"remote provider {service_id!r} cannot enable native_passthrough"
        )
    try:
        native_lane.validate_native_block(config.get("native"))
        native_lane.validate_connection(config.get("connection"))
    except ValueError as error:
        raise ValueError(f"service {service_id!r}: {error}") from error
    if bool_from_config(config.get("native_passthrough"), False):
        reason = engine_check.static_blocked_reason(native_host_url(config))
        if reason:
            raise ValueError(f"service {service_id!r}: native pass-through cannot reach its host. {reason}")


def native_host_url(config: Mapping[str, Any]) -> str:
    """Where the native lane sends a record's calls: its ``url``, else its base URL, else its host and port."""
    explicit = str(config.get("url") or "").strip()
    if explicit:
        return explicit
    host = str(config.get("host") or "").strip()
    if ":" in host and not host.startswith("[") and not str(config.get("base_url") or config.get("api_base") or "").strip():
        # An IPv6 address needs its brackets to be a URL host.
        config = {**config, "host": f"[{host}]"}
    return service_base_url(config)


def is_remote_provider_config(config: Mapping[str, Any]) -> bool:
    return (
        any(str(config.get(key) or "").strip().lower() in REMOTE_PROVIDER_TYPES for key in ("provider_type", "cost_class"))
        or bool_from_config(config.get("remote_provider"), False)
        or bool_from_config(config.get("paid_remote"), False)
    )


# -- where a record's credential goes ---------------------------------------
# A credential reference is a name the compiler turns into a secret
# (``os.environ/NAME``). Whoever can attach one, or move the host that holds
# one, decides where that secret is sent. These helpers find both so a caller
# can hold them back for the owner. They name no engine.

CREDENTIAL_REFERENCE_KEYS = ("api_key_env", "api_key_ref", "api_key_secret_id")
_ENV_REFERENCE_PREFIX = "os.environ/"
_ENDPOINT_KEYS = (
    "base_url", "api_base", "default_base_url", "host", "port", "scheme", "base_path",
)
LIFECYCLE_BASE_KEY = "gateway_lifecycle_base_url"
# The origin the keys of a lifecycle driver (its admin key and API key) are bound to. The driver sends them only
# to the address it dials when that address has this origin; only the owner records it.
DRIVER_CREDENTIAL_ORIGIN_KEY = "driver_credential_origin"
_DRIVER_KEY_FIELDS = ("admin_key", "api_key")
_DRIVER_KEY_ENV_FIELDS = ("admin_key_env", "api_key_env")


def service_base_url(config: Mapping[str, Any]) -> str:
    raw = str(
        config.get("base_url")
        or config.get("api_base")
        or config.get("default_base_url")
        or ""
    ).strip()
    if raw:
        return raw.rstrip("/")
    host = str(config.get("host") or "").strip()
    port = config.get("port")
    if not host or port is None:
        return ""
    scheme = str(config.get("scheme") or "http").strip() or "http"
    base_path = str(config.get("base_path") or "/v1").strip() or "/v1"
    return f"{scheme}://{host}:{port}/{base_path.strip('/')}".rstrip("/")


def origin_of(url: str) -> str:
    """Scheme, host and port of a URL, or ``""`` when it is not an HTTP(S) URL."""
    try:
        return credential_origin_from_url(url)
    except Exception:  # noqa: BLE001 - an unreadable URL has no origin
        return ""


_REFERENCE_KEY_SUFFIXES = ("_env", "_ref", "_secret_id")
_EXTERNAL_DRIVER = "external"


def _names_credential(key: str) -> bool:
    """Deny by default: a key that looks like it names a secret is treated as one.

    Matches ``credential_origin``, ``api_key`` and any key ending in ``_env``,
    ``_ref`` or ``_secret_id``, at any depth and in any block of the record.
    """
    lowered = key.strip().lower()
    return (
        lowered in ("credential_origin", DRIVER_CREDENTIAL_ORIGIN_KEY, "api_key")
        or lowered.endswith(_REFERENCE_KEY_SUFFIXES)
        or (is_secret_key(lowered) and is_secret_reference_key(lowered))
    )


def _is_inject_secret(path: str, key: str) -> bool:
    """An ``auth_inject`` rule's ``secret`` names a downstream secret, though no suffix says it is a reference."""
    return key.strip().lower() == "secret" and "auth_inject[" in path


def credential_references(record: Mapping[str, Any]) -> dict[str, str]:
    """Every field of a service record that names a credential, keyed by its path.

    The whole record is scanned (``config``, ``secret_refs``, the ``lifecycle``
    block and anything else), so a new block cannot become a place to hide a
    key reference.

    A secret entry (``secret_ref``) is a reference like any other here, so moving the host of a record that holds
    one is the owner's. A record holds no secret value itself (decision D26).
    """
    found: dict[str, str] = {}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                here = f"{path}.{key}" if path else str(key)
                if _names_credential(str(key)) or path == "secret_refs" or _is_inject_secret(path, str(key)):
                    found[here] = repr(item)
                else:
                    walk(item, here)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str) and value.strip().lower().startswith(_ENV_REFERENCE_PREFIX):
            found[path] = repr(value)

    walk(record, "")
    return found


def _driven_lifecycle_blocks(record: Mapping[str, Any]) -> frozenset[str]:
    """The record's lifecycle blocks that name a driver, as comparable text.

    Looks at the record's ``lifecycle`` block and at the copy the form path keeps
    in ``config``. Blocks that name no driver (or ``external``) are left out.
    """
    process = record.get("process")
    config = record.get("config")
    blocks = [record.get("lifecycle"), config.get("lifecycle") if isinstance(config, Mapping) else None]
    driven = {json.dumps(block, sort_keys=True, default=str) for block in blocks if _is_driven(block)}
    if isinstance(process, Mapping):
        legacy = process.get("legacy_lifecycle")
        controls_host = _is_driven(process) or _is_driven(legacy) or any(
            key in process for key in ("start", "stop", "status", "profile_apply", "env", "cwd")
        )
        if controls_host:
            driven.add(json.dumps(process, sort_keys=True, default=str))
    return frozenset(driven)


def _is_driven(block: Any) -> bool:
    """True when a lifecycle block names a driver other than ``external``."""
    if not isinstance(block, Mapping):
        return False
    for field in ("driver", "type"):
        name = str(block.get(field) or "").strip().lower()
        if name and name not in {_EXTERNAL_DRIVER, "none"}:
            return True
    return False


def driver_keys_held(record: Mapping[str, Any]) -> bool:
    """True when a lifecycle block of the record names a key its driver sends (an admin key or an API key).

    The key may be stored (an entry) or named by an environment variable (``admin_key_env``, ``api_key_env``).
    """
    config = record.get("config")
    process = record.get("process")
    blocks = [record.get("lifecycle"), config.get("lifecycle") if isinstance(config, Mapping) else None,
              process.get("legacy_lifecycle") if isinstance(process, Mapping) else None]
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        driver_config = block.get("driver_config")
        if any(block.get(field) for field in _DRIVER_KEY_FIELDS):
            return True
        if isinstance(driver_config, Mapping) and any(driver_config.get(field) for field in _DRIVER_KEY_ENV_FIELDS):
            return True
    return False


def lifecycle_driver_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether a lifecycle block that names a driver was created, changed or removed.

    A driver other than ``external`` runs commands on the host or sends a stored
    key to the record's host, so only the owner may set, change or remove one.
    A block that stays as it is, and an ``external`` block, are not changes.
    """
    return _driven_lifecycle_blocks(before) != _driven_lifecycle_blocks(after)


# Config fields whose value makes the gateway touch the host's files or processes.
# ``config_path`` is where the compiled LiteLLM config is written.
OWNER_ONLY_CONFIG_KEYS = ("config_path",)


def _reads_process_command_lines(config: Mapping[str, Any]) -> bool:
    """True when ``aux`` asks the gateway to read the command lines of host processes."""
    aux = config.get("aux")
    return isinstance(aux, Mapping) and aux.get("probe") == "process-cmdline"


def lifecycle_dial_url(config: Mapping[str, Any]) -> str:
    """Where a lifecycle driver of this record dials: its host and port, else the origin of its base URL.

    The driver's keys (an admin key, an API key) go to this address. It is one of the addresses the credential
    fingerprint covers, so a change to it is the owner's on a record that holds a key. Empty when the record names
    no address; the driver then has nowhere to send a key.
    """
    host = str(config.get("host") or "").strip()
    port = config.get("port")
    if host and port not in (None, ""):
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # an IPv6 address needs its brackets to be a URL host
        scheme = str(config.get("scheme") or "http").strip() or "http"
        return f"{scheme}://{host}:{port}"
    return origin_of(service_base_url(config))


def _dialed_origins(config: Mapping[str, Any]) -> frozenset[str]:
    """The origins of every address the gateway can dial for this record, however the record spells them.

    ``service_base_url`` returns the first of ``base_url``, ``api_base`` and ``default_base_url`` that is set, and the
    host and port apply only when none is, so comparing just its result would hide a change to the others. The
    lifecycle driver reads the host and port whatever the base URL says. A set, because naming an address a second
    time sends a key nowhere new.
    """
    addresses = [str(config.get(key) or "").strip() for key in ("base_url", "api_base", "default_base_url")]
    addresses.append(lifecycle_dial_url(config))
    return frozenset(origin_of(address) or address for address in addresses if address)


def _endpoint_fingerprint(config: Mapping[str, Any]) -> tuple[Any, ...]:
    """Every address a credential of this record can be sent to, and every origin it is bound to.

    The gateway dials the record's base URL (chat), its native URL (the native lane prefers ``url``),
    the address the form stores and the host and port a lifecycle driver dials; a native credential goes
    only to the origin written beside it. A change to any of these moves a key, so each has its own place
    in the fingerprint.
    """
    connection = config.get("connection") if isinstance(config.get("connection"), Mapping) else {}
    credentials = connection.get("credentials") if isinstance(connection.get("credentials"), Mapping) else {}
    bound = tuple(
        (str(name), str(item.get("origin") or ""))
        for name, item in sorted(credentials.items(), key=lambda pair: str(pair[0]))
        if isinstance(item, Mapping)
    )
    addresses = (
        service_base_url(config), native_host_url(config),
        str(config.get("address") or "").strip(), str(connection.get("address") or "").strip(),
        str(connection.get("control_address") or "").strip(),
    )
    return (
        tuple(origin_of(address) or address for address in addresses),
        tuple(sorted(_dialed_origins(config))),
        bound,
        str(config.get(LIFECYCLE_BASE_KEY) or ""),
    )


def credential_binding_changes(
    old_registry: Mapping[str, Any], new_registry: Mapping[str, Any]
) -> dict[str, list[str]]:
    """Services whose credential or credential host differs between two registries.

    A service is listed when it gains, changes or loses a credential reference
    (a lost one leaves its secret in the store with no record that owns it), when it
    sets the base of the gateway's own internal key, when it already carries a
    credential and its origin moves, when its lifecycle driver is set, changed
    or removed (a driver runs commands or sends a stored key), when its
    ``auth_inject`` rules change, when the origin a URL field with stored
    credentials is bound to changes (decision D39), or when it sets a field that
    makes the gateway write a file or read process command lines.
    The value says which.
    """
    old = (normalize_service_registry(old_registry).get("services")) or {}
    new = (normalize_service_registry(new_registry).get("services")) or {}
    changed: dict[str, list[str]] = {}
    for service_id, record in new.items():
        before = old.get(service_id) or {}
        refs, prior = credential_references(record), credential_references(before)
        reasons = [path for path, value in refs.items() if prior.get(path) != value]
        # Taking a reference away detaches the secret it names and leaves it in the store with nothing to own it.
        reasons += [path for path in prior if path not in refs]
        config = record.get("config") or {}
        before_config = before.get("config") or {}
        if config.get(LIFECYCLE_BASE_KEY) and config.get(LIFECYCLE_BASE_KEY) != before_config.get(LIFECYCLE_BASE_KEY):
            reasons.append(f"config.{LIFECYCLE_BASE_KEY}")
        if prior and _endpoint_fingerprint(config) != _endpoint_fingerprint(before_config):
            reasons.append("host")
        # Credentials stored with a URL go where that URL goes: a change to the address of such a field (or to the base
        # a path joins) moves them (decision D39).
        marks_before, marks_after = dict(url_binding.binding_marks(before)), dict(url_binding.binding_marks(record))
        reasons += [path for path, mark in marks_before.items() if path in marks_after and marks_after[path] != mark]
        if lifecycle_driver_changed(before, record):
            reasons.append("lifecycle")
        if (config.get("auth_inject") or None) != (before_config.get("auth_inject") or None):
            # The rules name the keys the gateway sends to the host, and are never edited through the admin surface.
            reasons.append("config.auth_inject")
        for key in OWNER_ONLY_CONFIG_KEYS:
            if config.get(key) and config.get(key) != before_config.get(key):
                reasons.append(f"config.{key}")
        if _reads_process_command_lines(config) and config.get("aux") != before_config.get("aux"):
            reasons.append("config.aux")
        if reasons:
            changed[service_id] = reasons
    return changed


def bind_credential_origins(
    registry: Mapping[str, Any], service_ids: Iterable[str]
) -> dict[str, Any]:
    """Record, for each named service, the origin its credential was attached for.

    Only the owner reaches this: it states "this key goes to this host". The
    compiler sends a local service's key only to the origin recorded here.
    """
    out = normalize_service_registry(registry)
    for service_id in service_ids:
        record = out["services"].get(service_id)
        if record is None:
            continue
        config = record["config"]
        if is_remote_provider_config(config):
            continue
        if any(config.get(key) for key in CREDENTIAL_REFERENCE_KEYS):
            origin = origin_of(str(config.get(LIFECYCLE_BASE_KEY) or "") or service_base_url(config))
            if origin:
                config["credential_origin"] = origin
        if driver_keys_held(record):
            # The driver's keys go to the address it dials, which may not be the one the base URL names.
            driver_origin = origin_of(lifecycle_dial_url(config))
            if driver_origin:
                config[DRIVER_CREDENTIAL_ORIGIN_KEY] = driver_origin
        # Each per-class native credential is bound to the host the native lane sends to.
        connection = config.get("connection")
        credentials = connection.get("credentials") if isinstance(connection, Mapping) else None
        native_origin = origin_of(native_host_url(config))
        for credential in credentials.values() if isinstance(credentials, Mapping) else ():
            if native_origin and isinstance(credential, dict) and (credential.get("secret_env") or credential.get("secret_ref")):
                credential["origin"] = native_origin
    return out


def is_litellm_service_record(raw_id: Any, service: Mapping[str, Any]) -> bool:
    """Return whether a service record declares the LiteLLM execution substrate."""
    service_id = str(service.get("id") or raw_id or "").strip().lower()
    kind = str(service.get("kind") or "").strip().lower()
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    execution_substrate = str(config.get("execution_substrate") or "").strip().lower()
    return (
        service_id == "litellm"
        or kind in {"litellm", "llm-substrate"}
        or execution_substrate == "litellm"
    )


# ---------------------------------------------------------------------------
# Single-service CRUD on a normalized registry document.
#
# These are pure: they take a registry dict, apply one operation, and return a
# new normalized registry (plus, for upsert, the affected record). The store
# layer (gateway/admin_runtime.AdminStore) persists the result.
#
# EDITABILITY LAW: all service rows are editable and removable regardless of
# ``source`` (source is provenance/history, not permission). The DB registry
# is the source of truth after the one-time config seed; removed rows are NOT
# re-imported on reboot. The sole exception is the LiteLLM managed-substrate
# row — the gateway spawns that process, so endpoint/protocol fields are
# derived and locked; rename/enable/note are still allowed; removal is blocked.
# ---------------------------------------------------------------------------

# Per-protocol fields beyond the generic openai-compatible set.  Each entry
# lists fields that service_form_to_raw should accept and round-trip for that
# protocol.  Fields not listed here for a given protocol pass through the
# generic path unchanged.
_PROTOCOL_FIELDS: dict[str, tuple[str, ...]] = {
    "comfyui": ("workflows_dir", "video_workflows_dir", "default_model"),
    # openai-compatible uses the generic path; listed here for documentation.
    "openai-compatible": (),
}


def refuse_unoffered_kinds(
    models: Iterable[Any], record_capabilities: Iterable[str], *, path: str = "models",
) -> None:
    """A model's kind must be one its record's capabilities serve. Otherwise the catalog binds the model to nothing.

    A save that adds or changes a record's models or capabilities runs this, through
    ``refuse_unoffered_kinds_in_record``, from the engine page and the generic service API alike, so a refusal
    names the same row and field whichever form made the record. ``path`` is the list the rows sit in; a save of
    many records names the record in it, as ``services.<id>.config.models``.
    """
    offered = offered_model_kinds(record_capabilities)
    for index, row in enumerate(models):
        kind = row.get("kind") if isinstance(row, Mapping) else None
        if not kind:
            continue
        field = f"{path}.{index}.kind"
        if kind not in MODEL_KIND_LABELS:
            name = row.get("model_group") or row.get("upstream_model") or ""
            raise engine_check.DraftError(f"The capability of “{name}” is not one of the choices.", field=field)
        if kind not in offered:
            raise engine_check.DraftError(f"This engine doesn’t offer {MODEL_KIND_LABELS[kind]}.", field=field)


def refuse_unoffered_kinds_in_record(
    service_id: str, record: Mapping[str, Any], *, before: Mapping[str, Any] | None = None, path: str | None = None,
) -> None:
    """Refuse a record whose model kinds it does not offer. Every save that writes a record calls this.

    A new record (``before`` is None), or one whose models or capabilities differ from ``before``, the stored
    record it replaces, must offer each model's kind. A stored record that the save leaves with the same models
    and capabilities is not checked, so a record stored before the check loads and saves as it is. ``path`` is
    where a refusal points; by default it is the record's own ``services.<id>.config.models``.
    """
    if isinstance(before, Mapping) and _same_kind_inputs(service_id, record, before):
        return
    config = record.get("config")
    models = config.get("models") if isinstance(config, Mapping) else None
    refuse_unoffered_kinds(
        models if isinstance(models, list) else [], record.get("capabilities") or [],
        path=path or f"services.{service_id}.config.models",
    )


def refuse_unoffered_kinds_in_registry(
    registry: Mapping[str, Any], *, before: Mapping[str, Any] | None = None,
) -> None:
    """Check the model kinds of the records a whole-registry save adds or changes.

    Each record is checked by ``refuse_unoffered_kinds_in_record`` against its stored copy in ``before``, so a
    record the save leaves as it was is not checked. Both sides are compared as they would be stored, as
    ``validate_protocol_dispatch_registry`` does.
    """
    previous = (before or {}).get("services") or {}
    for service_id, service in (registry.get("services") or {}).items():
        if isinstance(service, Mapping):
            refuse_unoffered_kinds_in_record(service_id, service, before=previous.get(service_id))


def _same_kind_inputs(service_id: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Whether two rows offer the same models and capabilities: the only inputs the kind check reads."""
    try:
        return _kind_inputs(service_id, left) == _kind_inputs(service_id, right)
    except (TypeError, ValueError):
        return False


def _kind_inputs(service_id: str, record: Mapping[str, Any]) -> tuple[Any, frozenset[str]]:
    stored = normalize_service_record(service_id, record, check_secrets=False)
    return stored["config"].get("models"), frozenset(stored["capabilities"])


def upsert_service_in_registry(
    registry: Mapping[str, Any],
    *,
    service_id: str,
    raw: Mapping[str, Any],
    source: str = "ui",
    replace: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add or replace one service in a registry; returns (registry, record).

    ``replace=False`` (create) raises if the id already exists. ``replace=True``
    (edit) raises if the id is missing.

    EDITABILITY LAW: after the one-time config import the DB registry is the
    source of truth, so all rows are editable regardless of ``source``. The
    sole exception is managed-substrate rows (LiteLLM) — the gateway spawns
    that process, so endpoint/protocol fields are derived and locked; only
    name, enabled, and note may be changed.
    """
    validate_protocol_dispatch_local_only(raw)
    validate_record_connections(raw)
    out = normalize_service_registry(registry)
    sid = normalize_id(service_id)
    if not sid:
        raise ValueError("service id is required")
    existing = out["services"].get(sid)
    if replace:
        if existing is None:
            raise ValueError(f"service {sid!r} not found")
        if is_litellm_service_record(sid, existing):
            # Managed substrate: only allow metadata edits (name, enabled, note).
            # Endpoint and connection fields are derived by the gateway.
            # host/port/base_url define the actual connection; protocol and
            # execution_substrate control how the gateway talks to the process.
            _MANAGED_LOCKED_FIELDS = frozenset(
                {"host", "port", "base_url", "protocol", "execution_substrate"}
            )
            blocked = [k for k in raw if k in _MANAGED_LOCKED_FIELDS]
            if blocked:
                raise ValueError(
                    f"service {sid!r} is a managed substrate (LiteLLM); "
                    f"fields {sorted(blocked)} are derived by the gateway and "
                    "cannot be edited — only name, enabled, and note may be changed"
                )
    elif existing is not None:
        raise ValueError(f"service {sid!r} already exists")

    if replace and existing is not None and is_litellm_service_record(sid, existing):
        # For managed substrate: preserve the existing config; only patch allowed fields.
        updated = dict(existing)
        for field in ("name", "enabled"):
            if field in raw:
                updated[field] = raw[field]
        if "note" in raw:
            # ``config.note`` is the canonical persisted location.  Do not put
            # this managed metadata at the record top level: normalization
            # intentionally discards unknown top-level fields.
            updated["config"] = {
                **copy.deepcopy(dict(existing.get("config") or {})),
                "note": raw["note"],
            }
        record = normalize_service_record(sid, updated)
    else:
        # Preserve the original source for non-UI rows (editability law: source is
        # provenance/history, not permission; don't re-stamp it as "ui").
        effective_source = source
        if replace and existing is not None:
            existing_source = str(existing.get("source") or "ui")
            if existing_source in SERVICE_SOURCES:
                effective_source = existing_source
        record = service_from_backend_config(sid, raw, source=effective_source)

    if "retention_facts" in record["config"]:
        from gateway.retention_facts import validate_facts
        try:
            record["config"]["retention_facts"] = validate_facts(
                record["config"]["retention_facts"], for_write=True,
            )
        except ValueError as exc:
            raise ValueError(f"service {sid!r} config.retention_facts: {exc}") from None
    # A single-record save names its model rows as its form does: models.N.kind.
    refuse_unoffered_kinds_in_record(sid, record, before=existing, path="models")
    out["services"][sid] = record
    out["updated_at"] = int(time.time())
    out = normalize_service_registry(out)
    return out, out["services"][sid]


# Top-level record fields a service edit form has no widget for. A partial edit
# must carry them over unless the caller names them.
_LIFECYCLE_FORM_KEYS = {"lifecycle_type": "type", "managed": "managed", "service_name": "service_name"}


def carry_unnamed_record_fields(
    existing: Mapping[str, Any], updated: dict[str, Any], *, named: Mapping[str, Any]
) -> None:
    """Keep what a partial edit did not name: ``secret_refs``, ``lifecycle``, ``kind``, ``provenance``.

    The edit path rebuilds the record from flat form fields, and those blocks
    are not form fields. Without this, a note edit drops a key held in
    ``secret_refs`` and turns a managed lifecycle back into ``external``.
    ``updated`` changes in place. A field the caller names keeps the caller's value.
    """
    if isinstance(existing.get("secret_refs"), Mapping):
        updated["secret_refs"] = {**copy.deepcopy(dict(existing["secret_refs"])), **updated.get("secret_refs", {})}
    lifecycle_named = "lifecycle" in named or any(key in named for key in _LIFECYCLE_FORM_KEYS)
    for field in ("process", "observations", "residency", "resources", "offerings"):
        if lifecycle_named and field in ("process", "observations", "residency"):
            continue
        if field in existing and field not in named:
            updated[field] = copy.deepcopy(existing[field])
    if isinstance(existing.get("lifecycle"), Mapping) and "lifecycle" not in named:
        kept = copy.deepcopy(dict(existing["lifecycle"]))
        for form_key, block_key in _LIFECYCLE_FORM_KEYS.items():
            if form_key in named:
                kept[block_key] = updated["lifecycle"][block_key]
        updated["lifecycle"] = kept
    for field in ("kind", "provenance"):
        if field in existing and field not in named:
            updated[field] = copy.deepcopy(existing[field])


def remove_service_from_registry(
    registry: Mapping[str, Any], service_id: str
) -> dict[str, Any]:
    """Hard-remove a service from the registry.

    Raises for managed-substrate rows (e.g. LiteLLM) — the gateway spawns
    those processes, so they cannot be deleted; disable them instead. All
    other sources (config-imported, provisioned, builtin, discovered, ui)
    are removable: the DB registry is the source of truth after the one-time
    seed, and the YAML is never re-merged on subsequent boots.
    """
    out = normalize_service_registry(registry)
    sid = normalize_id(service_id)
    existing = out["services"].get(sid)
    if existing is None:
        raise ValueError(f"service {sid!r} not found")
    if is_litellm_service_record(sid, existing):
        raise ValueError(
            f"service {sid!r} is a managed substrate (LiteLLM); "
            "disable it instead of removing it — the gateway manages this process"
        )
    del out["services"][sid]
    out["updated_at"] = int(time.time())
    return out


def set_service_enabled(
    registry: Mapping[str, Any], service_id: str, enabled: bool
) -> dict[str, Any]:
    """Enable/disable any service (allowed regardless of source)."""
    out = normalize_service_registry(registry)
    sid = normalize_id(service_id)
    existing = out["services"].get(sid)
    if existing is None:
        raise ValueError(f"service {sid!r} not found")
    existing["enabled"] = bool(enabled)
    out["updated_at"] = int(time.time())
    return out


def split_base_url(value: Any) -> dict[str, Any]:
    """Parse a base URL into host/port/base_path (the form's single URL field).

    Accepts ``http://host:port/v1`` and bare ``host:port``. Returns only the
    keys it can determine, so a caller can merge with explicit fields.
    """
    from urllib.parse import urlparse

    text = str(value or "").strip()
    if not text:
        return {}
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    out: dict[str, Any] = {}
    if parsed.hostname:
        out["host"] = parsed.hostname
    if parsed.port:
        out["port"] = int(parsed.port)
    path = (parsed.path or "").rstrip("/")
    if path:
        out["base_path"] = path
    return out


# Form-level keys that are consumed/transformed by service_form_to_raw and
# must NOT be passed through as raw config verbatim.  Everything else in the
# form dict is treated as an unknown config key and passes through unchanged
# (honesty law H1: a partial PATCH must never silently drop a stored field).
_FORM_CONSUMED_KEYS: frozenset[str] = frozenset({
    # Parsed/derived — stored as host+port+base_path instead.
    "base_url",
    # Top-level record fields, not config fields.
    "name",
    "enabled",
    "role",
    "priority",
    "capabilities",
    "managed",
    # Explicitly handled below with type coercion.
    "host",
    "port",
    "base_path",
    "protocol",
    "health_endpoint",
    "api_key_env",
    "note",
    "service_name",
    "timeout",
    "resources",
    "native_passthrough",
    "models",
    # Protocol-specific fields handled explicitly below.
    "video_timeout",
})


def service_form_to_raw(form: Mapping[str, Any]) -> dict[str, Any]:
    """Map the SLVP add/edit form fields to a backend-config dict.

    The form's single ``base_url`` field is parsed into host/port/base_path,
    overridden by any explicit host/port/base_path. Secrets are referenced by
    env-var name only (``api_key_env``) — the plaintext key is never accepted
    or stored.

    Honesty law H1: keys not explicitly consumed by this function are passed
    through verbatim so that a partial PATCH via update_service never silently
    drops stored config keys the form has no widget for (e.g. comfyui protocol
    fields, or any future extension field).
    """
    if "preset" in form:
        # The preset list is gone (engine definitions replace it). Passed through,
        # the key would be stored and the record would silently not route.
        raise ValueError("engine presets are gone: add an engine with a definition and its values "
                         "(registry/engine_definitions.json), or send a raw record with its protocol")
    raw: dict[str, Any] = {}
    raw.update(split_base_url(form.get("base_url")))
    base_url = str(form.get("base_url") or "").strip().rstrip("/")
    if base_url and is_remote_provider_config(form):
        raw["base_url"] = base_url

    # Explicit fields win over URL-derived values.
    for key in ("name", "host", "base_path", "protocol", "health_endpoint",
                "api_key_env", "note", "service_name"):
        value = form.get(key)
        if value not in (None, ""):
            raw[key] = str(value).strip()
    if form.get("port") not in (None, ""):
        raw["port"] = int(form["port"])
    if form.get("timeout") not in (None, ""):
        raw["timeout"] = int(form["timeout"])
    if form.get("priority") not in (None, ""):
        raw["priority"] = int(form["priority"])

    # resources is a nested dict.
    resources = form.get("resources")
    if isinstance(resources, Mapping) and resources:
        raw["resources"] = dict(resources)

    caps = normalize_string_list(form.get("capabilities"))
    if caps:
        raw["capabilities"] = caps
    raw["role"] = str(form.get("role") or "required")
    raw["enabled"] = bool_from_config(form.get("enabled"), True)
    raw["managed"] = bool_from_config(form.get("managed"), False)
    if "native_passthrough" in form:
        raw["native_passthrough"] = bool_from_config(form.get("native_passthrough"), False)

    # The model/deployment mapping: what makes an OpenAI-compatible service
    # actually routable. The form sends `models` as a list of {public, upstream}
    # pairs (or {model_group/id, upstream_model}); store as the list shape
    # litellm_substrate._deployment_records consumes. A `*` upstream means
    # "expose everything this backend serves" (model-listing backends).
    models = _form_models_to_config(form.get("models"))
    if models:
        raw["models"] = models

    # Protocol-specific string fields (e.g. comfyui directory paths, default_model).
    # Each protocol declares its extra string fields in _PROTOCOL_FIELDS; we also
    # accept them unconditionally so an explicit key always wins regardless of
    # what protocol label the form sends.
    _all_protocol_str_fields: set[str] = set()
    for _fields in _PROTOCOL_FIELDS.values():
        _all_protocol_str_fields.update(_fields)
    for key in _all_protocol_str_fields:
        value = form.get(key)
        if value not in (None, ""):
            raw[key] = str(value).strip()

    # video_timeout is a protocol-specific integer field (comfyui).
    if form.get("video_timeout") not in (None, ""):
        raw["video_timeout"] = int(form["video_timeout"])

    # Honesty law H1: pass through any key not explicitly consumed above so
    # that stored config fields unknown to the current form schema are never
    # silently dropped during a partial PATCH.  _FORM_CONSUMED_KEYS lists every
    # key the code above already handles; everything else goes straight through.
    for key, value in form.items():
        if key not in _FORM_CONSUMED_KEYS and key not in raw and value not in (None, ""):
            raw[key] = value

    # base_path defaults to /v1 for an OpenAI-style backend when nothing set it.
    raw.setdefault("base_path", "/v1")
    return {key: value for key, value in raw.items() if value not in (None, "")}


def _form_models_to_config(value: Any) -> list[dict[str, Any]]:
    """Normalize the form's model rows into config.models deployment records.

    Accepts a list of mappings with public/upstream (or model_group/id +
    upstream_model) keys. Stable ids, base references, transforms, and ordered
    alias overlays survive the form path; rows missing both names are dropped.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        public = str(
            row.get("public") or row.get("model_group") or row.get("id") or ""
        ).strip()
        upstream = str(
            row.get("upstream") or row.get("upstream_model") or row.get("model") or ""
        ).strip()
        if not public and not upstream:
            continue
        # A public name defaults to the upstream name and vice versa.
        public = public or upstream
        upstream = upstream or public
        normalized = copy.deepcopy(dict(row))
        normalized.pop("public", None)
        normalized.pop("upstream", None)
        normalized["model_group"] = public
        normalized["upstream_model"] = upstream
        out.append(normalized)
    return out


def normalize_protocol_frontend(frontend_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    source = str(raw.get("source") or "ui")
    if source not in PROTOCOL_FRONTEND_SOURCES:
        source = "ui"
    out = copy.deepcopy(dict(raw))
    out.update({
        "id": frontend_id,
        "name": str(raw.get("name") or frontend_id),
        "protocol": str(raw.get("protocol") or frontend_id),
        "source": source,
        "editable": bool_from_config(raw.get("editable"), source == "ui"),
        "enabled": bool_from_config(raw.get("enabled"), True),
        "routes": normalize_routes(raw.get("routes")),
        "intents": normalize_string_list(raw.get("intents")),
        "canonical": bool_from_config(raw.get("canonical"), False),
    })
    return out


def normalize_workflow_record(workflow_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    manifest = raw.get("manifest")
    if not isinstance(manifest, Mapping):
        manifest = {
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key
            not in {
                "editable",
                "enabled",
                "identity",
                "incarnation",
                "manifest_hash",
                "migration",
                "overrides",
                "policy",
                "provenance",
                "published",
                "record_revision",
                "registry_source",
                "source_path",
                "state",
                "summary",
            }
        }
    manifest_dict = copy.deepcopy(dict(manifest))
    enforce_contract(manifest_dict)   # a save refuses what a load refuses; schema 1 is refused, not converted
    frontend_profiles.check(manifest_dict)   # and what the frontend that serves its intent cannot carry (D9)
    source = str(raw.get("registry_source") or raw.get("source") or "ui")
    if source not in DATA_OBJECT_SOURCES:
        source = "ui"
    identity = manifest_identity(manifest_dict, stored_provenance(source))
    policy = raw.get("policy")
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    facts = stored_facts(policy, manifest_dict.get("intent")) or _release_facts_of(identity.id, source, provenance)
    source_path = str(raw.get("source_path") or "")
    manifest_path = str(raw.get("manifest_path") or "")
    record = WorkflowRecord(
        manifest=manifest_dict,
        identity=identity,
        source_path=Path(source_path) if source_path else Path(""),
        facts=facts,
    )
    normalized = {
        "id": identity.id or workflow_id,
        "source": source,
        "editable": bool_from_config(raw.get("editable"), source == "ui"),
        "enabled": bool_from_config(raw.get("enabled"), True),
        "manifest": manifest_dict,
        "identity": {
            "version": identity.version,
            "manifest_hash": identity.manifest_hash,
            "trusted": identity.trusted,
            "source": identity.source,
        },
        "summary": summarize_workflow(record, audience="internal"),
        "source_path": source_path,
        "manifest_path": manifest_path,
        "provenance": copy.deepcopy(dict(provenance)),
    }
    stated = stored_facts(policy, manifest_dict.get("intent"))
    if stated is not None:
        # The record's own facts (an owner's patch over the release's): kept, so a read and a write back keep them.
        normalized["policy"] = {name: list(getattr(stated, name)) if name == "tags" else getattr(stated, name) for name in FACT_NAMES}
        if isinstance(policy.get(RESOURCE_CLASS_KEY), str) and policy[RESOURCE_CLASS_KEY]:
            normalized["policy"][RESOURCE_CLASS_KEY] = policy[RESOURCE_CLASS_KEY]
    normalized.update(run_control_of(raw))
    if isinstance(raw.get("overrides"), str) and raw["overrides"]:
        normalized["overrides"] = raw["overrides"]
    return normalized


def _release_facts_of(workflow_id: str, source: str, provenance: Any) -> Any:
    """The release's facts for a stored workflow. A record an installed package wrote (source ``imported``) gets a row that
    names the package it came from; any other record gets a row that names none."""
    stated = provenance if isinstance(provenance, Mapping) else {}
    package_id, package_hash = stated.get("package_id"), stated.get("package_hash")
    return release_facts(workflow_id, package_id=package_id if isinstance(package_id, str) else None,
                         package_hash=package_hash if isinstance(package_hash, str) else None,
                         from_package=source == "imported")


def stored_provenance(source: str) -> Provenance:
    """A stored record is not the release's own file: it is not reviewed, and its source is where it was stored from."""
    return Provenance(trusted=False, source=source)


def normalize_capability_record(capability_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    source = str(raw.get("source") or "ui")
    if source not in DATA_OBJECT_SOURCES:
        source = "ui"
    resources = raw.get("resources")
    if isinstance(resources, ResourceEstimate):
        resource_estimate = resources
    elif isinstance(resources, Mapping):
        resource_estimate = ResourceEstimate(**dict(resources))
    else:
        resource_estimate = ResourceEstimate()
    capability = Capability(
        intent=str(raw.get("intent") or ""),
        backend=str(raw.get("backend") or ""),
        model=str(raw["model"]) if raw.get("model") is not None else None,
        workflow=str(raw["workflow"]) if raw.get("workflow") is not None else None,
        input_modalities=tuple(normalize_string_list(raw.get("input_modalities"))),
        output_modalities=tuple(normalize_string_list(raw.get("output_modalities"))),
        execution=str(raw.get("execution") or "sync"),
        streaming=bool_from_config(raw.get("streaming"), False),
        exposure=str(raw.get("exposure") or "default"),
        trust_tier=str(raw.get("trust_tier") or "stable"),
        resource_tier=str(raw.get("resource_tier") or "interactive"),
        tags=tuple(normalize_string_list(raw.get("tags"))),
        lifecycle=str(raw.get("lifecycle") or "unknown"),
        health=str(raw.get("health") or "unknown"),
        policy_available=bool_from_config(raw.get("policy_available"), True),
        resources=resource_estimate,
        caveats=tuple(normalize_string_list(raw.get("caveats"))),
        policy_sources=tuple(normalize_string_list(raw.get("policy_sources"))),
        policy_reasons=tuple(normalize_string_list(raw.get("policy_reasons"))),
    )
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    normalized = summarize_capability(capability, audience="internal")
    normalized.update(
        {
            "id": capability_id,
            "source": source,
            "editable": bool_from_config(raw.get("editable"), source == "ui"),
            "enabled": bool_from_config(raw.get("enabled"), True),
            "provenance": copy.deepcopy(dict(provenance)),
        }
    )
    return normalized


def workflow_records_from_registry(registry: Mapping[str, Any]) -> list[WorkflowRecord]:
    normalized = normalize_service_registry(registry)
    records: list[WorkflowRecord] = []
    for record in (normalized.get("workflows") or {}).values():
        if not isinstance(record, Mapping) or not record.get("enabled", True):
            continue
        manifest = record.get("manifest")
        if not isinstance(manifest, Mapping):
            continue
        manifest_dict = copy.deepcopy(dict(manifest))
        identity = manifest_identity(manifest_dict, stored_provenance(str(record.get("source") or "ui")))
        source_path = str(record.get("source_path") or "")
        records.append(
            WorkflowRecord(
                manifest=manifest_dict,
                identity=identity,
                source_path=Path(source_path) if source_path else Path("admin.db"),
                facts=stored_facts(record.get("policy"), manifest_dict.get("intent")) or _release_facts_of(
                    identity.id, str(record.get("source") or "ui"), record.get("provenance")),
            )
        )
    return records


class BundledWorkflowId(ValueError):
    """A save would store a workflow under the id of a bundled one without saying it overrides it (8.4).

    ``bundled`` is the manifest the release ships, so a client can copy it, edit the copy and save it with ``overrides``.
    """

    def __init__(self, workflow_id: str, bundled: Mapping[str, Any]) -> None:
        super().__init__(f"workflow {workflow_id!r} is a bundled workflow; to replace it, save with overrides: {workflow_id!r}")
        self.workflow_id = workflow_id
        self.bundled = copy.deepcopy(dict(bundled))


def refuse_unclaimed_bundled_workflows(
    payload: Mapping[str, Any], registry: Mapping[str, Any], *, before: Mapping[str, Any] | None = None,
) -> None:
    """Refuse a whole-registry save that adds or changes a workflow under a bundled id unless its record says ``overrides: <id>``.

    A stored override the save leaves as it was is not a new claim, so a registry read and written back unchanged
    saves. ``registry`` is ``payload`` normalized; ``overrides`` is read from the payload, since a stored record
    does not keep it. The release has to ship the id: an id it does not ship needs no claim.
    """
    bundled = bundled_workflow_manifests()
    previous = (before or {}).get("workflows") or {}
    for workflow_id, record in (registry.get("workflows") or {}).items():
        if workflow_id not in bundled:
            continue
        stored = previous.get(workflow_id)
        if isinstance(stored, Mapping) and stored.get("manifest") == record.get("manifest"):
            continue
        if not _claims_override(payload, workflow_id):
            raise BundledWorkflowId(workflow_id, bundled[workflow_id])


def _claims_override(payload: Mapping[str, Any], workflow_id: str) -> bool:
    for key, raw in (payload.get("workflows") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        manifest = raw.get("manifest")
        if workflow_id in (key, raw.get("id"), manifest.get("id") if isinstance(manifest, Mapping) else None):
            return raw.get("overrides") == workflow_id
    return False


def workflow_overrides(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{id: {"source": "owner", "fields": [...]}}`` for each stored workflow that has the id of a bundled one.

    The fields are the names of the manifest fields that differ from the bundled manifest, never values. A record that
    is not listed serves from the release, or is the owner's own (no bundled manifest has its id).
    """
    bundled = bundled_workflow_manifests()
    view: dict[str, dict[str, Any]] = {}
    for workflow_id, record in (registry.get("workflows") or {}).items():
        manifest = record.get("manifest") if isinstance(record, Mapping) else None
        if workflow_id in bundled and isinstance(manifest, Mapping):
            base = bundled[workflow_id]
            view[workflow_id] = {
                "source": registry_layers.SOURCE_OWNER,
                "fields": sorted(key for key in {*base, *manifest} if base.get(key) != manifest.get(key)),
            }
    return view


# -- run control of a stored workflow record (CODE_TO_DATA_S4_S8, I2 and 5.2) ---------------------------------------------
# ``state`` and ``published`` are the owner's run control; ``incarnation`` is set when the id is created and never changes
# until it is deleted; ``record_revision`` counts the accepted changes of this one workflow. All four sit beside the
# manifest on the stored record, so a build that does not know them ignores them (5.5).

RESOURCE_CLASS_KEY = "resource_class"
WORKFLOW_STATES = ("active", "disabled")
WORKFLOW_RECORD_STEP = "workflow-record-1"
RUN_CONTROL_KEYS = ("state", "published", "incarnation", "record_revision")
# What a stored record that predates the step means: nothing visible changes (5.5). The incarnation is the one thing
# that cannot be invented; the step writes a fresh one, and a reader that meets none uses this label.
UNSTEPPED_INCARNATION = "unstepped"


def run_control_of(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The run-control keys a stored record states, valid ones only. Nothing is filled in here: a record the step has not
    reached keeps looking as it did."""
    found: dict[str, Any] = {}
    if raw.get("state") in WORKFLOW_STATES:
        found["state"] = raw["state"]
    if isinstance(raw.get("published"), bool):
        found["published"] = raw["published"]
    if isinstance(raw.get("incarnation"), str) and raw["incarnation"]:
        found["incarnation"] = raw["incarnation"]
    revision = raw.get("record_revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
        found["record_revision"] = revision
    return found


def with_run_control_defaults(record: Mapping[str, Any]) -> dict[str, Any]:
    """A stored record with the run control a pre-step record means: active, published, revision 1."""
    out = dict(record)
    out.setdefault("state", "active")
    out.setdefault("published", True)
    out.setdefault("incarnation", UNSTEPPED_INCARNATION)
    out.setdefault("record_revision", 1)
    return out


def plan_workflow_record_step(stored: Mapping[str, Any], *, new_incarnation: Any) -> dict[str, Any]:
    """The owner-data document after the ``workflow-record-1`` step: every stored workflow record states ``state: active``,
    ``published: true``, a fresh ``incarnation`` and ``record_revision: 1`` where it states none. A key a record already
    holds is kept. Pure: ``new_incarnation()`` supplies the fresh values."""
    out = copy.deepcopy(dict(stored))
    owned = out.get("workflows")
    if not isinstance(owned, Mapping):
        return out
    stepped: dict[str, Any] = {}
    for record_id, entry in owned.items():
        body = entry.get(registry_layers.REPLACE_KEY) if isinstance(entry, Mapping) and set(entry) == {registry_layers.REPLACE_KEY} else entry
        if not isinstance(body, Mapping):
            stepped[record_id] = copy.deepcopy(entry)
            continue
        fresh = {**with_run_control_defaults(body), **run_control_of(body)}
        if "incarnation" not in run_control_of(body):
            fresh["incarnation"] = new_incarnation()
        stepped[record_id] = {registry_layers.REPLACE_KEY: fresh} if body is not entry else fresh
    out["workflows"] = stepped
    return out


def ensure_workflow_record_step(*, path: Path, actor: str = "system") -> dict[str, Any] | None:
    """Run the step once: the stored registry after it, or None when there is no stored registry. The step is recorded in
    the same transaction as the registry revision it writes. Bounded retry on a concurrent write, as the other steps."""
    import secrets

    for _attempt in range(3):
        done = admin_store.migration_step_recorded(WORKFLOW_RECORD_STEP, path=path)
        record = admin_store.get_service_registry_json_with_generation(path=path)
        if record is None or not _parses_as_object(record["value_json"]):
            return None
        stored = _stored_document(record["value_json"])
        if done:
            return stored
        if admin_store.migration_step_recorded(WORKFLOW_RECORD_STEP, path=path):
            continue
        generation = int(record["operator_settings_generation"])
        planned = plan_workflow_record_step(stored, new_incarnation=lambda: secrets.token_hex(8))
        if planned == stored:
            try:
                admin_store.record_migration_step_at_registry_generation(
                    WORKFLOW_RECORD_STEP, expected_generation=generation, path=path)
            except admin_store.OperatorSettingsGenerationConflict:
                continue
            return stored
        try:
            admin_store.write_service_registry_with_audit(
                planned,
                expected_generation=generation,
                updated_by=actor,
                audit_action="operator.service_registry.workflow_record_1",
                audit_actor_kind="admin",
                audit_actor_id=actor,
                audit_actor_label="System",
                audit_target_kind="operator_setting",
                audit_target_id=SERVICE_REGISTRY_SETTING_KEY,
                audit_metadata={"migration_step": WORKFLOW_RECORD_STEP, "workflow_count": len(planned.get("workflows") or {})},
                session=None,  # the gateway migrating its own registry at start: no browser sign-in asked
                path=path,
                migration_step=WORKFLOW_RECORD_STEP,
                stored_form=True,
            )
        except admin_store.OperatorSettingsGenerationConflict:
            continue
        return planned
    raise RuntimeError("service registry changed during bounded startup retry")


# -- names (CODE_TO_DATA_S4_S8, 3.0 and 5.6) --------------------------------------------------------------------------


class NameClash(ValueError):
    """A write of the registry document would give one name to two owners (``id_clash``). ``problems`` names each clash."""

    code = "id_clash"

    def __init__(self, problems: Iterable[Any]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(problem.message for problem in self.problems))


def workflow_name_pairs(workflow_id: str) -> list[tuple[str, Any]]:
    """The names one workflow holds: its id and the operation and component names its id generates."""
    from gateway import workflow_kernel
    from gateway.workflow_runtime_types import NameOwner

    return [(workflow_id, NameOwner("workflow", workflow_id)),
            *((name, NameOwner(kind, workflow_id)) for kind, name in workflow_kernel.generated_names(workflow_id).items())]


_dynamic_model_aliases: Callable[[], Mapping[str, Any] | None] = lambda: None


def set_dynamic_model_aliases_source(source: Callable[[], Mapping[str, Any] | None]) -> None:
    """Where the write-time name check reads the ``model_aliases`` config section (the snapshot builder reads the same one)."""
    global _dynamic_model_aliases
    _dynamic_model_aliases = source


def dynamic_model_aliases() -> Mapping[str, Any] | None:
    return _dynamic_model_aliases()


def registry_name_pairs(document: Mapping[str, Any], bundled: Iterable[str] | None = None) -> list[tuple[str, Any]]:
    """Every (name, owner) pair a registry document holds: the workflows it stores and the bundled ones, model ids and
    model aliases of its services, its frontend ids and the routes they register. ``document`` is the effective document."""
    from gateway import workflow_kernel
    from gateway.workflow_runtime_types import NameOwner

    pairs: list[tuple[str, Any]] = []
    stored = document.get("workflows") if isinstance(document.get("workflows"), Mapping) else {}
    shipped = bundled_workflow_manifests() if bundled is None else bundled
    ids = {str(workflow_id) for workflow_id in shipped}
    ids.update(str(workflow_id) for workflow_id in stored)
    for workflow_id in sorted(ids):
        pairs.extend(workflow_name_pairs(workflow_id))
        record = stored.get(workflow_id)
        contract = record.get("manifest", record) if isinstance(record, Mapping) else None
        if not isinstance(contract, Mapping):   # a bundled id the document does not store: the release's own contract
            contract = shipped.get(workflow_id) if isinstance(shipped, Mapping) else None
        aliases = contract.get("aliases") if isinstance(contract, Mapping) else None
        pairs.extend((str(alias), NameOwner("workflow_alias", workflow_id)) for alias in aliases or () if isinstance(alias, str))
    # Model ids, model aliases and the dynamic aliases come from the snapshot builder's own functions: one name source.
    from gateway import registry_snapshot
    services = document.get("services") if isinstance(document.get("services"), Mapping) else {}
    pairs.extend(registry_snapshot.service_model_names(services))
    pairs.extend(registry_snapshot.dynamic_alias_names(dynamic_model_aliases()))
    frontends = document.get("protocol_frontends") if isinstance(document.get("protocol_frontends"), Mapping) else {}
    for key, frontend in frontends.items():
        if not isinstance(frontend, Mapping):
            continue
        frontend_id = str(frontend.get("id") or key)
        pairs.append((frontend_id, NameOwner("frontend", frontend_id)))
        for row in frontend.get("registered_routes") or ():
            if not isinstance(row, Mapping):
                continue
            for path in (row.get("canonical"), row.get("alias")):
                if isinstance(path, str) and path:
                    pairs.extend((workflow_kernel.route_key(method, path), NameOwner("route", frontend_id))
                                 for method in row.get("methods") or () if isinstance(method, str))
    return pairs


def new_name_clashes(before: list[tuple[str, Any]], after: list[tuple[str, Any]]) -> tuple[Any, ...]:
    """The clashes ``after`` has and ``before`` did not. A clash the registry already held does not stop an unrelated
    write (it would lock every edit); a write may not add one."""
    from gateway import workflow_kernel

    held = {(problem.code, problem.message) for problem in workflow_kernel.name_clashes(before)}
    return tuple(problem for problem in workflow_kernel.name_clashes(after) if (problem.code, problem.message) not in held)


def refuse_new_name_clashes(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """Raise ``NameClash`` when the effective registry ``after`` holds a name clash that ``before`` did not. The one name
    check every write of the registry document runs (``admin_store.write_service_registry_in_transaction``)."""
    problems = new_name_clashes(registry_name_pairs(before), registry_name_pairs(after))
    if problems:
        raise NameClash(problems)


def capability_from_registry_record(record: Mapping[str, Any]) -> Capability:
    normalized = normalize_capability_record(str(record.get("id") or ""), record)
    resources = normalized.get("resources")
    resource_estimate = (
        ResourceEstimate(**dict(resources))
        if isinstance(resources, Mapping)
        else ResourceEstimate()
    )
    return Capability(
        intent=str(normalized.get("intent") or ""),
        backend=str(normalized.get("backend") or ""),
        model=str(normalized["model"]) if normalized.get("model") is not None else None,
        workflow=str(normalized["workflow"])
        if normalized.get("workflow") is not None
        else None,
        input_modalities=tuple(normalize_string_list(normalized.get("input_modalities"))),
        output_modalities=tuple(normalize_string_list(normalized.get("output_modalities"))),
        execution=str(normalized.get("execution") or "sync"),
        streaming=bool_from_config(normalized.get("streaming"), False),
        exposure=str(normalized.get("exposure") or "default"),
        trust_tier=str(normalized.get("trust_tier") or "stable"),
        resource_tier=str(normalized.get("resource_tier") or "interactive"),
        tags=tuple(normalize_string_list(normalized.get("tags"))),
        lifecycle=str(normalized.get("lifecycle") or "unknown"),
        health=str(normalized.get("health") or "unknown"),
        policy_available=bool_from_config(normalized.get("policy_available"), True),
        resources=resource_estimate,
        caveats=tuple(normalize_string_list(normalized.get("caveats"))),
        policy_sources=tuple(normalize_string_list(normalized.get("policy_sources"))),
        policy_reasons=tuple(normalize_string_list(normalized.get("policy_reasons"))),
    )


def capabilities_from_registry(registry: Mapping[str, Any]) -> list[Capability]:
    normalized = normalize_service_registry(registry)
    capabilities: list[Capability] = []
    for record in (normalized.get("capabilities") or {}).values():
        if isinstance(record, Mapping) and record.get("enabled", True):
            capabilities.append(capability_from_registry_record(record))
    return capabilities


def load_seed_catalogs(proxy_config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    catalog_dir = catalog_dir_from_config(proxy_config)
    services = load_catalog_records(catalog_dir / "services.yml", normalizer=normalize_service_record)
    protocol_frontends = load_catalog_records(
        catalog_dir / "protocol_frontends.yml",
        normalizer=normalize_protocol_frontend,
    )
    workflows = load_catalog_records(
        catalog_dir / "workflows.yml",
        normalizer=normalize_workflow_record,
    )
    capabilities = load_catalog_records(
        catalog_dir / "capabilities.yml",
        normalizer=normalize_capability_record,
    )

    for key, normalizer, target in (
        ("services", normalize_service_record, services),
        ("protocol_frontends", normalize_protocol_frontend, protocol_frontends),
        ("workflows", normalize_workflow_record, workflows),
        ("capabilities", normalize_capability_record, capabilities),
    ):
        raw_records = proxy_config.get(key)
        if not isinstance(raw_records, Mapping):
            continue
        for raw_id, raw in raw_records.items():
            if isinstance(raw, Mapping):
                record_id = normalize_id(raw.get("id") or raw_id)
                if record_id:
                    target[record_id] = normalizer(record_id, raw)

    return {
        "services": services,
        "protocol_frontends": protocol_frontends,
        "workflows": workflows,
        "capabilities": capabilities,
    }


def catalog_dir_from_config(proxy_config: Mapping[str, Any]) -> Path:
    registry_cfg = proxy_config.get("registry")
    raw_dir = None
    if isinstance(registry_cfg, Mapping):
        raw_dir = registry_cfg.get("catalog_dir")
    path = Path(str(raw_dir or DEFAULT_CATALOG_DIR))
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent.parent / path


def load_catalog_records(
    path: Path,
    *,
    normalizer,
) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, Mapping) and isinstance(raw.get("records"), list):
        iterable = raw["records"]
    elif isinstance(raw, Mapping):
        iterable = [
            {"id": record_id, **record} if isinstance(record, Mapping) else {"id": record_id}
            for record_id, record in raw.items()
        ]
    elif isinstance(raw, list):
        iterable = raw
    else:
        raise ValueError(f"{path} must contain a mapping or list")

    out: dict[str, dict[str, Any]] = {}
    for item in iterable:
        if not isinstance(item, Mapping):
            continue
        record_id = normalize_id(item.get("id"))
        if not record_id:
            continue
        out[record_id] = normalizer(record_id, item)
    return out


def split_secret_refs(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    safe: dict[str, Any] = {}
    secret_refs: dict[str, Any] = {}
    for key, value in raw.items():
        key_str = str(key)
        if is_secret_key(key_str) and not is_secret_reference_key(key_str) and key_str != "credential_origin":
            secret_refs[key_str] = "<external-secret>"
            continue
        safe[key_str] = copy.deepcopy(value)
    return safe, secret_refs


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def is_secret_reference_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered.endswith("_env")
        or lowered.endswith("_file")
        or lowered.endswith("_ref")
        or lowered.endswith("_secret_id")
    )


def normalize_id(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def canonical_provider_id(value: Any) -> str:
    """Return the canonical key for provider policy, registry and ledger rows."""
    return normalize_id(value)


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value).strip()] if str(value).strip() else []
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def normalize_routes(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            out.append(copy.deepcopy(dict(item)))
        else:
            text = str(item).strip()
            if text:
                out.append(text)
    return out


def bool_from_config(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Client-visible fields from a normalized service record.
_RESOURCE_CLIENT_FIELDS = frozenset({"id", "name", "kind", "enabled", "capabilities"})
_RESOURCE_ADMIN_FIELDS = frozenset({"id", "name", "kind", "enabled", "capabilities", "source", "priority", "role"})


def resource_summary(
    service: Mapping[str, Any],
    *,
    audience: str = "client",
    status: str | None = None,
    queue_depth: int | None = None,
    max_queue_depth: int | None = None,
) -> dict[str, Any]:
    """Project a normalized service record into a client-visible resource summary.

    ``status`` is a caller-supplied reachability hint (e.g. ``"available"``,
    ``"degraded"``, ``"unavailable"``). ``queue_depth`` / ``max_queue_depth``
    are optional runtime facts; include them when the caller has live counters.
    """
    fields = _RESOURCE_ADMIN_FIELDS if audience in ("admin", "internal") else _RESOURCE_CLIENT_FIELDS
    out: dict[str, Any] = {k: service[k] for k in fields if k in service}
    if "kind" in out:
        out["type"] = out["kind"]
    if status is not None:
        out["status"] = status
    if queue_depth is not None:
        out["queue_depth"] = queue_depth
    if max_queue_depth is not None:
        out["max_queue_depth"] = max_queue_depth
    return out


def list_resource_summaries(
    registry: Mapping[str, Any],
    *,
    audience: str = "client",
    runtime_facts: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of resource summaries for all enabled services.

    ``runtime_facts`` is an optional dict keyed by service_id with keys
    ``status``, ``queue_depth``, and ``max_queue_depth``.
    """
    services = registry.get("services") or {}
    facts = runtime_facts or {}
    out = []
    for service_id, service in services.items():
        if not isinstance(service, Mapping):
            continue
        if not bool_from_config(service.get("enabled"), True):
            continue
        svc_facts = facts.get(service_id) or {}
        out.append(resource_summary(
            service,
            audience=audience,
            status=svc_facts.get("status"),
            queue_depth=svc_facts.get("queue_depth"),
            max_queue_depth=svc_facts.get("max_queue_depth"),
        ))
    out.sort(key=lambda s: (int(s.get("priority", 100)) if "priority" in s else 100, s.get("id", "")))
    return out
