"""Read-only inventory of gateway configuration ownership.

The admin UI owns operator policy and runtime overrides. Bootstrap config,
secrets, and hackable workflow definitions stay in files, env, and manifests.
This module classifies those sources without importing proxy runtime code or
reading process environment.
"""

from __future__ import annotations

from gateway.compat import get_env

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


UI = "ui"
FILE = "file"
ENV = "env"
MANIFEST = "manifest"
MIXED = "mixed"

DASHBOARD_PLANE = "dashboard"
BOOTSTRAP_PLANE = "bootstrap"
PROVISIONED_PLANE = "provisioned"
SPLIT_PLANE = "split"

RUNTIME = "runtime"
RESTART = "restart"
READ_ONLY = "read_only"
ADVANCED = "advanced"

APPLY_LIVE = "live"
APPLY_RESTART = "restart"
APPLY_RELOAD_OR_RESTART = "reload_or_restart"
APPLY_READ_ONLY = "read_only"
APPLY_SPLIT = "split"

PROV_BUNDLE_CANDIDATE = "bundle_candidate"
PROV_BOOTSTRAP_ONLY = "bootstrap_only"
PROV_SOURCE_MANIFEST = "source_manifest"
PROV_SPLIT = "split"
PROV_SECRET_BOOTSTRAP = "secret_bootstrap"

_REDACTED = "<redacted>"
_MISSING = "<not set>"
_SECRET_WORDS = ("key", "secret", "token", "password", "credential", "client_secret")


@dataclass(frozen=True)
class ConfigSpec:
    path: tuple[str, ...]
    label: str
    domain: str
    ui_owner: str
    editability: str
    recommendation: str
    secret: bool = False
    source: str = "proxy_config.yml"


CONFIG_SPECS: tuple[ConfigSpec, ...] = (
    ConfigSpec(("proxy",), "Proxy server", "proxy/network", FILE, RESTART, "Keep bootstrap listener settings in YAML."),
    ConfigSpec(("server",), "Server network", "proxy/network", FILE, RESTART, "Keep bind and deployment shape in files."),
    ConfigSpec(("network",), "Network policy", "proxy/network", MIXED, RESTART, "Expose safe runtime overrides only when operators need them."),
    ConfigSpec(("backends",), "Service/provider registry seed", "services/providers", FILE, READ_ONLY, "Seed/provisioning input only; DB/UI service registry is the live operator surface."),
    ConfigSpec(("providers",), "Provider registry seed", "services/providers", FILE, READ_ONLY, "Seed/provisioning input only; DB/UI service registry is the live operator surface."),
    ConfigSpec(("models",), "Model catalog seed", "model routing", FILE, READ_ONLY, "Seed/provisioning input only; UI-managed targets and aliases live in admin.db."),
    ConfigSpec(("model_routing",), "Model routing bootstrap", "model routing", FILE, ADVANCED, "Keep model directories and compatibility sources in files; UI owns runtime target routing."),
    ConfigSpec(("routing",), "Routing policy", "model routing", UI, RUNTIME, "Good candidate for admin runtime policy controls."),
    ConfigSpec(("roles",), "Role policy", "roles/models", UI, RUNTIME, "UI should own operator-facing role access policy."),
    ConfigSpec(("role_models",), "Role model grants", "roles/models", UI, RUNTIME, "UI should own role-to-model runtime policy."),
    ConfigSpec(("images",), "Image generation/cache", "media jobs", UI, RUNTIME, "UI owns runtime admission and queue policy; files own only storage roots and signing-secret bootstrap."),
    ConfigSpec(("images", "signed_url_secret"), "Image URL signing secret", "media jobs", FILE, RESTART, "Keep URL signing secrets in env or file bootstrap; never edit them in the UI.", True),
    ConfigSpec(("image_cache",), "Image cache", "media jobs", MIXED, RUNTIME, "UI can own limits and cleanup policy; files own storage bootstrap."),
    ConfigSpec(("media_jobs",), "Media jobs", "media jobs", UI, RUNTIME, "Expose admission limits and queue controls in UI."),
    ConfigSpec(("resource_admission",), "Resource admission", "resource admission", UI, RUNTIME, "UI should own runtime caps and throttles."),
    ConfigSpec(("admission",), "Admission control", "resource admission", UI, RUNTIME, "UI should own runtime caps and throttles."),
    ConfigSpec(("video",), "Video generation", "workflow manifests", MANIFEST, READ_ONLY, "Keep workflow definitions in manifests; show status in UI."),
    ConfigSpec(("audio",), "Audio generation", "workflow manifests", MANIFEST, READ_ONLY, "Keep workflow definitions in manifests; show status in UI."),
    ConfigSpec(("workflows",), "Workflow config", "workflow manifests", MANIFEST, READ_ONLY, "Keep workflow definitions in manifests; show status in UI."),
    ConfigSpec(("model_compatibility",), "Model compatibility", "model compatibility", FILE, ADVANCED, "Keep compatibility tables in versioned files."),
    ConfigSpec(("compatibility",), "Compatibility rules", "model compatibility", FILE, ADVANCED, "Keep compatibility tables in versioned files."),
    ConfigSpec(("auth",), "Auth settings", "auth/admin", UI, RUNTIME, "UI owns admin policy; env/files own only trust roots and explicit secret bootstrap."),
    ConfigSpec(("oidc",), "OIDC settings", "auth/admin", UI, RUNTIME, "Dashboard-managed OIDC provider config; secret references may point at env or secret stores."),
    ConfigSpec(("admin",), "Admin settings", "auth/admin", UI, RUNTIME, "UI should own admin runtime policy backed by admin.db."),
    ConfigSpec(("admin", "user_defaults"), "User access defaults", "auth/admin", UI, RUNTIME, "Bootstrap defaults seed admin.db; manage ongoing identity/Profile mappings in the dashboard."),
)

