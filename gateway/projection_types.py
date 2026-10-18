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
    ANY_SCALAR, ARGV, BOOL, CONFIGURED, ENV, HEADERS, HIDDEN, INLINE_SECRET, INT, JSON, NUM, OMIT, REF_MAP, SCALAR_SECRET, SHORT,
    STR, URL, URL_ENTRIES, Alt, Dict, Obj, Policy, Projection, Seq, mapping, project, register, seq,
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

PROFILE_APPLY = Obj("ProfileApply", {**_COMMANDS, "env": ENV, "kind": SHORT, "file": STR, "restore": SHORT}, extras=HIDDEN)

DRIVER_CONFIG = Obj("DriverConfig", {
    **_COMMANDS, "env": ENV, "profile_apply": PROFILE_APPLY,
    **_urls(record_carriers.URL_FIELD_PATHS, "lifecycle.driver_config"),
    # what the lifecycle drivers read (``gateway/lifecycle_drivers.py`` documents each driver's keys)
    **{key: ANY_SCALAR for key in (
        "container", "compose_file", "compose_service", "scope", "unit", "dropin", "swap_strategy", "health_timeout_s",
        "admin_header", "admin_key_env", "load_body_params", "precheck", "activation_requires_absolute_path", "load_timeout_s", "success_marker", "timeout_s", "cwd")},
    **{key: JSON for key in ("activation_env_keys", "activation_profiles")},
}, extras=HIDDEN)

LIFECYCLE = Obj("Lifecycle", {
    "type": SHORT, "managed": BOOL, "service_name": STR, "driver": SHORT, "driver_config": DRIVER_CONFIG,
    "admin_key": SCALAR_SECRET,
}, extras=HIDDEN)

PROCESS = Obj("Process", {
    **_COMMANDS, "driver": SHORT, "legacy_driver": SHORT, "needs_owner_conversion": BOOL, "profile_apply": PROFILE_APPLY,
    "port": JSON, "cwd": STR, "env": ENV, "ready_timeout_s": NUM, "stop_timeout_s": NUM, "restart": JSON,
    "log_max_bytes": INT, "legacy_lifecycle": LIFECYCLE,
    "health_endpoint": URL, "health_endpoint" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
}, extras=HIDDEN)

# What a record's ``config`` may show (decision D34). Production code reads exactly these keys of a service's config
# (``tests/test_config_keys_are_classified.py`` fails when it reads one that is not listed here); every other key is
# kept in storage unchanged and omitted from every management output, with a count of how many.
#
# Plain settings (a name, a number, a switch, an environment variable's *name*): public scalars.
CONFIG_SCALAR_KEYS = (
    "port", "base_path", "path_prefix", "scheme", "protocol", "native_passthrough", "definition", "definition_version",
    "timeout", "api_key_env", "api_key_ref", "credential_origin", "driver_credential_origin", "model_policy_schema_version", "name", "enabled", "role",
    "priority", "managed", "base_url_env", "api_base_env", "default_model", "activation_model", "model_id", "model",
    "execution_substrate", "paid_remote", "remote_provider", "preset_id", "provider_id", "provider_type", "cost_class",
    "cost_model", "litellm_model", "litellm_provider", "provider_adapter", "concurrency_limit", "id_param", "cost_path",
    "path", "config_path", "gateway_lifecycle_api_key_ref", "model_file_scope", "state_type", "kind", "description",
    "display_name", "note", "placement", "queue_name", "video_timeout", "video_workflows_dir", "workflows_dir",
    "vram_budget_key", "health_success_path", "configured", "missing", "builder", "chain_workflow", "save_node",
    "id", "max_app_concurrency", "max_seconds", "min_seconds", "chain_segment_seconds", "max_total_seconds", "control_after_generate",
)
# Descriptors an operator authors as data (a list of transform ids, an engine's check settings, a price table): they
# name paths, ids and numbers, never a credential, and are public by contract.
CONFIG_DESCRIPTOR_KEYS = (
    "capabilities", "engine_capabilities", "transforms", "engine_check", "engine_tests", "chunking", "aux", "offerings",
    "billing_dimensions", "spend_caps_usd", "price", "generation_stats", "model_listing", "auth_test", "protocols",
    "default_models", "protocol_operations", "control_file_roots", "loaded_model_identity", "gateway_lifecycle",
    "auth_inject", "recommendations", "connection", "workflows",
)

# A model row: its address is a URL like any other; the rest of the row is the model list's public data.
MODEL_ROW = Obj("ModelRow", {"url": URL, "url" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES}, extras=JSON)

# The record's operational blocks. Each names the fields production code reads; anything else is hidden.
RESOURCES = Obj("Resources", {**{key: ANY_SCALAR for key in ("gpu_required", "vram_gb_estimate", "duration_class", "max_parallel")}, "gpu": JSON},
                extras=HIDDEN)
