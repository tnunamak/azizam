"""The registered projection types: every field of every domain type that a management response carries (decision D28).

Each field is classified once, here, as public data, a reference to the secret store, a carrier of secret entries,
or omitted; a field the type does not name is forbidden. ``verify()`` runs at startup and in CI: it fails when the
code that builds a record produces a field that no type here classifies, so a new field cannot be added without
deciding what it is. A secret field costs one entry (a ``Configured`` or a carrier); a public field one scalar.

The carriers are derived from ``gateway/record_carriers.py``, so the model that decides where a secret can live and
the projection that hides it cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from gateway import record_carriers
from gateway.projection import (
    ANY_SCALAR, ARGV, BOOL, CONFIGURED, ENV, HEADERS, INLINE_SECRET, INT, JSON, NUM, OMIT, REF_MAP, SCALAR_SECRET, SHORT, STR, URL,
    URL_ENTRIES, Dict, Obj, Policy, Projection, Seq, mapping, project, register, seq,
)


def _urls(paths: tuple[str, ...], prefix: str) -> dict[str, Policy]:
    """Every URL field ``record_carriers`` declares under ``prefix`` and the entries that sit beside it."""
    out: dict[str, Policy] = {}
    for path in paths:
        parent, _, key = path.rpartition(".")
        if parent == prefix:
            out[key] = URL
            out[key + record_carriers.SECRETS_SUFFIX] = URL_ENTRIES
    return out


# --- the service record --------------------------------------------------------------------------------------------

_COMMANDS = {key: ARGV for key in record_carriers.COMMAND_KEYS}

PROFILE_APPLY = Obj("ProfileApply", {**_COMMANDS, "env": ENV, "kind": SHORT}, extras=JSON)

DRIVER_CONFIG = Obj("DriverConfig", {
    **_COMMANDS, "env": ENV, "profile_apply": PROFILE_APPLY,
    "health_endpoint": URL, "health_endpoint" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
}, extras=JSON)

LIFECYCLE = Obj("Lifecycle", {
    "type": SHORT, "managed": BOOL, "service_name": STR, "driver": SHORT, "driver_config": DRIVER_CONFIG,
    "admin_key": SCALAR_SECRET,
})

PROCESS = Obj("Process", {
    **_COMMANDS, "driver": SHORT, "legacy_driver": SHORT, "needs_owner_conversion": BOOL, "profile_apply": PROFILE_APPLY,
    "port": JSON, "cwd": STR, "env": ENV, "ready_timeout_s": NUM, "stop_timeout_s": NUM, "restart": JSON,
    "log_max_bytes": INT, "legacy_lifecycle": LIFECYCLE,
    "health_endpoint": URL, "health_endpoint" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
})

CONFIG = Obj("ServiceConfig", {
    "headers": HEADERS, "env": ENV, "process": PROCESS, "lifecycle": LIFECYCLE,
    "api_key_secret_id": CONFIGURED,
    **_urls(record_carriers.URL_FIELD_PATHS, "config"),
    "context_window_probe": Obj("ContextWindowProbe", {
        "endpoint": URL, "endpoint" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES}, extras=JSON),
}, extras=JSON)

_HTTP = Obj("ObservationHttp", {
    "path": URL, "path" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
    "url": URL, "url" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
}, extras=JSON)
OBSERVATION = Obj("Observation", {"http": _HTTP}, extras=JSON)

SERVICE_RECORD_FIELDS_PROJECTED = {
    "id": STR, "name": STR, "kind": SHORT, "source": SHORT, "enabled": BOOL, "priority": INT, "role": SHORT,
    "capabilities": seq(SHORT), "config": CONFIG, "secret_refs": REF_MAP, "lifecycle": LIFECYCLE, "provenance": JSON,
    "process": PROCESS, "observations": mapping(OBSERVATION), "residency": JSON, "resources": JSON, "editable": BOOL,
    # Left by the schema 2 migration on a record it refused; the text is the gateway's own and is never shown.
    "migration_refused": OMIT,
}
SERVICE_RECORD = register(Obj("ServiceRecord", SERVICE_RECORD_FIELDS_PROJECTED))

REGISTRY_DOC = register(Obj("RegistryDoc", {
    "schema_version": INT, "services": mapping(SERVICE_RECORD), "protocol_frontends": JSON, "workflows": JSON,
    "capabilities": JSON, "seeded_from": STR, "seed_hash": STR, "updated_at": INT,
}))

# A record the registry leaves out. ``record`` is already a projection of the stored record (built once, by
# ``excluded_view``); here it is carried as public JSON.
EXCLUDED_ITEM = register(Obj("ExcludedItem", {
    "section": SHORT, "id": SHORT, "error": STR, "parsed": BOOL, "secret_slots": seq(STR), "record": JSON,
    "failed_fields": seq(SHORT),
}))

# --- extension packages ------------------------------------------------------------------------------------------------

PACKAGE_SOURCE = register(Obj("PackageSource", {
    "type": SHORT, "kind": SHORT, "id": SHORT, "name": STR, "ref": SHORT, "resolved_ref": SHORT, "commit": SHORT,
    "tag": SHORT, "branch": SHORT, "subdir": STR, "path_in_repo": STR, "path": STR, "directory": STR,
    **{key: URL for key in record_carriers.PACKAGE_SOURCE_URL_KEYS},
    **{key + record_carriers.SECRETS_SUFFIX: URL_ENTRIES for key in record_carriers.PACKAGE_SOURCE_URL_KEYS},
}))
PACKAGE_RECORD = register(Obj("PackageRecord", {
    "id": STR, "name": STR, "version": STR, "enabled": BOOL, "source": PACKAGE_SOURCE, "package_hash": STR,
    "object_counts": JSON, "services": seq(STR), "protocol_frontends": seq(STR), "capabilities": seq(STR),
    "workflows": seq(STR), "adapters": seq(STR), "translators": seq(STR), "code_objects": JSON,
    "requires_code_trust": BOOL, "warnings": seq(STR), "updated_at": INT,
}))
PACKAGE_INDEX = register(Obj("PackageIndex", {
    "schema_version": INT, "sources": seq(PACKAGE_SOURCE), "packages": mapping(PACKAGE_RECORD), "updated_at": INT,
}))
REVIEW = register(Obj("PackageReview", {
    "package": JSON, "package_hash": STR, "services": seq(SERVICE_RECORD), "protocol_frontends": JSON,
    "capabilities": JSON, "workflows": JSON, "adapters": JSON, "translators": JSON, "code_objects": JSON,
    "requires_code_trust": BOOL, "warnings": seq(STR), "errors": seq(STR), "ok": BOOL, "schema_version": ANY_SCALAR,
    "kind": ANY_SCALAR, "id": ANY_SCALAR, "version": ANY_SCALAR, "object_counts": JSON,
    "resolved_source": PACKAGE_SOURCE,
}))

# --- the router config preview -----------------------------------------------------------------------------------------

LITELLM_PREVIEW = register(Obj("LitellmPreview", {
    "model_list": seq(Obj("LitellmEntry", {
        "model_name": STR,
        "litellm_params": Obj("LitellmParams", {
            "model": STR, "api_base": URL, "api_key": STR, "timeout": NUM, "num_retries": INT, "max_retries": INT,
            # header names with a placeholder: the executable config holds the values, and is never a response
            "extra_headers": mapping(STR),
        }),
        "model_info": JSON,
    })),
    "litellm_settings": JSON, "general_settings": JSON,
}))

# --- provider settings ---------------------------------------------------------------------------------------------------

PROVIDER_KEY_STATUS = Obj("ProviderKeyStatus", {
    "state": SHORT, "source": SHORT, "api_key_env": ANY_SCALAR, "display": STR, "updated_at": ANY_SCALAR,
    "api_key_secret_id": CONFIGURED,
})
PROVIDER_VIEW = register(Obj("ProviderView", {
    "id": STR, "name": STR, "enabled": BOOL, "reachable": ANY_SCALAR, "checked_at": ANY_SCALAR, "provider_type": SHORT,
    "models": JSON, "key_status": PROVIDER_KEY_STATUS, "spend": JSON, "caps": JSON, "access_plans": JSON,
    "cap_guarantee": JSON, "concurrency": JSON, "recommendations": JSON, "catalog": JSON,
}))

# --- overrides ------------------------------------------------------------------------------------------------------------

OVERRIDES = register(Obj("Overrides", {"service_availability": JSON, "media_job_admission": JSON}, extras=JSON))

# --- the engine page ---------------------------------------------------------------------------------------------------------
# The engine view is assembled by ``engine_admin.engine_view`` from the projection of the record, so each nested part
# below is already a read of entries (never a value); it is carried here as public JSON, field by field.
ENGINE_VIEW = register(Obj("EngineView", {
    "service_id": STR, "definition": STR, "definition_version": ANY_SCALAR, "inferred": BOOL, "name": STR,
    "enabled": BOOL, "locked": BOOL, "editable": BOOL, "not_editable_reason": STR, "lifecycle_editable": BOOL,
    "values": JSON, "key": JSON, "capabilities": JSON, "models": JSON, "tests": JSON, "check": JSON, "calls": JSON,
    "other_settings": JSON, "record": JSON, "v2": JSON, "access": JSON,
}))

# --- the effective configuration -------------------------------------------------------------------------------------------
# ``GET /admin/api/settings/effective`` shows the running configuration: the YAML the operator keeps, the OIDC
# settings, the registry and the overrides. A top-level section the type does not name is not shown, so a new
# section cannot appear in the view before someone says what it holds.

BACKEND_VIEW = Obj("BackendView", {
    "headers": HEADERS, "env": ENV, "process": PROCESS, "lifecycle": LIFECYCLE,
    "api_key": INLINE_SECRET, "admin_key": INLINE_SECRET, "api_key_secret_id": CONFIGURED,
    **_urls(record_carriers.URL_FIELD_PATHS, "config"),
}, extras=JSON)
EFFECTIVE_CONFIG = register(Obj("EffectiveConfig", {
    "proxy": JSON, "llm": JSON, "backends": mapping(BACKEND_VIEW),
    "images": Obj("ImagesConfig", {"signed_url_secret": INLINE_SECRET}, extras=JSON),
    "model_routing": JSON, "routing": JSON, "identity": JSON, "reasoning": JSON, "roles": JSON, "models": JSON,
    "videos": JSON, "audio": JSON, "model_aliases": JSON, "voices": JSON, "model_compatibility": JSON, "auth": JSON,
    "admin": JSON, "oidc": Obj("OidcSnapshot", {"client_secret": INLINE_SECRET, "client_secret_ref": OMIT}, extras=JSON),
    "service_registry": REGISTRY_DOC, "instance": JSON, "receipt_retention": JSON, "operator_overrides": OVERRIDES,
}))

# --- the keys of a management response body --------------------------------------------------------------------------------
# A route answers with a mapping; each key is a registered type. A key that is not here is a programming error:
# the projector refuses the whole body, so a new route cannot answer with a field nobody classified.
EXCLUDED_LIST = seq(EXCLUDED_ITEM)
BODY_KEYS: dict[str, Policy] = {
    "service_registry": REGISTRY_DOC,
    "excluded": EXCLUDED_LIST,
    "extension_packages": PACKAGE_INDEX,
    "extension_package": PACKAGE_RECORD,
    "review": REVIEW,
    "litellm_config": LITELLM_PREVIEW,
    "provider": PROVIDER_VIEW,
    "overrides": OVERRIDES,
    "operator_settings_generation": INT,
    "service_id": STR,
    "engine": ENGINE_VIEW,
    "config": EFFECTIVE_CONFIG,
    "read_only": BOOL,
    "redacted": BOOL,
    "export_url": STR,
}

# The process fields the v2 record view may show; any other field stays stored only.
PUBLIC_PROCESS_FIELDS = (
    "driver", "legacy_driver", "needs_owner_conversion", "start", "stop", "status", "profile_apply",
    "port", "cwd", "env", "ready_timeout_s", "stop_timeout_s", "restart", "log_max_bytes",
)

# What builds each domain type's fields, so ``verify`` can tell when code produces one nobody classified.
SERVICE_RECORD_FIELDS = tuple(SERVICE_RECORD_FIELDS_PROJECTED)


def verify(*, produced: dict[str, tuple[str, ...]] | None = None) -> None:
    """Fail when a builder produces a field that its type does not classify.

    ``produced`` maps a type name to the field names the builder of that type writes; the default asks the code
    that builds records (``service_registry``) and package indexes (``extension_packages``). Raises ``ValueError``
    naming the type and the field, never a value.
    """
    if produced is None:
        from gateway import extension_packages, service_registry

        produced = {
            "ServiceRecord": tuple(service_registry.SERVICE_RECORD_FIELDS),
            "PackageIndex": tuple(extension_packages.PACKAGE_INDEX_FIELDS),
            "PackageRecord": tuple(extension_packages.PACKAGE_RECORD_FIELDS),
        }
    from gateway.projection import TYPES

    for type_name, fields in produced.items():
        classified = TYPES[type_name].fields
        missing = [name for name in fields if name not in classified]
        if missing:
            raise ValueError(f"{type_name} is built with fields that are not classified in projection_types: {', '.join(missing)}")


def record_parses(section: str, record: Any) -> bool:
    """Whether a stored record can be read through its section's type (a mapping whose declared fields have their shape)."""
    from gateway.projection import project_policy

    if not isinstance(record, dict):
        return False
    if section == "services":
        shown = project("ServiceRecord", record)
        return not shown.shape_errors and shown.tree is not None
    return not project_policy(JSON, record).shape_errors


