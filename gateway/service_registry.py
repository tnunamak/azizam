"""DB-backed service and protocol registry helpers.

The gateway has two different kinds of configuration:

* bootstrap/provisioning inputs needed before the process can start;
* runtime product state operators manage from the dashboard/API.

This module turns legacy ``proxy_config.yml`` backend/API declarations into a
durable registry document in ``admin.db``. After that first seed, the registry
is the runtime source of truth. Provisioned/builtin/discovered rows can still
be read-only, but their ownership is explicit instead of hidden in YAML.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from gateway import admin_store
from gateway.capabilities import Capability, ResourceEstimate, summarize_capability
from gateway.discovery import WorkflowRecord, summarize_workflow
from gateway.manifests import manifest_identity, validate_manifest


SERVICE_REGISTRY_SETTING_KEY = "service_registry"
SERVICE_REGISTRY_SCHEMA_VERSION = 1

SERVICE_SOURCES = frozenset({"ui", "provisioned", "builtin", "discovered", "imported"})
PROTOCOL_FRONTEND_SOURCES = frozenset({"builtin", "ui", "provisioned", "imported"})
DATA_OBJECT_SOURCES = frozenset({"builtin", "ui", "provisioned", "discovered", "imported"})
DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent.parent / "registry"

_SECRET_HINTS = (
    "api_key",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "secret",
    "token",
)


def ensure_service_registry(
    proxy_config: Mapping[str, Any],
    *,
    path: Path = admin_store.DB_PATH,
    actor: str = "system",
) -> dict[str, Any]:
    """Return the DB-owned registry, seeding it from config if absent."""

    record = admin_store.get_operator_setting(SERVICE_REGISTRY_SETTING_KEY, path=path)
    if record is not None and isinstance(record.get("value"), Mapping):
        return backfill_seed_catalog_metadata(record["value"], proxy_config=proxy_config)

    registry = build_service_registry_seed(proxy_config)
    admin_store.put_operator_setting(
        SERVICE_REGISTRY_SETTING_KEY,
        registry,
        updated_by=actor,
        path=path,
    )
    admin_store.record_audit(
        "operator.service_registry.seed",
        actor_kind="admin",
        actor_id=actor,
        target_kind="operator_setting",
        target_id=SERVICE_REGISTRY_SETTING_KEY,
        metadata={
            "service_count": len(registry.get("services") or {}),
            "protocol_frontend_count": len(registry.get("protocol_frontends") or {}),
        },
        path=path,
    )
    return registry


def load_service_registry(*, path: Path = admin_store.DB_PATH) -> dict[str, Any] | None:
    record = admin_store.get_operator_setting(SERVICE_REGISTRY_SETTING_KEY, path=path)
    if record is None or not isinstance(record.get("value"), Mapping):
        return None
    return backfill_seed_catalog_metadata(record["value"])


def build_service_registry_seed(proxy_config: Mapping[str, Any]) -> dict[str, Any]:
    catalogs = load_seed_catalogs(proxy_config)
    services = dict(catalogs.get("services") or {})
    for raw_id, raw in (proxy_config.get("backends") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        service_id = normalize_id(raw_id)
        if not service_id:
            continue
        source = str(raw.get("source") or "provisioned")
        services[service_id] = service_from_backend_config(
            service_id,
            raw,
            source=source,
            provenance={
                "seed": f"proxy_config.yml:backends.{raw_id}",
                "seed_hash": stable_hash(raw),
            },
        )

    return {
        "schema_version": SERVICE_REGISTRY_SCHEMA_VERSION,
        "services": services,
        "protocol_frontends": catalogs.get("protocol_frontends") or {},
        "workflows": catalogs.get("workflows") or {},
        "capabilities": catalogs.get("capabilities") or {},
        "seeded_from": "registry catalog + proxy_config.yml",
        "seed_hash": stable_hash(
            {
                "catalogs": catalogs,
                "backends": proxy_config.get("backends") or {},
            }
        ),
        "updated_at": int(time.time()),
    }


def normalize_service_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    services = {}
    for raw_id, raw in (value.get("services") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        service_id = normalize_id(raw.get("id") or raw_id)
        if not service_id:
            continue
        services[service_id] = normalize_service_record(service_id, raw)

    frontends = {}
    for raw_id, raw in (value.get("protocol_frontends") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        frontend_id = normalize_id(raw.get("id") or raw_id)
        if not frontend_id:
            continue
        frontends[frontend_id] = normalize_protocol_frontend(frontend_id, raw)

    workflows = {}
    for raw_id, raw in (value.get("workflows") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        workflow_id = normalize_id(raw.get("id") or raw_id)
        if not workflow_id:
            continue
        record = normalize_workflow_record(workflow_id, raw)
        workflows[record["id"]] = record

    capabilities = {}
    for raw_id, raw in (value.get("capabilities") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        capability_id = normalize_id(raw.get("id") or raw_id)
        if not capability_id:
            continue
        record = normalize_capability_record(capability_id, raw)
        capabilities[record["id"]] = record

    return {
        "schema_version": int(value.get("schema_version") or SERVICE_REGISTRY_SCHEMA_VERSION),
        "services": services,
        "protocol_frontends": frontends,
        "workflows": workflows,
        "capabilities": capabilities,
        "seeded_from": str(value.get("seeded_from") or ""),
        "seed_hash": str(value.get("seed_hash") or ""),
        "updated_at": int(value.get("updated_at") or time.time()),
    }


def backfill_seed_catalog_metadata(
    registry: Mapping[str, Any],
    *,
    proxy_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge new bundled metadata into older DB-owned registry records."""

    out = normalize_service_registry(registry)
    catalogs = load_seed_catalogs(proxy_config or {})
    _backfill_collection_metadata(
        out["protocol_frontends"],
        catalogs.get("protocol_frontends") or {},
        keys=("registered_routes", "asset_content_policies", "public_discovery"),
    )
    return out