_RESIDENCY_OP = Obj("ResidencyOperation", {"op": SHORT, "args": HIDDEN}, extras=HIDDEN)
RESIDENCY = Obj("Residency", {
    "load": _RESIDENCY_OP, "unload": _RESIDENCY_OP, "sleep": _RESIDENCY_OP, "wake": _RESIDENCY_OP,
    "load_deadline": Obj("LoadDeadline", {"stall_s": NUM, "max_s": NUM, "progress": SHORT}, extras=HIDDEN),
    "legacy_driver": SHORT, "needs_owner_conversion": BOOL, "idle_stop_s": NUM,
}, extras=HIDDEN)
PROVENANCE = Obj("Provenance", {key: SHORT for key in ("package_id", "package_version", "package_hash", "package", "source", "seed", "seed_hash")},
                 extras=HIDDEN)
NATIVE = Obj("NativePolicy", {
    "allow": seq(Obj("NativeAllow", {"method": SHORT, "path": STR, "class": SHORT, "query": seq(SHORT)}, extras=HIDDEN)),
    "case_insensitive": BOOL, "unlisted_reads": SHORT, "max_request_bytes": INT, "max_response_bytes": INT,
}, extras=HIDDEN)
PROVIDER_AUTH = Obj("ProviderAuth", {"header": SHORT, "prefix": SHORT}, extras=HIDDEN)

CONFIG = Obj("ServiceConfig", {
    "headers": HEADERS, "env": ENV, "process": PROCESS, "lifecycle": LIFECYCLE,
    "api_key_secret_id": CONFIGURED,
    **_urls(record_carriers.URL_FIELD_PATHS, "config"),
    **{key: ANY_SCALAR for key in CONFIG_SCALAR_KEYS},
    **{key: JSON for key in CONFIG_DESCRIPTOR_KEYS},
    "models": Alt((Seq(Alt((MODEL_ROW, ANY_SCALAR))), Dict(Alt((MODEL_ROW, ANY_SCALAR))))),
    "auth": PROVIDER_AUTH, "native": NATIVE, "resources": RESOURCES, "residency": Alt((ANY_SCALAR, RESIDENCY)),
    "context_window_probe": Obj("ContextWindowProbe", {
        "endpoint": URL, "endpoint" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES}, extras=HIDDEN),
}, extras=HIDDEN)

_HTTP = Obj("ObservationHttp", {
    "path": URL, "path" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
    "url": URL, "url" + record_carriers.SECRETS_SUFFIX: URL_ENTRIES,
    "expect": ANY_SCALAR, "timeout_s": NUM, "select": ANY_SCALAR,
}, extras=HIDDEN)
OBSERVATION = Obj("Observation", {"http": _HTTP, "credential_class": SHORT, "select": ANY_SCALAR, "means": SHORT}, extras=HIDDEN)