def excluded_view(section: str, record_id: str, error: str, record: Any, *, configured=None) -> dict[str, Any]:
    """A record the registry leaves out, as a caller may read it (decision D28).

    A record that parses into its type is shown through it, so it can be edited (secret slots as configured
    entries). One that does not (text where a mapping belongs) is shown only as its id, its error and the fields
    that failed; ``secret_slots`` lists the secret entries it holds. The error is cleaned of any secret the record
    still holds inline.
    """
    from gateway.projection import project_policy, scrub_text

    legacy = record_carriers.legacy_secret_texts(record) if section == "services" else []
    if section == "services":
        shown = project("ServiceRecord", record, configured=configured)
        parsed = isinstance(record, dict) and not shown.shape_errors and shown.tree is not None
    else:
        shown = project_policy(JSON, record, configured=configured)
        parsed = isinstance(record, dict) and not shown.shape_errors
    failed_fields = sorted({path.split(".")[0].split("[")[0] for path in shown.shape_errors
                            if path.split(".")[0].split("[")[0] in SERVICE_RECORD_FIELDS_PROJECTED}) if not parsed else []
    out: dict[str, Any] = {
        "section": section[:256], "id": scrub_text(record_id[:256] if isinstance(record_id, str) else "", legacy),
        "error": scrub_text(error[:600] if isinstance(error, str) else "", legacy), "parsed": parsed,
        "secret_slots": sorted(set(shown.entries)) if section == "services" else [],
    }
    if parsed:
        out["record"] = shown.tree
    else:
        out["failed_fields"] = failed_fields
    return out


def project_body(body: Any, *, configured=None) -> Projection:
    """A response body (a mapping of registered keys to values) projected key by key."""
    from gateway.projection import ProjectionViolation, _Walk, _project, OMITTED
    from gateway import secret_refs

    if not isinstance(body, dict):
        raise ProjectionViolation("a response body must be a mapping")
    walk = _Walk(configured=configured or secret_refs.configured)
    out: dict[str, Any] = {}
    for key, value in body.items():
        policy = BODY_KEYS.get(key) if isinstance(key, str) else None
        if policy is None:
            raise ProjectionViolation("a response body has a key no projection type classifies")
        projected = _project(policy, value, walk, key, 1)
        if projected is not OMITTED:
            out[key] = projected
    return Projection(out, walk.dropped, walk.shape_errors, walk.entries)
