"""Add engine routes: definitions, check, draft test, workflow read, engine view, create and edit.

The dialog, a script and an MCP tool use the same routes. The check and the
gate run on the server: a create or an edit that changes the connection re-runs
the check and refuses on an error, or on a warning the caller did not
acknowledge, unless the engine is saved turned off.

Keys: an owner pastes a key and it goes into the provider secret store, bound
to the address it was entered for. It is write-only: the record holds a secret
id, the view says "configured", and ``SECRET_MASK`` in a draft means "keep the
stored key". A stored key is never sent to an address other than the one it
was stored for. Administrators may add keyless engines; attaching a key,
reading an environment variable and lifecycle commands stay with the owner
(r47).
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway import secret_store, service_registry, transforms
from gateway.engine_check import (
    SECRET_MASK,
    DraftError,
    Fetch,
    KeyInput,
    call_line,
    comparable_address,
    duplicate_finding,
    make_http_fetch,
    normalize_address,
    origin_of,
    read_workflow,
    run_check,
    run_real_test,
)
from gateway.engine_definitions import EngineCatalog, EngineDefinitionError, load_engine_catalog

# Record keys this module writes. Every other config key is the owner's and is
# carried through an edit untouched ("Other settings").
_KNOWN_CONFIG_KEYS = frozenset({
    "host", "port", "base_path", "scheme", "protocol", "native_passthrough", "transforms", "models",
    "definition", "definition_version", "address", "route_path", "path_prefix", "auth_header",
    "connect_timeout_s", "timeout", "headers", "api_key_secret_id", "api_key_env", "credential_origin",
    "engine_check", "engine_tests", "voices", "default_voice", "workflows", "model_policy_schema_version",
    "name", "enabled", "capabilities", "role", "priority", "managed", "lifecycle",
})
_RECORD_KEYS = frozenset({"id", "name", "kind", "source", "enabled", "priority", "role", "capabilities", "config",
                          "secret_refs", "lifecycle", "provenance", "editable"})
_LIFECYCLE_FIELDS = ("lifecycle_mode", "lifecycle_start", "lifecycle_stop", "lifecycle_ready_s", "lifecycle_idle_min")
_TRANSFORM_KINDS = frozenset({"value-map", "drop-params", "default-params", "param-map", "message-inject", "clamp"})


class EngineRouteError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, field: str | None = None, check: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.check = check

    def as_response(self) -> JSONResponse:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            error["field"] = self.field
        content: dict[str, Any] = {"error": error}
        if self.check is not None:
            content["check"] = dict(self.check)
        return JSONResponse(status_code=self.status_code, content=content)


def _is_owner(ctx: Any) -> bool:
    return "owner" in getattr(ctx, "roles", ())


def _owner_only(message: str) -> EngineRouteError:
    return EngineRouteError(status.HTTP_403_FORBIDDEN, "owner_required", message)


def _definition(catalog: EngineCatalog, definition_id: Any) -> dict[str, Any]:
    definition = catalog.definition(definition_id)
    if definition is None:
        raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "unknown_definition",
                               f"There is no kind of server called {definition_id!r}.", field="definition")
    return definition


# ---------------------------------------------------------------------------
# Values: validation and the record they become
# ---------------------------------------------------------------------------


def _clean_transforms(value: Any) -> list[Any]:
    """Check each request adjustment against the six primitives. Named built-ins pass by name."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise DraftError("Request adjustments must be a list.", field="transforms")
    out: list[Any] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
            continue
        if not isinstance(item, Mapping) or item.get("kind") not in _TRANSFORM_KINDS:
            raise DraftError(f"Adjustment {index + 1} is not one of the six kinds Azizam knows.", field="transforms")
        try:
            transforms._build_primitive(dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            raise DraftError(f"Adjustment {index + 1} ({item.get('kind')}) is missing a value: {exc}.", field="transforms") from None
        if item.get("phase", "request") not in ("request", "response"):
            raise DraftError(f"Adjustment {index + 1} must apply to requests or replies.", field="transforms")
        out.append(copy.deepcopy(dict(item)))
    return out


def _clean_headers(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise DraftError("Request headers must be a list of name and value rows.", field="headers")
    out = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise DraftError(f"Header {index + 1} needs a name and a value.", field="headers")
        name = str(row.get("name") or "").strip()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise DraftError(f"Header {index + 1}: “{name}” is not a header name.", field="headers")
        if name.lower() in ("host", "content-length", "transfer-encoding", "connection"):
            raise DraftError(f"Header {index + 1}: Azizam sets “{name}” itself.", field="headers")
        value_text = str(row.get("value") or "")
        if any(ch in value_text for ch in "\r\n") or not value_text.isascii():
            raise DraftError(f"Header {index + 1}: a value can hold plain letters, digits and signs only, no line break.", field="headers")
        out.append({"name": name, "value": value_text, "secret": bool(row.get("secret"))})
    return out


def _number(values: Mapping[str, Any], key: str, default: float, *, low: float, high: float) -> float:
    raw = values.get(key)
    if raw in (None, ""):
        return default
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise DraftError(f"{key.replace('_', ' ')} must be a number.", field=key) from None
    if not low <= number <= high:
        raise DraftError(f"{key.replace('_', ' ')} must be between {low:g} and {high:g}.", field=key)
    return number


def _field_default(definition: Mapping[str, Any], key: str, fallback: Any = None) -> Any:
    for field in definition["fields"]:
        if field["key"] == key:
            return copy.deepcopy(field.get("default", fallback))
    return fallback


def _split_address(address: str, definition: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    from urllib.parse import urlsplit

    parts = urlsplit(address)
    out: dict[str, Any] = {"host": parts.hostname, "port": parts.port or (443 if parts.scheme == "https" else 80)}
    if parts.scheme == "https":
        out["scheme"] = "https"
    declared_base = definition["record"]["base_path"]
    path = (parts.path or "").rstrip("/")
    base = path if declared_base == "from_address" else declared_base
    if prefix:
        base = base.rstrip("/") + "/" + prefix.strip("/")
    out["base_path"] = base
    return out


def lifecycle_block(values: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record's lifecycle block from the typed Start and stop fields; ``None`` for "leave it running"."""
    mode = str(values.get("lifecycle_mode") or _field_default(definition, "lifecycle_mode", "external"))
    if mode != "managed":
        return None
    start = str(values.get("lifecycle_start") or "").strip()
    if not start:
        raise DraftError("Type the command that starts the server.", field="lifecycle_start")
    driver_config: dict[str, Any] = {"start": start}
    stop = str(values.get("lifecycle_stop") or "").strip()
    if stop:
        driver_config["stop"] = stop
    driver_config["health_timeout_s"] = _number(values, "lifecycle_ready_s", 120, low=1, high=3600)
    idle = _number(values, "lifecycle_idle_min", 0, low=0, high=10080)
    if idle:
        driver_config["idle_stop_minutes"] = idle
    return {"type": "command", "managed": True, "driver": "command", "driver_config": driver_config}


def clean_values(definition: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate draft values against the definition's field list."""
    if not isinstance(values, Mapping):
        raise DraftError("values must be an object.", field="values")
    fields = {f["key"]: f for f in definition["fields"]}
    unknown = sorted(set(values) - set(fields) - {"workflow_graph", "workflow_file", "workflow_inputs", "workflow_makes"})
    if unknown:
        raise DraftError(f"This kind of server has no field called {unknown[0]!r}.", field=unknown[0])
    out = {key: copy.deepcopy(value) for key, value in values.items()}
    out["address"] = normalize_address(values.get("address"), placeholder=fields["address"].get("placeholder", ""))
    if "path" in fields:
        path = str(values.get("path") or "").strip()
        if not path.startswith("/") or any(ch in path for ch in "?# \\"):
            raise DraftError(f"Type the path the server uses, starting with /, for example {fields['path'].get('placeholder')}.", field="path")
        out["path"] = path
    prefix = str(values.get("path_prefix") or "").strip()
    if prefix and (not prefix.startswith("/") or any(ch in prefix for ch in "?# \\")):
        raise DraftError("The extra path starts with /, for example /openai.", field="path_prefix")
    out["path_prefix"] = prefix.rstrip("/")
    env = str(values.get("api_key_env") or "").strip()
    if env and not service_registry.ENV_VAR_NAME_RE.fullmatch(env):
        raise DraftError("An environment variable name holds letters, digits and _ only, for example LAN_SERVER_KEY.", field="api_key_env")
    out["api_key_env"] = env
    key = str(values.get("api_key") or "")
    if key and key != SECRET_MASK and (not key.isascii() or any(ch.isspace() for ch in key.strip())):
        raise DraftError("The key holds a space or a character a request cannot carry. Paste it again.", field="api_key")
    out["headers"] = _clean_headers(values.get("headers"))
    out["transforms"] = _clean_transforms(values["transforms"]) if "transforms" in values else None
    auth_header = str(values.get("auth_header") or "Authorization").strip()
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", auth_header):
        raise DraftError("The key header name holds letters, digits and - only, for example X-API-Key.", field="auth_header")
    out["auth_header"] = auth_header
    out["connect_timeout_s"] = _number(values, "connect_timeout_s", _field_default(definition, "connect_timeout_s", 5), low=1, high=60)
    out["reply_timeout_s"] = _number(values, "reply_timeout_s", _field_default(definition, "reply_timeout_s", 120), low=1, high=3600)
    return out


def _existing_engines(registry: Mapping[str, Any], *, exclude: str | None = None) -> list[dict[str, Any]]:
    out = []
    for sid, record in (registry.get("services") or {}).items():
        if sid == exclude:
            continue
        config = record.get("config") or {}
        address = config.get("address") or service_registry.service_base_url(config)
        if address:
            out.append({"id": sid, "name": record.get("name") or sid, "address": address, "path": config.get("route_path") or ""})
    return out


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _stored_key(record: Mapping[str, Any] | None, *, db_path: Path, env: Callable[[str], str | None]) -> tuple[str | None, str, str]:
    """``(value, source, origin)`` of the key a record already holds."""
    if not record:
        return None, "none", ""
    config = record.get("config") or {}
    origin = str(config.get("credential_origin") or "")
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        try:
            return secret_store.get_provider_secret(secret_id, db_path=db_path, expected_origin=origin or None), "stored", origin
        except Exception:  # noqa: BLE001 - an unreadable secret is "no key", never a crash
            return None, "stored", origin
    name = str(config.get("api_key_env") or "").strip()
    if name:
        return env(name), "env", origin
    return None, "none", origin


def resolve_key(values: Mapping[str, Any], ctx: Any, *, record: Mapping[str, Any] | None, db_path: Path,
                env: Callable[[str], str | None]) -> KeyInput:
    """The key a check or test may send to the draft address."""
    pasted = str(values.get("api_key") or "")
    if pasted and pasted != SECRET_MASK:
        if not _is_owner(ctx):
            raise _owner_only("Only the owner can add a key.")
        return KeyInput(pasted.strip(), "pasted")
    stored_config = (record or {}).get("config") or {}
    env_name = str(values.get("api_key_env") or "").strip()
    if env_name and env_name != str(stored_config.get("api_key_env") or ""):
        if not _is_owner(ctx):
            raise _owner_only("Only the owner can read a key from a server environment variable.")
        value = env(env_name)
        if not value:
            raise DraftError(f"The variable {env_name} is not set on the gateway’s computer.", field="api_key_env")
        return KeyInput(value, "env")
    value, source, origin = _stored_key(record, db_path=db_path, env=env)
    if source == "none":
        return KeyInput()
    if not origin or origin != origin_of(str(values.get("address") or "")):
        return KeyInput(None, "none", withheld=True)
    return KeyInput(value, source) if value else KeyInput()


# ---------------------------------------------------------------------------
# Building the record
# ---------------------------------------------------------------------------


def _models_config(definition: Mapping[str, Any], values: Mapping[str, Any], models: Any,
                   stored: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Model rows for the record. A row the record already holds keeps every key this form does not edit."""
    stored_rows = [dict(row) for row in stored if isinstance(row, Mapping)]

    def stored_row(upstream: str, public: str) -> dict[str, Any]:
        for row in stored_rows:
            if row.get("upstream_model") == upstream or row.get("model_group") == public:
                return copy.deepcopy(row)
        return {}

    if definition["flow"] == "declared":
        public = str(values.get("public_name") or _field_default(definition, "public_name", "")).strip()
        if not public:
            raise DraftError("Give the model the public name apps use to pick it.", field="public_name")
        upstream = str(values.get("server_model") or "").strip() or public
        first = copy.deepcopy(stored_rows[0]) if stored_rows else {}
        first.pop("id", None)
        return [{**first, "model_group": public, "upstream_model": upstream}, *copy.deepcopy(stored_rows[1:])]
    if definition["flow"] == "workflow":
        public = str(values.get("workflow_public_name") or "").strip()
        if stored_rows and not isinstance(values.get("workflow_graph"), Mapping):
            # No new workflow: the stored rows stay; only the name apps use can change.
            rows = copy.deepcopy(stored_rows)
            if public and len(rows) == 1:
                rows[0].pop("id", None)
                rows[0]["model_group"] = public
            return rows
        if not public:
            raise DraftError("Give the workflow the name apps use.", field="workflow_public_name")
        return [{"model_group": public, "upstream_model": public}]
    out = []
    taken: set[str] = set()
    for index, row in enumerate(models or ()):
        if not isinstance(row, Mapping):
            raise DraftError(f"Model {index + 1} must be an object.", field="models")
        upstream = str(row.get("id") or row.get("upstream_model") or "").strip()
        public = str(row.get("public_name") or row.get("model_group") or upstream).strip()
        if not upstream:
            raise DraftError(f"Model {index + 1} needs the name the server uses.", field="models")
        if public in taken:
            raise DraftError(f"Two models use the public name “{public}”. Each needs its own.", field="models")
        taken.add(public)
        item = stored_row(upstream, public)
        item.pop("id", None)
        item.update(model_group=public, upstream_model=upstream)
        window = row.get("context_window")
        if window not in (None, ""):
            try:
                item["context_window"] = int(window)
            except (TypeError, ValueError):
                raise DraftError(f"The context window of “{public}” must be a whole number.", field="models") from None
        else:
            item.pop("context_window", None)
        if row.get("enabled") is False:
            item["enabled"] = False
        else:
            item.pop("enabled", None)
        out.append(item)
    return out


def _workflow_config(definition: Mapping[str, Any], values: Mapping[str, Any]) -> list[dict[str, Any]]:
    graph = values.get("workflow_graph")
    if not isinstance(graph, Mapping):
        return []
    inputs = values.get("workflow_inputs") or {}
    read = read_workflow(definition, __import__("json").dumps(graph), str(values.get("workflow_file") or "workflow.json"))
    if not read["ok"]:
        raise DraftError("The workflow is not in API format.", field="workflow")
    options = {o["value"] for row in read["inputs"] for o in row["options"]}
    clean_inputs = {}
    for key, target in (inputs.items() if isinstance(inputs, Mapping) else ()):
        if target in (None, ""):
            continue
        if target not in options:
            raise DraftError(f"The workflow has no field {target!r} for {key}.", field="workflow_inputs")
        clean_inputs[str(key)] = str(target)
    for row in read["inputs"]:
        if row["required"] and row["key"] not in clean_inputs:
            raise DraftError("Azizam needs to know where the prompt goes. Choose the field that holds the prompt text.", field="workflow_inputs")
    return [{
        "public_name": str(values.get("workflow_public_name") or "").strip(),
        "file": read["file"],
        "makes": values.get("workflow_makes") if values.get("workflow_makes") in ("images", "video") else read["makes"],
        "inputs": clean_inputs,
        "graph": copy.deepcopy(dict(graph)),
    }]


# The config keys each optional field owns. A definition without the field leaves them as stored.
_FIELD_CONFIG_KEYS = {
    "path_prefix": ("path_prefix",),
    "auth_header": ("auth_header",),
    "transforms": ("transforms",),
    "headers": ("headers",),
    "connect_timeout_s": ("connect_timeout_s",),
    "reply_timeout_s": ("timeout",),
    "voices": ("voices",),
    "default_voice": ("default_voice",),
    "path": ("route_path",),
}


def capability_words(catalog: EngineCatalog, definition: Mapping[str, Any]) -> set[str]:
    return {word for cap_id in definition["capabilities"] for word in catalog.capability(cap_id)["record_capabilities"]}


def build_raw(
    catalog: EngineCatalog,
    definition: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    name: str,
    capabilities: Iterable[str],
    models: Any,
    enabled: bool,
    check: Mapping[str, Any],
    tests: Mapping[str, Any] | None,
    secret_id: str | None,
    header_secret_ids: Mapping[str, str],
    keep: Mapping[str, Any] | None = None,
    stored_models: Iterable[Any] = (),
    extra_capabilities: Iterable[str] = (),
    record_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """The raw service record for ``service_from_backend_config``. ``keep`` holds config keys to carry over."""
    caps = list(dict.fromkeys(capabilities))
    for cap_id in caps:
        if cap_id not in definition["capabilities"]:
            raise DraftError(f"This kind of server does not offer {cap_id!r}.", field="capabilities")
    record_caps: list[str] = []
    for cap_id in caps:
        for value in catalog.capability(cap_id)["record_capabilities"]:
            if value not in record_caps:
                record_caps.append(value)
    record_caps += [value for value in extra_capabilities if value not in record_caps]
    if record_capabilities is not None:
        record_caps = list(record_capabilities)
    has = {f["key"] for f in definition["fields"]}
    raw: dict[str, Any] = {key: copy.deepcopy(value) for key, value in (keep or {}).items()}
    raw.update(_split_address(values["address"], definition, (values.get("path_prefix") or "") if "path_prefix" in has else ""))
    raw.update({
        "name": name,
        "enabled": enabled,
        "capabilities": record_caps,
        "protocol": definition["record"]["protocol"],
        "native_passthrough": definition["record"]["native_passthrough"],
        "definition": definition["definition"],
        "definition_version": definition["definition_version"],
        "address": values["address"],
        "engine_check": dict(check),
        "engine_capabilities": caps,
    })
    if "path" in has:
        raw["route_path"] = values["path"]
    if "transforms" in has:
        transforms_value = values.get("transforms")
        raw["transforms"] = transforms_value if transforms_value is not None else list(definition["record"].get("transforms") or [])
        if not raw["transforms"]:
            raw.pop("transforms")
    for key, default in (("path_prefix", ""), ("auth_header", "Authorization")):
        if key not in has:
            continue
        if values.get(key) and values.get(key) != default:
            raw[key] = values[key]
        else:
            raw.pop(key, None)
    if "reply_timeout_s" in has:
        raw["timeout"] = int(values["reply_timeout_s"])
    if "connect_timeout_s" in has:
        raw["connect_timeout_s"] = values["connect_timeout_s"]
    if "headers" in has:
        headers = []
        for row in values.get("headers") or ():
            if row["secret"]:
                if row["name"] in header_secret_ids:
                    headers.append({"name": row["name"], "value_secret_id": header_secret_ids[row["name"]]})
            else:
                headers.append({"name": row["name"], "value": row["value"]})
        if headers:
            raw["headers"] = headers
        else:
            raw.pop("headers", None)
    models_config = _models_config(definition, values, models, stored_models)
    if models_config:
        raw["models"] = models_config
        raw.setdefault("model_policy_schema_version", service_registry.MODEL_POLICY_SCHEMA_VERSION)
    else:
        raw.pop("models", None)
    if definition["flow"] == "workflow":
        workflows = _workflow_config(definition, values)
        if workflows:
            raw["workflows"] = workflows
    if "voices" in has:
        voices = [str(v).strip() for v in values.get("voices") or () if str(v).strip()]
        raw["voices"] = voices
        default_voice = str(values.get("default_voice") or "").strip()
        if default_voice and default_voice not in voices:
            raise DraftError("Pick a default voice from the list.", field="default_voice")
        if default_voice:
            raw["default_voice"] = default_voice
        else:
            raw.pop("default_voice", None)
    if tests:
        raw["engine_tests"] = dict(tests)
    else:
        raw.pop("engine_tests", None)
    raw.pop("api_key_secret_id", None)
    raw.pop("api_key_env", None)
    if secret_id:
        raw["api_key_secret_id"] = secret_id
    elif values.get("api_key_env"):
        raw["api_key_env"] = values["api_key_env"]
    lifecycle = lifecycle_block(values, definition)
    if lifecycle is not None:
        raw["lifecycle"] = lifecycle
    else:
        raw.pop("lifecycle", None)
    return raw


def _clean_tests(definition: Mapping[str, Any], tests: Any) -> dict[str, Any]:
    """Real-test results the dialog shows; stored as data on the record (time, outcome, how it ran)."""
    out: dict[str, Any] = {}
    if not isinstance(tests, Mapping):
        return out
    for cap_id, row in tests.items():
        if cap_id in definition["capabilities"] and isinstance(row, Mapping):
            via = row.get("via") if row.get("via") in ("draft", "saved") else "draft"
            item = {"ok": bool(row.get("ok")), "at": int(row.get("at") or time.time()), "via": via}
            if isinstance(row.get("seconds"), (int, float)):
                item["seconds"] = float(row["seconds"])
            out[cap_id] = item
    return out


# ---------------------------------------------------------------------------
# The engine view (what the record page reads)
# ---------------------------------------------------------------------------


def infer_definition(catalog: EngineCatalog, record: Mapping[str, Any]) -> str:
    """The definition a record was saved under; for an older record, the first whose protocol it shares."""
    config = record.get("config") or {}
    if config.get("definition") and catalog.definition(config["definition"]) is not None:
        return str(config["definition"])
    protocol = str(config.get("protocol") or "")
    for row in catalog.data["definitions"]:
        if protocol and row["record"]["protocol"] == protocol:
            return row["definition"]
    return catalog.data["definitions"][0]["definition"]


def engine_view(catalog: EngineCatalog, record: Mapping[str, Any], *, db_path: Path) -> dict[str, Any]:
    config = record.get("config") or {}
    definition = catalog.definition(infer_definition(catalog, record))
    assert definition is not None
    address = str(config.get("address") or service_registry.service_base_url(config) or "")
    values: dict[str, Any] = {"address": address}
    if definition["flow"] == "declared":
        values["path"] = config.get("route_path") or ""
    values["api_key_env"] = config.get("api_key_env") or ""
    values["path_prefix"] = config.get("path_prefix") or ""
    values["auth_header"] = config.get("auth_header") or "Authorization"
    values["connect_timeout_s"] = config.get("connect_timeout_s") or _field_default(definition, "connect_timeout_s", 5)
    values["reply_timeout_s"] = config.get("timeout") or _field_default(definition, "reply_timeout_s", 120)
    values["headers"] = [
        {"name": h.get("name"), "value": SECRET_MASK if h.get("value_secret_id") else h.get("value", ""), "secret": bool(h.get("value_secret_id"))}
        for h in config.get("headers") or () if isinstance(h, Mapping)
    ]
    values["transforms"] = copy.deepcopy(config.get("transforms") or [])
    lifecycle = record.get("lifecycle") or {}
    driver_config = lifecycle.get("driver_config") or {}
    if lifecycle.get("driver") == "command":
        values.update(lifecycle_mode="managed", lifecycle_start=driver_config.get("start", ""), lifecycle_stop=driver_config.get("stop", ""),
                      lifecycle_ready_s=driver_config.get("health_timeout_s", 120), lifecycle_idle_min=driver_config.get("idle_stop_minutes", 0))
    else:
        values["lifecycle_mode"] = "external"
    lifecycle_editable = lifecycle.get("driver") in (None, "", "command", "external")
    model_use = (config.get("engine_check") or {}).get("model_use") or {}
    models = [{"id": m.get("upstream_model"), "public_name": m.get("model_group") or m.get("id"),
               "context_window": m.get("context_window"), "enabled": m.get("enabled", True),
               "use": model_use.get(m.get("upstream_model"), "")}
              for m in config.get("models") or () if isinstance(m, Mapping)]
    if definition["flow"] == "declared" and models:
        values["public_name"] = models[0]["public_name"]
        values["server_model"] = models[0]["id"] if models[0]["id"] != models[0]["public_name"] else ""
        values["voices"] = list(config.get("voices") or [])
        values["default_voice"] = config.get("default_voice") or ""
    workflow = None
    if definition["flow"] == "workflow" and config.get("workflows"):
        first = config["workflows"][0]
        values["workflow_public_name"] = first.get("public_name", "")
        workflow = {key: first.get(key) for key in ("public_name", "file", "makes", "inputs")}
        workflow["node_count"] = len([k for k in (first.get("graph") or {}) if not str(k).startswith("_")])
    secret_id = str(config.get("api_key_secret_id") or "")
    key: dict[str, Any] = {"state": "none"}
    if secret_id:
        key = {"state": "configured", "source": "stored", "origin": config.get("credential_origin") or ""}
    elif config.get("api_key_env"):
        key = {"state": "configured", "source": "env", "env_name": config["api_key_env"], "origin": config.get("credential_origin") or ""}
    caps = config.get("engine_capabilities")
    if not isinstance(caps, list):
        caps = _caps_from_record(catalog, definition, record)
    field_keys = {f["key"] for f in definition["fields"]}
    values = {k: v for k, v in values.items() if k in field_keys}
    other = {k: v for k, v in config.items() if k not in _KNOWN_CONFIG_KEYS and k != "engine_capabilities"}
    return {
        "service_id": record.get("id"),
        "definition": definition["definition"],
        "definition_version": definition["definition_version"],
        "inferred": not config.get("definition"),
        "name": record.get("name"),
        "enabled": bool(record.get("enabled", True)),
        "locked": service_registry.is_litellm_service_record(record.get("id"), record),
        "lifecycle_editable": lifecycle_editable,
        "values": values,
        "key": key,
        "capabilities": caps,
        "models": models,
        "tests": copy.deepcopy(config.get("engine_tests") or {}),
        "check": copy.deepcopy(config.get("engine_check") or {}),
        "workflow": workflow,
        "calls": call_line(definition, values),
        "other_settings": copy.deepcopy(other),
        "record": redacted_record(record),
    }


def _caps_from_record(catalog: EngineCatalog, definition: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
    """For a record saved before definitions: a capability is on when the record holds a word only it uses."""
    have = set(record.get("capabilities") or ())
    words = {cap_id: set(catalog.capability(cap_id)["record_capabilities"]) for cap_id in definition["capabilities"]}
    out = []
    for cap_id, own in words.items():
        shared = set().union(*(other for other_id, other in words.items() if other_id != cap_id))
        if (own - shared) & have:
            out.append(cap_id)
    return out


def redacted_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The stored record as the page shows it: secret ids and references masked, never a value."""
    out = copy.deepcopy({k: v for k, v in record.items() if k in _RECORD_KEYS})

    def mask(value: Any) -> Any:
        if isinstance(value, Mapping):
            masked = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered.endswith("_secret_id") or lowered == "api_key":
                    masked[key] = "configured"
                elif lowered == "graph":
                    masked[key] = f"(a workflow of {len(item) if isinstance(item, Mapping) else 0} nodes)"
                else:
                    masked[key] = mask(item)
            return masked
        if isinstance(value, list):
            return [mask(item) for item in value]
        return value

    out = mask(out)
    if isinstance(out.get("secret_refs"), Mapping):
        out["secret_refs"] = {k: "configured" for k in out["secret_refs"]}
    return out


# ---------------------------------------------------------------------------
# Create and edit (called from the services routes when the body names a definition)
# ---------------------------------------------------------------------------


def _gate(check: Mapping[str, Any], *, acknowledge_warnings: bool) -> None:
    errors = [f for f in check.get("findings") or () if f.get("severity") == "error"]
    if errors:
        raise EngineRouteError(422, "engine_check_failed", errors[0]["what"],
                               field=errors[0].get("field"), check=check)
    warnings = [f for f in check.get("findings") or () if f.get("severity") == "warning"]
    if warnings and not acknowledge_warnings:
        raise EngineRouteError(status.HTTP_409_CONFLICT, "engine_check_warnings",
                               f"{warnings[0]['what']} Send acknowledge_warnings to add it anyway.",
                               field=warnings[0].get("field"), check=check)


def _check_summary(check: Mapping[str, Any] | None, *, address: str, disabled: bool, reason: str = "") -> dict[str, Any]:
    if disabled or check is None:
        summary = {"status": "not_started", "origin": origin_of(address)}
        if reason in ("needkey",):
            # Kept off because the server needs a key only the owner can add.
            summary["reason"] = reason
        return summary
    return {
        "status": "ok" if check.get("outcome") != "error" else "failed",
        "checked_at": check.get("checked_at"),
        "origin": origin_of(address),
        "key_sent": bool(check.get("key_sent")),
        "model_count": len(check.get("models") or ()),
        "capabilities": [{key: row[key] for key in ("id", "level", "evidence", "why") if key in row}
                         for row in check.get("capabilities") or ()],
        "model_use": {row["id"]: row.get("use", "") for row in check.get("models") or ()},
    }


def _new_secret_id(service_id: str, purpose: str) -> str:
    return f"engine:{service_id}:{purpose}:{secrets.token_hex(4)}"


def _same_text(value: Any) -> str:
    """One spelling per value, so 120 and "120.0" compare equal."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    return f"{number:g}"


def _admin_limits(ctx: Any, values: Mapping[str, Any], definition: Mapping[str, Any], *, before: Mapping[str, Any] | None) -> None:
    if _is_owner(ctx):
        return
    pasted = str(values.get("api_key") or "")
    if pasted and pasted != SECRET_MASK:
        raise _owner_only("Only the owner can add a key.")
    before_values = (before or {}).get("values") or {}
    if values.get("api_key_env") and values.get("api_key_env") != before_values.get("api_key_env"):
        raise _owner_only("Only the owner can read a key from a server environment variable.")
    for row in values.get("headers") or ():
        if row.get("secret") and row.get("value") not in ("", SECRET_MASK):
            raise _owner_only("Only the owner can add or replace secret values.")
    changed_lifecycle = any(_same_text(values.get(k, "")) != _same_text(before_values.get(k, _field_default(definition, k, "")))
                            for k in _LIFECYCLE_FIELDS if k in values)
    if changed_lifecycle and (values.get("lifecycle_mode") == "managed" or before_values.get("lifecycle_mode") == "managed"):
        raise _owner_only("Only the owner can change how Azizam starts and stops a server.")


def old_headers_ids(config: Mapping[str, Any]) -> set[str]:
    return {str(h.get("value_secret_id")) for h in config.get("headers") or () if isinstance(h, Mapping) and h.get("value_secret_id")}


class EngineService:
    """The create and edit steps. The store persists; the r47 rules are re-checked there."""

    def __init__(self, store: Any, *, catalog_loader: Callable[[], EngineCatalog] = load_engine_catalog,
                 fetch: Fetch | None = None, env: Callable[[str], str | None] | None = None) -> None:
        self.store = store
        self.catalog_loader = catalog_loader
        self.fetch = fetch or make_http_fetch()
        self.env = env or (lambda name: os.environ.get(name) or None)

    @property
    def db_path(self) -> Path:
        return Path(getattr(self.store, "path"))

    def catalog(self) -> EngineCatalog:
        try:
            return self.catalog_loader()
        except EngineDefinitionError as exc:
            raise EngineRouteError(status.HTTP_503_SERVICE_UNAVAILABLE, "engine_definitions_invalid", str(exc)) from None

    async def check(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        registry = self.store.get_service_registry()
        service_id = body.get("service_id")
        record = (registry.get("services") or {}).get(service_id) if service_id else None
        if service_id and record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", f"No engine called {service_id!r}.")
        clean = clean_values(definition, values)
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        clean = {**clean, "headers": self._headers_for_check(clean.get("headers") or [], record, clean["address"])}
        return await run_check(catalog, definition, clean, key=key, fetch=self.fetch,
                               existing=_existing_engines(registry, exclude=service_id),
                               viewer_role="owner" if _is_owner(ctx) else "admin")

    def _headers_for_check(self, headers: list[Mapping[str, Any]], record: Mapping[str, Any] | None, address: str) -> list[dict[str, Any]]:
        """Pasted header values go as typed; stored secret values only to the address they were stored for."""
        stored = {}
        if record:
            config = record.get("config") or {}
            same_origin = origin_of(address) == str(config.get("credential_origin") or "")
            for row in config.get("headers") or ():
                if isinstance(row, Mapping) and row.get("value_secret_id") and same_origin:
                    try:
                        stored[row["name"]] = secret_store.get_provider_secret(row["value_secret_id"], db_path=self.db_path,
                                                                               expected_origin=config.get("credential_origin") or None)
                    except Exception:  # noqa: BLE001
                        continue
        out = []
        for row in headers:
            value = row.get("value")
            if value == SECRET_MASK:
                value = stored.get(row.get("name"))
            if value:
                out.append({"name": row["name"], "value": value})
        return out

    async def test(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        registry = self.store.get_service_registry()
        service_id = body.get("service_id")
        record = (registry.get("services") or {}).get(service_id) if service_id else None
        clean = clean_values(definition, values)
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        clean = {**clean, "headers": self._headers_for_check(clean.get("headers") or [], record, clean["address"])}
        capability = str(body.get("capability") or "")
        workflow = body.get("workflow") if isinstance(body.get("workflow"), Mapping) else values.get("workflow_graph")
        mapping = body.get("inputs") if isinstance(body.get("inputs"), Mapping) else values.get("workflow_inputs")
        if definition["flow"] == "workflow":
            capability = capability or definition["capabilities"][0]
        model = str(body.get("model") or values.get("server_model") or values.get("public_name") or "").strip() or None
        result = await run_real_test(catalog, definition, clean, capability, key=key, fetch=self.fetch, model=model,
                                     voice=str(body.get("voice") or values.get("default_voice") or "") or None,
                                     workflow=workflow, mapping=mapping)
        result["key_sent"] = bool(key.value)
        result["key_withheld"] = key.withheld
        if record is not None:
            result["via"] = "saved"
        return result

    def workflow(self, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition") or "node-graph")
        if definition["flow"] != "workflow":
            raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "no_workflow", "This kind of server does not run workflow files.")
        text = body.get("text")
        if not isinstance(text, str):
            raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "invalid_workflow", "Send the file’s text as text.", field="file")
        file_name = str(body.get("file") or "workflow.json")
        result = read_workflow(definition, text, file_name)
        if not result["ok"]:
            row = next((r for r in catalog.advice_rows(definition) if r["code"] == "badfile"), None)
            if row is not None and result["reason"] in ("ui_format", "not_json"):
                result["finding"] = {
                    "code": row["code"], "severity": "error", "field": "file", "title": row["title"],
                    "what": row["what"].replace("{file}", file_name), "why": row["why"], "fix": row["fix"], "keep_off": True,
                    "details": {"raw": result.get("found", ""), "raw_label": row.get("raw_label") or "What Azizam found in the file"},
                }
            elif result["reason"] == "too_large":
                result["finding"] = {"code": "too_large", "severity": "error", "field": "file", "title": "The file is too large",
                                     "what": f"“{file_name}” is larger than {definition['workflow']['max_bytes'] // 1048576} MB.",
                                     "why": "A workflow file is small. A larger file is probably not a workflow.",
                                     "fix": "Pick the workflow file you exported in API format.", "keep_off": True}
        else:
            result["graph"] = __import__("json").loads(text)
        return result

    def view(self, service_id: str) -> dict[str, Any]:
        registry = self.store.get_service_registry()
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", f"No engine called {service_id!r}.")
        return engine_view(self.catalog(), record, db_path=self.db_path)

    async def create(self, ctx: Any, body: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            raise DraftError("Give the engine a name.", field="name")
        clean = clean_values(definition, values)
        _admin_limits(ctx, {**values, **clean}, definition, before=None)
        registry = self.store.get_service_registry()
        base_id = service_registry.normalize_id(re.sub(r"[^a-z0-9 _.-]+", "", name.lower())) or "engine"
        service_id, n = base_id, 2
        while service_id in (registry.get("services") or {}):
            service_id, n = f"{base_id}-{n}", n + 1
        disabled = bool(body.get("disabled"))
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=None, db_path=self.db_path, env=self.env)
        check: dict[str, Any] | None = None
        if disabled:
            # A server kept off is not checked, but an address already in use still stops.
            dup = duplicate_finding(catalog, definition, clean, _existing_engines(registry),
                                    viewer_role="owner" if _is_owner(ctx) else "admin")
            if dup is not None:
                raise EngineRouteError(status.HTTP_409_CONFLICT, "engine_duplicate", dup["what"], field="address",
                                       check={"outcome": "error", "findings": [dup]})
        else:
            check = await run_check(catalog, definition, clean, key=key, fetch=self.fetch,
                                    existing=_existing_engines(registry),
                                    viewer_role="owner" if _is_owner(ctx) else "admin")
            _gate(check, acknowledge_warnings=bool(body.get("acknowledge_warnings")))
        capabilities = body.get("capabilities")
        if capabilities is None and definition["flow"] == "workflow" and isinstance(values.get("workflow_graph"), Mapping):
            makes = values.get("workflow_makes") or read_workflow(definition, __import__("json").dumps(values["workflow_graph"]), "w.json").get("makes")
            capabilities = [cap_id for cap_id in definition["capabilities"] if cap_id.endswith("video" if makes == "video" else "images")]
        if capabilities is None:
            capabilities = [c["id"] for c in (check or {}).get("capabilities") or () if c.get("ticked")]
        if not isinstance(capabilities, list):
            raise DraftError("capabilities must be a list of ids.", field="capabilities")
        if disabled and body.get("capabilities") is None:
            capabilities = []
        models = body.get("models")
        if models is None and check is not None:
            models = check.get("models") or []
        record = self._write(ctx, catalog, definition, clean, service_id=service_id, name=name, capabilities=capabilities,
                             models=models, enabled=not disabled,
                             check=_check_summary(check, address=clean["address"], disabled=disabled, reason=str(body.get("keep_off_reason") or "")),
                             tests=_clean_tests(definition, body.get("tests")), key=key, existing=None,
                             expected_generation=expected_generation, action="create")
        return {**record, "service_id": service_id}

    async def update(self, ctx: Any, service_id: str, body: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        catalog = self.catalog()
        registry = self.store.get_service_registry()
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", f"No engine called {service_id!r}.")
        if service_registry.is_litellm_service_record(service_id, record):
            raise EngineRouteError(status.HTTP_403_FORBIDDEN, "service_not_editable", "Azizam runs this engine itself. Only its name and Enabled can change.")
        before = engine_view(catalog, record, db_path=self.db_path)
        definition = _definition(catalog, body.get("definition") or before["definition"])
        values = {**before["values"], **(body.get("values") if isinstance(body.get("values"), Mapping) else {})}
        if "api_key" not in values:
            values["api_key"] = SECRET_MASK if before["key"]["state"] == "configured" and before["key"].get("source") == "stored" else ""
        clean = clean_values(definition, values)
        _admin_limits(ctx, {**values, **clean}, definition, before=before)
        name = str(body.get("name") or before["name"] or service_id).strip()
        disabled = bool(body.get("disabled"))
        enabled = False if disabled else bool(body.get("enabled", before["enabled"]))
        moved = (comparable_address(clean["address"]) != comparable_address(before["values"]["address"] or clean["address"])
                 or clean.get("path", "") != before["values"].get("path", ""))
        connection_changed = (
            moved
            or (values.get("api_key") not in ("", SECRET_MASK, None))
            or clean.get("api_key_env") != (before["key"].get("env_name") or "")
            or clean.get("path_prefix", "") != (before["values"].get("path_prefix") or "")
            or clean.get("auth_header", "Authorization") != (before["values"].get("auth_header") or "Authorization")
            or [(h["name"], h["value"], h["secret"]) for h in clean.get("headers") or ()]
               != [(h["name"], h["value"], h["secret"]) for h in before["values"].get("headers") or ()]
            or _same_text(clean.get("connect_timeout_s")) != _same_text(before["values"].get("connect_timeout_s", clean.get("connect_timeout_s")))
            or body.get("recheck") is True
        )
        if moved:
            # An address already in use stops, saved on or off.
            dup = duplicate_finding(catalog, definition, clean, _existing_engines(registry, exclude=service_id),
                                    viewer_role="owner" if _is_owner(ctx) else "admin")
            if dup is not None:
                raise EngineRouteError(status.HTTP_409_CONFLICT, "engine_duplicate", dup["what"], field="address",
                                       check={"outcome": "error", "findings": [dup]})
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        check_summary = before["check"] or {}
        if connection_changed and not disabled:
            check = await run_check(catalog, definition, {**clean, "headers": self._headers_for_check(clean["headers"], record, clean["address"])},
                                    key=key, fetch=self.fetch, existing=_existing_engines(registry, exclude=service_id),
                                    viewer_role="owner" if _is_owner(ctx) else "admin")
            _gate(check, acknowledge_warnings=bool(body.get("acknowledge_warnings")))
            check_summary = _check_summary(check, address=clean["address"], disabled=False)
        elif disabled and connection_changed:
            check_summary = _check_summary(None, address=clean["address"], disabled=True)
        capabilities_sent = isinstance(body.get("capabilities"), list)
        capabilities = body.get("capabilities") if capabilities_sent else before["capabilities"]
        models = body.get("models") if isinstance(body.get("models"), list) else before["models"]
        tests = dict(before["tests"])
        if connection_changed:
            tests = {}
        tests.update(_clean_tests(definition, body.get("tests")))
        has = {f["key"] for f in definition["fields"]}
        unmodelled = {key for field, keys in _FIELD_CONFIG_KEYS.items() if field not in has for key in keys}
        keep = {k: v for k, v in (record.get("config") or {}).items()
                if (k not in _KNOWN_CONFIG_KEYS or k in unmodelled) and k != "engine_capabilities"}
        words = capability_words(catalog, definition)
        # Sent capabilities keep the words this kind does not use; unsent ones stay exactly as stored.
        extra_capabilities = [c for c in record.get("capabilities") or () if c not in words]
        if definition["flow"] == "workflow" and "workflow_graph" not in values and (record.get("config") or {}).get("workflows"):
            existing_workflows = copy.deepcopy(record["config"]["workflows"])
            if values.get("workflow_public_name"):
                existing_workflows[0]["public_name"] = values["workflow_public_name"]
            keep["workflows"] = existing_workflows
        return self._write(ctx, catalog, definition, clean, service_id=service_id, name=name, capabilities=capabilities,
                           models=models, enabled=enabled, check=check_summary, tests=tests, key=key, existing=record,
                           expected_generation=expected_generation, action="update", keep=keep,
                           extra_capabilities=extra_capabilities,
                           record_capabilities=None if capabilities_sent else list(record.get("capabilities") or ()),
                           keep_lifecycle=all(str(values.get(k, "")) == str(before["values"].get(k, ""))
                                              for k in _LIFECYCLE_FIELDS),
                           key_dropped=bool(
                               key.withheld
                               and values.get("api_key") in ("", SECRET_MASK, None)
                               and origin_of(clean["address"]) != origin_of(before["values"].get("address") or clean["address"])
                           ))

    def _write(self, ctx: Any, catalog: EngineCatalog, definition: Mapping[str, Any], values: Mapping[str, Any], *,
               service_id: str, name: str, capabilities: list[Any], models: Any, enabled: bool, check: Mapping[str, Any],
               tests: Mapping[str, Any], key: KeyInput, existing: Mapping[str, Any] | None, expected_generation: int,
               action: str, keep: Mapping[str, Any] | None = None, key_dropped: bool = False,
               keep_lifecycle: bool = False, extra_capabilities: Iterable[str] = (),
               record_capabilities: list[str] | None = None) -> dict[str, Any]:
        """Write new secrets first, then the record; on failure remove what was written. Old secrets go after success."""
        written: list[str] = []
        retired: list[str] = []
        old_config = (existing or {}).get("config") or {}
        origin = origin_of(values["address"])
        old_origin = str(old_config.get("credential_origin") or "")
        # A stored secret never moves to another address: it is kept only where it was stored.
        origin_moved = existing is not None and bool(old_origin) and old_origin != origin
        try:
            secret_id: str | None = None
            if key.source == "pasted" and key.value:
                secret_id = _new_secret_id(service_id, "api_key")
                secret_store.put_provider_secret(secret_id, key.value, db_path=self.db_path, approved_origin=origin)
                written.append(secret_id)
                if old_config.get("api_key_secret_id"):
                    retired.append(str(old_config["api_key_secret_id"]))
            elif existing is not None and old_config.get("api_key_secret_id") and not key_dropped and not values.get("api_key_env"):
                secret_id = str(old_config["api_key_secret_id"])
            elif old_config.get("api_key_secret_id"):
                retired.append(str(old_config["api_key_secret_id"]))
            env_values = dict(values)
            if key_dropped:
                env_values["api_key_env"] = ""
            header_ids: dict[str, str] = {}
            old_headers = {h.get("name"): h for h in old_config.get("headers") or () if isinstance(h, Mapping)}
            for row in values.get("headers") or ():
                if not row["secret"]:
                    continue
                if row["value"] in ("", SECRET_MASK) and old_headers.get(row["name"], {}).get("value_secret_id"):
                    if not origin_moved:
                        header_ids[row["name"]] = old_headers[row["name"]]["value_secret_id"]
                    continue  # moved: the stored value is left behind, like the key
                if not row["value"] or row["value"] == SECRET_MASK:
                    raise DraftError(f"Type the value of the secret header {row['name']}.", field="headers")
                new_id = _new_secret_id(service_id, "header")
                secret_store.put_provider_secret(new_id, row["value"], db_path=self.db_path, approved_origin=origin)
                written.append(new_id)
                header_ids[row["name"]] = new_id
            for row in old_headers.values():
                if row.get("value_secret_id") and row["value_secret_id"] not in header_ids.values():
                    retired.append(row["value_secret_id"])
            raw = build_raw(catalog, definition, env_values, name=name, capabilities=capabilities, models=models,
                            enabled=enabled, check=check, tests=tests, secret_id=secret_id, header_secret_ids=header_ids, keep=keep,
                            stored_models=old_config.get("models") or (), extra_capabilities=extra_capabilities,
                            record_capabilities=record_capabilities)
            if existing is not None:
                for field in ("priority", "role"):
                    if existing.get(field) is not None:
                        raw[field] = existing[field]
                for key in ("protocol", "native_passthrough"):
                    # How the gateway talks to an engine is set when it is added; an edit keeps it, set or unset.
                    if key in old_config:
                        raw[key] = copy.deepcopy(old_config[key])
                    else:
                        raw.pop(key, None)
            kept_key = (
                (secret_id and secret_id == old_config.get("api_key_secret_id"))
                or (raw.get("api_key_env") and raw.get("api_key_env") == old_config.get("api_key_env"))
                or (raw.get("api_key_ref") and raw.get("api_key_ref") == old_config.get("api_key_ref"))
                or any(h.get("value_secret_id") in old_headers_ids(old_config) for h in raw.get("headers") or () if h.get("value_secret_id"))
            )
            if existing is not None and old_origin and kept_key and not origin_moved:
                # The owner bound this key to this address; an edit that keeps both keeps the binding.
                raw["credential_origin"] = old_origin
            elif any(h.get("value_secret_id") for h in raw.get("headers") or ()) and not any(raw.get(k) for k in service_registry.CREDENTIAL_REFERENCE_KEYS):
                # Secret header values only: bind them to this address, as the store does for a key.
                raw["credential_origin"] = origin
            if existing is not None and keep_lifecycle:
                # Start and stop untouched: keep the stored block exactly (other drivers, status, cwd, env).
                # The config keeps its own copy of the block; carry that copy, so neither changes.
                stored = old_config.get("lifecycle") if isinstance(old_config.get("lifecycle"), Mapping) else existing.get("lifecycle")
                if isinstance(stored, Mapping):
                    raw["lifecycle"] = copy.deepcopy(dict(stored))
                else:
                    raw.pop("lifecycle", None)
            elif existing is not None and "lifecycle" in raw:
                old_driver_config = ((existing.get("lifecycle") or {}).get("driver_config") or {})
                for extra in ("status", "cwd", "env", "timeout_s"):
                    if extra in old_driver_config and (existing.get("lifecycle") or {}).get("driver") == "command":
                        raw["lifecycle"]["driver_config"].setdefault(extra, copy.deepcopy(old_driver_config[extra]))
            record = self.store.write_engine(service_id=service_id, raw=raw, actor=ctx, expected_generation=expected_generation,
                                             replace=existing is not None, existing=existing)
        except BaseException:
            for secret_id_written in written:
                try:
                    secret_store.delete_provider_secret(secret_id_written, db_path=self.db_path)
                except Exception:  # noqa: BLE001
                    pass
            raise
        for old in retired:
            try:
                secret_store.delete_provider_secret(old, db_path=self.db_path)
            except Exception:  # noqa: BLE001
                pass
        return record


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_engine_routes(router: APIRouter, *, service: EngineService, guard: Callable[[Request], Any],
                           read_json: Callable[[Request], Any]) -> None:
    """Mount the Add engine routes on the admin router (owner and administrator)."""

    def _error(exc: BaseException) -> JSONResponse:
        if isinstance(exc, EngineRouteError):
            return exc.as_response()
        if isinstance(exc, DraftError):
            return EngineRouteError(status.HTTP_400_BAD_REQUEST, "invalid_engine", str(exc), field=exc.field).as_response()
        if isinstance(exc, PermissionError):
            return _owner_only(str(exc)).as_response()
        raise exc

    @router.get("/engine-definitions")
    async def get_engine_definitions(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            # The file is read (and re-read when it changes) off the event loop.
            payload = await asyncio.to_thread(lambda: service.catalog().public())
        except EngineRouteError as exc:
            return exc.as_response()
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)

    @router.post("/engines/check")
    async def check_engine(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await read_json(request)
            result = await service.check(ctx, body)
        except (EngineRouteError, DraftError, PermissionError) as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - the route error type is owned by admin_api
            if hasattr(exc, "as_response"):
                return exc.as_response()  # type: ignore[no-any-return]
            raise
        return JSONResponse(status_code=status.HTTP_200_OK, content={"check": result})

    @router.post("/engines/test")
    async def test_engine(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await read_json(request)
            result = await service.test(ctx, body)
        except (EngineRouteError, DraftError, PermissionError) as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "as_response"):
                return exc.as_response()  # type: ignore[no-any-return]
            raise
        return JSONResponse(status_code=status.HTTP_200_OK, content={"test": result})

    @router.post("/engines/workflow")
    async def read_engine_workflow(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await read_json(request)
            result = await asyncio.to_thread(service.workflow, body)
        except (EngineRouteError, DraftError) as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "as_response"):
                return exc.as_response()  # type: ignore[no-any-return]
            raise
        return JSONResponse(status_code=status.HTTP_200_OK, content={"workflow": result})

    @router.get("/engines/{service_id}")
    async def get_engine(service_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            view = await asyncio.to_thread(service.view, service_id)
        except EngineRouteError as exc:
            return exc.as_response()
        response = JSONResponse(status_code=status.HTTP_200_OK, content={"engine": view})
        response.headers["Cache-Control"] = "no-store"
        return response