ENV_RECOMMENDATIONS: Mapping[str, tuple[str, str, str]] = {
    "AZIZAM_USER_SELF_SERVICE": ("User access bootstrap environment", "auth/admin", "Bootstrap seed for admin.db user access defaults; manage runtime policy in the dashboard."),
    "AZIZAM_AUTO_CREATE_PERSONAL_APP": ("User access bootstrap environment", "auth/admin", "Bootstrap seed for admin.db user access defaults; manage runtime policy in the dashboard."),
    "AZIZAM_LITELLM": ("LiteLLM substrate environment", "services/providers", "Bootstrap sidecar URL and internal secret references; service records live in admin.db."),
    "OIDC": ("OIDC environment", "auth/admin", "Keep OIDC secrets and issuer bootstrap in env."),
    "OPENAI": ("OpenAI provider environment", "services/providers", "Keep provider credentials in env or secret stores."),
    "ANTHROPIC": ("Anthropic provider environment", "services/providers", "Keep provider credentials in env or secret stores."),
    "API_KEY": ("API key environment", "auth/admin", "Keep API keys outside runtime UI."),
    "SECRET": ("Secret environment", "auth/admin", "Keep secrets outside runtime UI."),
    "DATABASE": ("Database environment", "auth/admin", "Keep bootstrap storage wiring in env."),
}

KNOWN_ENV_SOURCE_NAMES: tuple[str, ...] = (
    "AZIZAM_ADMIN_DB",
    "AZIZAM_ADMIN_SESSION_KEY",
    "AZIZAM_ADMIN_OIDC_DISPLAY_NAME",
    "AZIZAM_AUTO_CREATE_PERSONAL_APP",
    "AZIZAM_LITELLM_API_KEY",
    "AZIZAM_LITELLM_BASE_URL",
    "AZIZAM_OIDC_CLIENT_SECRET",
    "AZIZAM_USER_SELF_SERVICE",
    "COMFY_ENABLED",
    "COMFY_HOST",
    "EMBEDDINGS_TIMEOUT",
    "IMAGE_MEDIA_JOB_RESUME_ON_START",
    "OPENAI_API_KEY",
    "OPENAI_MODEL_ALIASES_FILE",
    "OPENAI_MODEL_DISCOVERY_API_KEY",
    "OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE",
    "AZIZAM_AUTH_ENABLED",
    "AZIZAM_BASE_URL",
    "AZIZAM_MODEL_COMPAT_ENABLED",
    "AZIZAM_PORT",
    "AZIZAM_PUBLIC_URL",
    "UPSTREAM_READ_TIMEOUT",
)