def _backfill_collection_metadata(
    records: dict[str, dict[str, Any]],
    seeds: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> None:
    for record_id, record in records.items():
        seed = seeds.get(record_id)
        if not isinstance(seed, Mapping):
            continue
        for key in keys:
            if key not in record and key in seed:
                record[key] = copy.deepcopy(seed[key])


def apply_service_registry_to_config(
    proxy_config: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a config copy whose backend registry comes from admin.db."""

    out = copy.deepcopy(dict(proxy_config))
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if isinstance(services, Mapping):
        out["backends"] = {
            service_id: backend_config_from_service(service)
            for service_id, service in services.items()
            if isinstance(service, Mapping)
        }
    return out


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
        record = normalize_service_record(service_id, raw)
        _merge_imported_record(out["services"], service_id, record, "service")

    for raw_id, raw in (protocol_frontends or {}).items():
        if not isinstance(raw, Mapping):
            continue
        frontend_id = normalize_id(raw.get("id") or raw_id)
        if not frontend_id:
            continue
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
    return out


def _merge_imported_record(
    target: dict[str, dict[str, Any]],
    record_id: str,
    record: dict[str, Any],
    kind: str,
) -> None:
    existing = target.get(record_id)
    if existing is not None and not _same_imported_package_record(existing, record):
        raise ServiceRegistryConflict(f"{kind} {record_id!r} already exists")
    target[record_id] = record


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


def service_from_backend_config(
    service_id: str,
    raw: Mapping[str, Any],
    *,
    source: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_config, secret_refs = split_secret_refs(raw)
    source = source if source in SERVICE_SOURCES else "ui"
    name = str(raw.get("name") or service_id)
    capabilities = normalize_string_list(raw.get("capabilities"))
    return {
        "id": service_id,
        "name": name,
        "kind": str(raw.get("kind") or "provider"),
        "source": source,
        "editable": bool(raw.get("editable", source == "ui")),
        "enabled": bool_from_config(raw.get("enabled"), True),
        "priority": int(raw.get("priority") or 100),
        "role": str(raw.get("role") or "required"),
        "capabilities": capabilities,
        "config": safe_config,
        "secret_refs": secret_refs,
        "lifecycle": {
            "type": str(raw.get("lifecycle") or raw.get("lifecycle_type") or "external"),
            "managed": bool_from_config(raw.get("managed"), False),
            "service_name": str(raw.get("service_name") or ""),
        },
        "provenance": dict(provenance or {}),
    }


def normalize_service_record(service_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    source = str(raw.get("source") or "ui")
    if source not in SERVICE_SOURCES:
        source = "ui"
    config = raw.get("config")
    if not isinstance(config, Mapping):
        config = {}
    capabilities = normalize_string_list(raw.get("capabilities") or config.get("capabilities"))
    lifecycle = raw.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        lifecycle = {}
    secret_refs = raw.get("secret_refs")
    if not isinstance(secret_refs, Mapping):
        secret_refs = {}
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    return {
        "id": service_id,
        "name": str(raw.get("name") or config.get("name") or service_id),
        "kind": str(raw.get("kind") or "provider"),
        "source": source,
        "editable": bool_from_config(raw.get("editable"), source == "ui"),
        "enabled": bool_from_config(raw.get("enabled"), True),
        "priority": int(raw.get("priority") or 100),
        "role": str(raw.get("role") or config.get("role") or "required"),
        "capabilities": capabilities,
        "config": copy.deepcopy(dict(config)),
        "secret_refs": copy.deepcopy(dict(secret_refs)),
        "lifecycle": {
            "type": str(lifecycle.get("type") or "external"),
            "managed": bool_from_config(lifecycle.get("managed"), False),
            "service_name": str(lifecycle.get("service_name") or ""),
        },
        "provenance": copy.deepcopy(dict(provenance)),
    }


def backend_config_from_service(service: Mapping[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(dict(service.get("config") or {}))
    raw["name"] = str(service.get("name") or raw.get("name") or service.get("id") or "")
    raw["kind"] = str(service.get("kind") or raw.get("kind") or "")
    raw["service_source"] = str(service.get("source") or "ui")
    raw["editable"] = bool_from_config(service.get("editable"), raw.get("service_source") == "ui")
    raw["priority"] = int(service.get("priority") or raw.get("priority") or 100)
    raw["enabled"] = bool_from_config(service.get("enabled"), True)
    raw["role"] = str(service.get("role") or raw.get("role") or "required")
    raw["capabilities"] = normalize_string_list(
        service.get("capabilities") or raw.get("capabilities")
    )
    lifecycle = service.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        raw["managed"] = bool_from_config(lifecycle.get("managed"), raw.get("managed", False))
        if lifecycle.get("service_name"):
            raw["service_name"] = str(lifecycle["service_name"])
        if lifecycle.get("type") and lifecycle.get("type") != "external":
            raw["lifecycle"] = str(lifecycle["type"])
    return {key: value for key, value in raw.items() if value not in (None, "")}


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
                "manifest_hash",
                "provenance",
                "registry_source",
                "source_path",
                "summary",
            }
        }
    manifest_dict = copy.deepcopy(dict(manifest))
    validate_manifest(manifest_dict)
    identity = manifest_identity(manifest_dict)
    source = str(raw.get("registry_source") or raw.get("source") or "ui")
    if source not in DATA_OBJECT_SOURCES:
        source = "ui"
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    source_path = str(raw.get("source_path") or "")
    manifest_path = str(raw.get("manifest_path") or "")
    record = WorkflowRecord(
        manifest=manifest_dict,
        identity=identity,
        source_path=Path(source_path) if source_path else Path(""),
    )
    return {
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
        identity = manifest_identity(manifest_dict)
        source_path = str(record.get("source_path") or "")
        records.append(
            WorkflowRecord(
                manifest=manifest_dict,
                identity=identity,
                source_path=Path(source_path) if source_path else Path("admin.db"),
            )
        )
    return records


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
        if is_secret_key(key_str) and not is_secret_reference_key(key_str):
            secret_refs[key_str] = "<external-secret>"
            continue
        safe[key_str] = copy.deepcopy(value)
    return safe, secret_refs


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def is_secret_reference_key(key: str) -> bool:
    lowered = key.lower()
    return lowered.endswith("_env") or lowered.endswith("_file") or lowered.endswith("_ref")


def normalize_id(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


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