SERVICE_RECORD_FIELDS_PROJECTED = {
    "id": STR, "name": STR, "kind": SHORT, "source": SHORT, "enabled": BOOL, "priority": INT, "role": SHORT,
    "capabilities": seq(SHORT), "config": CONFIG, "secret_refs": REF_MAP, "lifecycle": LIFECYCLE, "provenance": PROVENANCE,
    "process": PROCESS, "observations": mapping(OBSERVATION), "residency": RESIDENCY, "resources": RESOURCES, "editable": BOOL,
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
    "repo": STR, "repository": STR,
    # one entry per field a caller put in the source that is not its own (a token, a header): only names and a flag show
    record_carriers.SOURCE_EXTRA_ENTRIES: mapping(SCALAR_SECRET),
}))
PACKAGE_RECORD = register(Obj("PackageRecord", {
    "id": STR, "name": STR, "version": STR, "enabled": BOOL, "source": PACKAGE_SOURCE, "package_hash": STR,
    "object_counts": JSON, "services": seq(STR), "protocol_frontends": seq(STR), "capabilities": seq(STR),
    "workflows": seq(STR), "adapters": seq(STR), "translators": seq(STR), "code_objects": JSON,
    "requires_code_trust": BOOL, "warnings": seq(STR), "updated_at": INT,
    # The install proofs are keyed digests (``mac``) of records, never records; the disable states are booleans.
    "installed_records": JSON, "installed_records_version": INT, "disabled_states": JSON,
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

OVERRIDES = register(Obj("Overrides", {
    "service_availability": JSON, "media_job_admission": JSON, "extension_package_index": PACKAGE_INDEX, "revisions": JSON,
}, extras=HIDDEN))

# --- the engine page ---------------------------------------------------------------------------------------------------------
# The engine view is assembled by ``engine_admin.engine_view`` from the projection of the record, so each nested part
# below is already a read of entries (never a value); it is carried here as public JSON, field by field.
ENGINE_VIEW = register(Obj("EngineView", {
    "service_id": STR, "definition": STR, "definition_version": ANY_SCALAR, "inferred": BOOL, "name": STR,
    "enabled": BOOL, "locked": BOOL, "editable": BOOL, "not_editable_reason": STR, "lifecycle_editable": BOOL,
    "values": JSON, "key": JSON, "capabilities": JSON, "models": JSON, "tests": JSON, "check": JSON, "calls": JSON,
    "other_settings": JSON, "hidden_settings": INT, "record": JSON, "v2": JSON, "access": JSON,
}))

# --- the effective configuration -------------------------------------------------------------------------------------------
# ``GET /admin/api/settings/effective`` shows the running configuration: the YAML the operator keeps, the OIDC
# settings, the registry and the overrides. A top-level section the type does not name is not shown, so a new
# section cannot appear in the view before someone says what it holds.

def _section(name: str, **fields: Policy) -> Obj:
    """A section of the YAML file the operator keeps: the keys it ships with are public; any other key is hidden (D34)."""
    return Obj(name, fields, extras=HIDDEN)


# A backend entry of the YAML file is a service's config, with the two inline secrets the file may hold.
BACKEND_VIEW = Obj("BackendView", {**CONFIG.fields, "api_key": INLINE_SECRET, "admin_key": INLINE_SECRET}, extras=HIDDEN)
EFFECTIVE_CONFIG = register(Obj("EffectiveConfig", {
    "proxy": _section("ProxySection", host=STR, port=INT),
    "llm": _section("LlmSection", pin_loaded_model=BOOL),
    "backends": mapping(BACKEND_VIEW),
    "images": _section("ImagesSection", cache_dir=STR, vram_estimates_file=STR, signed_url_ttl=INT, signed_url_secret=INLINE_SECRET,
                       vram_reserve_bytes=INT, free_vram_on_exhaustion=BOOL, max_batch_size=INT, background_removal=JSON,
                       variations=JSON, edits=JSON),
    # the keys of this section name engines, which the code does not list (docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md): its
    # values are paths, service ids and timeouts, public by contract
    "model_routing": JSON,
    "routing": _section("RoutingSection", default_backend=STR, audio_backend=STR),
    "identity": _section("IdentitySection", face_service=JSON),
    "reasoning": _section("ReasoningSection", default_effort=STR, effort_budgets=JSON),
    "roles": JSON, "models": JSON, "model_aliases": JSON,
    "videos": _section("VideosSection", default_workflow=STR, remix_workflow=STR, extend_workflow=STR, max_total_seconds=ANY_SCALAR,
                       allow_untracked_ids=BOOL, chain_segment_seconds=ANY_SCALAR, workflows=JSON, aliases=JSON),
    "audio": _section("AudioSection", default_workflow=STR, workflows_dir=STR, workflows=JSON, aliases=JSON),
    "voices": _section("VoicesSection", **{key: ANY_SCALAR for key in (
        "dir", "consent_mode", "max_voices_per_key", "max_consents_per_key", "max_bytes_per_key", "max_upload_bytes",
        "min_seconds", "max_seconds", "min_speech_seconds", "target_seconds", "quality_engine", "quality_warn_below",
        "design_engine")}),
    "model_compatibility": _section("ModelCompatibilitySection", enabled=BOOL, generated_aliases_file=STR,
                                    openai_models_api_url=URL, include_private=BOOL, defaults=JSON, aliases=JSON),
    "auth": _section("AuthSection", public_paths=JSON, trusted_proxies=JSON, setup_allowed_cidrs=JSON, cloudflare_mode=BOOL,
                     allow_query_param_key=BOOL),
    "admin": _section("AdminSection", db_path=STR, brand=JSON),
    "oidc": Obj("OidcSnapshot", {
        "client_secret": INLINE_SECRET, "client_secret_ref": OMIT, "source": SHORT, "apply_mode": SHORT, "enabled": BOOL,
        "client_id": STR, "client_secret_set": BOOL, "display_name": STR, "scopes": JSON, "clock_skew_seconds": ANY_SCALAR,
        "groups_claim": STR, "updated_at": ANY_SCALAR, "provenance": JSON,
        **{key: URL for key in ("issuer", "redirect_uri", "authorization_endpoint", "token_endpoint", "jwks_uri")},
    }, extras=HIDDEN),
    "service_registry": REGISTRY_DOC, "instance": JSON, "receipt_retention": JSON, "operator_overrides": OVERRIDES,
}))

# --- the keys of a management response body --------------------------------------------------------------------------------
# A route answers with a mapping; each key is a registered type. A key that is not here is a programming error:
# the projector refuses the whole body, so a new route cannot answer with a field nobody classified.
EXCLUDED_LIST = seq(EXCLUDED_ITEM)
AFFECTED_RECORD = Obj("AffectedRecord", {"kind": SHORT, "id": SHORT, "action": SHORT})
BODY_KEYS: dict[str, Policy] = {
    "service_registry": REGISTRY_DOC,
    "excluded": EXCLUDED_LIST,
    "extension_packages": PACKAGE_INDEX,
    "extension_package": PACKAGE_RECORD,
    "review": REVIEW,
    "litellm_config": LITELLM_PREVIEW,
    "provider": PROVIDER_VIEW,
    "providers": seq(PROVIDER_VIEW),
    "global": JSON,
    "key_status": PROVIDER_KEY_STATUS,
    "overrides": OVERRIDES,
    "operator_settings_generation": INT,
    "affected_records": seq(AFFECTED_RECORD),
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
    return Projection(out, walk.dropped, walk.shape_errors, walk.entries, walk.hidden)