def inventory_config_sources(
    proxy_config: Mapping[str, Any],
    *,
    env: Mapping[str, Any] | None = None,
    env_names: Sequence[str] | None = None,
    manifest_rows: Sequence[Mapping[str, Any]] | None = None,
    admin_db_summary: Mapping[str, Any] | None = None,
    admin_db_oidc: Mapping[str, Any] | None = None,
    admin_db_service_registry: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return JSON-able config source inventory rows sorted by stable id."""

    oidc_dashboard_managed = bool(admin_db_oidc)

    rows: list[dict[str, Any]] = []

    for spec in CONFIG_SPECS:
        value = _get_path(proxy_config, spec.path)
        if value is _MISSING:
            continue
        if spec.path == ("oidc",) and oidc_dashboard_managed:
            rows.append(_config_row(_dashboard_oidc_spec(spec), value))
            continue
        rows.append(_config_row(spec, value))

    rows.extend(_env_rows(env or {}, env_names or ()))
    rows.extend(_manifest_rows(manifest_rows or ()))

    if admin_db_summary:
        rows.append(
            _row(
                row_id="admin-db",
                label="Admin database",
                domain="auth/admin",
                source="admin.db",
                source_ref="admin.db",
                ui_owner=UI,
                editability=RUNTIME,
                secret=False,
                current_value_summary=_summarize(admin_db_summary),
                recommendation="Dashboard-managed runtime policy stored in admin.db with audit history.",
            )
        )

    if oidc_dashboard_managed:
        rows.append(
            _row(
                row_id="admin-db:oidc",
                label="OIDC provider config (admin.db)",
                domain="auth/admin",
                source="admin.db",
                source_ref="operator_settings.oidc_provider_config",
                ui_owner=UI,
                editability=RUNTIME,
                secret=False,
                current_value_summary=_summarize(admin_db_oidc),
                recommendation=(
                    "Manage OIDC provider in the admin dashboard. "
                    "client_secret is write-only and stored encrypted in "
                    "the admin.db secret store; the setting keeps only a "
                    "reference to it."
                ),
            )
        )

    if admin_db_service_registry:
        rows.append(
            _row(
                row_id="admin-db:service-registry",
                label="Service registry (admin.db)",
                domain="services/providers",
                source="admin.db",
                source_ref="operator_settings.service_registry",
                ui_owner=UI,
                editability=RUNTIME,
                secret=False,
                current_value_summary=_summarize(admin_db_service_registry),
                recommendation=(
                    "Dashboard-managed services, providers, protocol "
                    "frontends, and extension catalog records stored in "
                    "admin.db. proxy_config.yml and bundled registry files "
                    "only seed this registry."
                ),
            )
        )

    return sorted(rows, key=lambda row: row["id"])


def _dashboard_oidc_spec(spec: ConfigSpec) -> ConfigSpec:
    return ConfigSpec(
        path=spec.path,
        label=spec.label,
        domain=spec.domain,
        ui_owner=UI,
        editability=RUNTIME,
        recommendation=(
            "Dashboard-managed OIDC provider config overrides this YAML "
            "block; bootstrap values stay as a fallback when no admin.db "
            "setting is saved."
        ),
        secret=spec.secret,
        source=spec.source,
    )


def _config_row(spec: ConfigSpec, value: Any) -> dict[str, Any]:
    source_ref = ".".join(spec.path)
    is_secret = spec.secret or _looks_secret(source_ref)
    return _row(
        row_id=f"config:{source_ref}",
        label=spec.label,
        domain=spec.domain,
        source=spec.source,
        source_ref=source_ref,
        ui_owner=spec.ui_owner,
        editability=spec.editability,
        secret=is_secret,
        current_value_summary=_summarize(value, secret=is_secret),
        recommendation=spec.recommendation,
    )


def _env_rows(env: Mapping[str, Any], env_names: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in sorted(dict.fromkeys(env_names)):
        value = get_env(name, _MISSING, environ=env)
        is_secret = _looks_secret_env(name, value)
        label, domain, recommendation = _env_metadata(name)
        rows.append(
            _row(
                row_id=f"env:{name}",
                label=label,
                domain=domain,
                source=ENV,
                source_ref=name,
                ui_owner=ENV,
                editability=RESTART,
                secret=is_secret,
                current_value_summary=_summarize(value, secret=is_secret),
                recommendation=recommendation,
            )
        )
    return rows


def _manifest_rows(manifest_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, manifest in enumerate(manifest_rows):
        manifest_id = str(manifest.get("id") or manifest.get("name") or f"manifest-{index + 1}")
        kind = str(manifest.get("kind") or manifest.get("type") or "workflow")
        rows.append(
            _row(
                row_id=f"manifest:{manifest_id}",
                label=str(manifest.get("label") or manifest_id),
                domain=_manifest_domain(kind, manifest_id),
                source=MANIFEST,
                source_ref=str(manifest.get("source_ref") or manifest.get("path") or manifest_id),
                ui_owner=MANIFEST,
                editability=READ_ONLY,
                secret=False,
                current_value_summary=_summarize(manifest),
                recommendation="Manifest/catalog-owned workflow definition; dashboard shows live inventory and health only.",
            )
        )
    return rows


def _row(
    *,
    row_id: str,
    label: str,
    domain: str,
    source: str,
    source_ref: str,
    ui_owner: str,
    editability: str,
    secret: bool,
    current_value_summary: str,
    recommendation: str,
) -> dict[str, Any]:
    plane = _plane_for(ui_owner)
    lifecycle = _lifecycle_for(
        ui_owner=ui_owner,
        editability=editability,
        source=source,
        secret=secret,
    )
    return {
        "id": row_id,
        "label": label,
        "domain": domain,
        "source": source,
        "source_ref": source_ref,
        "ui_owner": ui_owner,
        "editability": editability,
        "plane": plane,
        "plane_label": _plane_label(plane),
        "change_path": _change_path_for(
            ui_owner=ui_owner,
            editability=editability,
            source=source,
            source_ref=source_ref,
        ),
        "restart_required": editability == RESTART,
        "secret": secret,
        "current_value_summary": current_value_summary,
        "recommendation": recommendation,
        "authored_via": lifecycle["authored_via"],
        "stored_in": lifecycle["stored_in"],
        "writable": lifecycle["writable"],
        "provisioning": lifecycle["provisioning"],
        "apply_mode": lifecycle["apply_mode"],
    }


def _lifecycle_for(
    *,
    ui_owner: str,
    editability: str,
    source: str,
    secret: bool,
) -> dict[str, Any]:
    """Map (ui_owner, editability, source) to explicit lifecycle facts.

    The five facts answer: how is the setting authored, where does it live,
    can the dashboard/API write it, does it belong in a provisioning bundle,
    and how does a change take effect.
    """

    if ui_owner == UI:
        stored_in = "admin.db"
        if source != "admin.db":
            stored_in = f"admin.db plus {source} defaults"
        return {
            "authored_via": "Dashboard/API",
            "stored_in": stored_in,
            "writable": {"dashboard": True, "api": True},
            "provisioning": PROV_BUNDLE_CANDIDATE,
            "apply_mode": APPLY_LIVE if editability != RESTART else APPLY_RESTART,
        }
    if ui_owner == ENV:
        return {
            "authored_via": "Service environment",
            "stored_in": "service environment",
            "writable": {"dashboard": False, "api": False},
            "provisioning": PROV_SECRET_BOOTSTRAP if secret else PROV_BOOTSTRAP_ONLY,
            "apply_mode": APPLY_RESTART,
        }
    if ui_owner == FILE:
        apply_mode = APPLY_RESTART if editability == RESTART else APPLY_RELOAD_OR_RESTART
        if editability == READ_ONLY:
            apply_mode = APPLY_READ_ONLY
        return {
            "authored_via": source,
            "stored_in": source,
            "writable": {"dashboard": False, "api": False},
            "provisioning": PROV_SECRET_BOOTSTRAP if secret else PROV_BOOTSTRAP_ONLY,
            "apply_mode": apply_mode,
        }
    if ui_owner == MANIFEST:
        manifest_source = "workflow manifest"
        if source != MANIFEST:
            manifest_source = f"workflow manifest plus {source} pointer"
        return {
            "authored_via": "Workflow manifest",
            "stored_in": manifest_source,
            "writable": {"dashboard": False, "api": False},
            "provisioning": PROV_SOURCE_MANIFEST,
            "apply_mode": APPLY_READ_ONLY,
        }
    # MIXED / split ownership
    return {
        "authored_via": f"Dashboard plus {source}",
        "stored_in": f"admin.db plus {source}",
        "writable": {"dashboard": True, "api": True},
        "provisioning": PROV_SPLIT,
        "apply_mode": APPLY_SPLIT,
    }


def _plane_for(ui_owner: str) -> str:
    if ui_owner == UI:
        return DASHBOARD_PLANE
    if ui_owner in {FILE, ENV}:
        return BOOTSTRAP_PLANE
    if ui_owner == MANIFEST:
        return PROVISIONED_PLANE
    return SPLIT_PLANE


def _plane_label(plane: str) -> str:
    return {
        DASHBOARD_PLANE: "Managed here",
        BOOTSTRAP_PLANE: "Bootstrap config",
        PROVISIONED_PLANE: "Provisioned artifacts",
        SPLIT_PLANE: "Split ownership",
    }.get(plane, "Other")


def _change_path_for(
    *,
    ui_owner: str,
    editability: str,
    source: str,
    source_ref: str,
) -> str:
    if ui_owner == UI:
        return "Manage in this dashboard."
    if ui_owner == ENV:
        return f"Set {source_ref} in the service environment and restart."
    if ui_owner == FILE:
        if editability == RESTART:
            return f"Edit {source} field {source_ref} and restart."
        return f"Edit {source} field {source_ref}; reload or restart as documented."
    if ui_owner == MANIFEST:
        return f"Edit the manifest source {source_ref}; reload or restart if the gateway does not pick it up automatically."
    return "Use dashboard controls for runtime overrides; edit file/env-owned bootstrap fields outside the dashboard."


def _get_path(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _summarize(value: Any, *, secret: bool = False) -> str:
    if value is _MISSING:
        return _MISSING
    if secret:
        return _REDACTED if value else "<empty>"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= 80 else f"{value[:77]}..."
    if isinstance(value, Mapping):
        keys = sorted(str(key) for key in value.keys())
        preview = ", ".join(keys[:6])
        suffix = "" if len(keys) <= 6 else f", +{len(keys) - 6} more"
        return f"{len(keys)} keys: {preview}{suffix}" if keys else "0 keys"
    if isinstance(value, Sequence):
        return f"{len(value)} items"
    return str(value)


def _looks_secret(name: str) -> bool:
    normalized = name.lower()
    return any(word in normalized for word in _SECRET_WORDS)


def _looks_secret_env(name: str, value: Any) -> bool:
    normalized = name.upper()
    if _looks_secret(name):
        return True
    if normalized.endswith("DATABASE_URL") or normalized in {"DATABASE_URL"}:
        return True
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return bool(parsed.scheme and parsed.username)
    return False


def _env_metadata(name: str) -> tuple[str, str, str]:
    upper_name = name.upper()
    for marker, metadata in ENV_RECOMMENDATIONS.items():
        if marker in upper_name:
            label, domain, recommendation = metadata
            return f"{label}: {name}", domain, recommendation
    return f"Environment: {name}", "proxy/network", "Keep deployment bootstrap values in env."


def _manifest_domain(kind: str, manifest_id: str) -> str:
    text = f"{kind} {manifest_id}".lower()
    if "video" in text:
        return "video workflows"
    if "audio" in text or "tts" in text or "stt" in text:
        return "audio workflows"
    return "workflow manifests"
