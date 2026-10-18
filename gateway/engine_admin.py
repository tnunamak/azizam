"""Add engine routes: definitions, check, draft test, who can use the models, engine view, create and edit.

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
from collections import deque
from contextlib import contextmanager
from math import ceil
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Iterator, Mapping

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from gateway import admin_store, category_enforcement, engine_redaction, secret_store, service_registry, transforms
from gateway.access_plans import PlanError, plan_from_document
from gateway.engine_access import who_can_use
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
    join_url,
    run_check,
    run_real_test,
)
from gateway.engine_definitions import EngineCatalog, EngineDefinitionError, load_engine_catalog
from gateway.engine_connection import inference_base_url
from gateway.litellm_substrate import CREDENTIAL_HEADER_RE, SECRET_PLACEHOLDER, header_problem, routed_models

# Record keys this module writes. Every other config key is the owner's and is
# carried through an edit untouched ("Other settings").
_KNOWN_CONFIG_KEYS = frozenset({
    "host", "port", "base_path", "scheme", "protocol", "native_passthrough", "transforms", "models",
    "definition", "definition_version", "address", "path_prefix",
    "timeout", "headers", "api_key_secret_id", "api_key_env", "api_key_ref", "credential_origin",
    "engine_check", "engine_tests", "model_policy_schema_version",
    "name", "enabled", "capabilities", "role", "priority", "managed", "lifecycle",
})
_RECORD_KEYS = frozenset({"id", "name", "kind", "source", "enabled", "priority", "role", "capabilities", "config",
                          "secret_refs", "lifecycle", "process", "observations", "residency", "resources",
                          "connection", "offerings", "protocol_operations", "control_file_roots",
                          "provenance", "editable"})
# An engine lists at most this many models in one access question.
MAX_ACCESS_MODELS = 500
_LIFECYCLE_FIELDS = ("lifecycle_mode", "lifecycle_start", "lifecycle_stop", "lifecycle_ready_s")
_TRANSFORM_KINDS = frozenset({"value-map", "drop-params", "default-params", "param-map", "message-inject", "clamp"})


class EngineRouteError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, field: str | None = None,
                 check: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.check = check
        self.headers = dict(headers or {})

    def as_response(self) -> JSONResponse:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            error["field"] = self.field
        content: dict[str, Any] = {"error": error}
        if self.check is not None:
            content["check"] = dict(self.check)
        return JSONResponse(status_code=self.status_code, content=content, headers=self.headers)


def _probe_limit(name: str, default: int) -> int:
    """Invalid settings keep the safe default rather than disabling protection."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


class _EngineProbeLimiter:
    """Shared check/draft-test budget per authenticated actor, per gateway process."""

    def __init__(self, *, max_concurrent: int, per_minute: int,
                 clock: Callable[[], float] | None = None) -> None:
        self.max_concurrent = max_concurrent
        self.per_minute = per_minute
        self._clock = clock or time.monotonic
        self._lock = Lock()
        self._active: dict[tuple[str, str], int] = {}
        self._starts: dict[tuple[str, str], deque[float]] = {}

    @contextmanager
    def slot(self, ctx: Any) -> Iterator[None]:
        # Never key on a request header, session, draft address or client IP.
        # A guard without an actor id conservatively shares one fallback bucket.
        key = (str(getattr(ctx, "actor_kind", None) or "user"),
               str(getattr(ctx, "actor_id", None) or ""))
        with self._lock:
            now = self._clock()
            for caller, starts in list(self._starts.items()):
                while starts and starts[0] <= now - 60:
                    starts.popleft()
                if not starts and not self._active.get(caller):
                    del self._starts[caller]
            starts = self._starts.setdefault(key, deque())
            if len(starts) >= self.per_minute:
                raise EngineRouteError(
                    status.HTTP_429_TOO_MANY_REQUESTS, "engine_probe_rate_limited",
                    "Too many engine checks this minute. Try again shortly.",
                    headers={"Retry-After": str(max(1, ceil(starts[0] + 60 - now)))},
                )
            if self._active.get(key, 0) >= self.max_concurrent:
                raise EngineRouteError(
                    status.HTTP_429_TOO_MANY_REQUESTS, "engine_probe_rate_limited",
                    "Too many engine checks are still running. Try again shortly.",
                    headers={"Retry-After": "1"},
                )
            starts.append(now)
            self._active[key] = self._active.get(key, 0) + 1
        try:
            yield
        finally:
            # Also release on malformed JSON, probe errors, disconnect/cancellation.
            with self._lock:
                remaining = self._active[key] - 1
                if remaining:
                    self._active[key] = remaining
                else:
                    del self._active[key]


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


def _clean_headers(value: Any, *, stored: Iterable[Any] = ()) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise DraftError("Request headers must be a list of name and value rows.", field="headers")
    out = []
    remaining = [copy.deepcopy(row) for row in stored if isinstance(row, Mapping)]
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise DraftError(f"Header {index + 1} needs a name and a value.", field="headers")
        name = str(row.get("name") or "").strip()
        if row.get("not_sent"):
            # Bind each hidden row to its stored header name. Reordering or
            # removing rows must never move a secret to a different header.
            matched = next((old for old in remaining if old.get("name") == name
                            and (old.get("secret") or header_problem(name, str(old.get("value") or "")))), None)
            if matched is None or row.get("value") != engine_redaction.REDACTED:
                raise DraftError("This hidden header has no stored value. Remove it and use the API key field.", field="headers")
            remaining.remove(matched)
            out.append(matched)
            continue
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise DraftError(f"Header {index + 1}: “{name}” is not a header name.", field="headers")
        value_text = str(row.get("value") or "")
        problem = "credential" if row.get("secret") else header_problem(name, value_text or "x")
        if problem == "transport":
            raise DraftError(f"Header {index + 1}: Azizam sets “{name}” itself.", field="headers")
        if problem == "credential":
            # Header values are saved as plain text. A key goes in the API key field, which is stored encrypted.
            raise DraftError(f"Header {index + 1}: put a key in the API key field. Header values are saved as plain text.",
                             field="headers")
        if problem or not value_text:
            raise DraftError(f"Header {index + 1}: a value can hold plain letters, digits and signs only, no line break.", field="headers")
        out.append({"name": name, "value": value_text})
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
    return {"type": "command", "managed": True, "driver": "command", "driver_config": driver_config}


def clean_values(definition: Mapping[str, Any], values: Mapping[str, Any], *,
                 stored_headers: Iterable[Any] = ()) -> dict[str, Any]:
    """Validate draft values against the definition's field list."""
    if not isinstance(values, Mapping):
        raise DraftError("values must be an object.", field="values")
    fields = {f["key"]: f for f in definition["fields"]}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise DraftError(f"This kind of server has no field called {unknown[0]!r}.", field=unknown[0])
    out = {key: copy.deepcopy(value) for key, value in values.items()}
    out["address"] = normalize_address(values.get("address"), placeholder=fields["address"].get("placeholder", ""))
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
    out["headers"] = _clean_headers(values.get("headers"), stored=stored_headers)
    out["transforms"] = _clean_transforms(values["transforms"]) if "transforms" in values else None
    out["reply_timeout_s"] = _number(values, "reply_timeout_s", _field_default(definition, "reply_timeout_s", 600), low=1, high=3600)
    return out


def _existing_engines(registry: Mapping[str, Any], *, exclude: str | None = None) -> list[dict[str, Any]]:
    out = []
    for sid, record in (registry.get("services") or {}).items():
        if sid == exclude:
            continue
        config = record.get("config") or {}
        address = config.get("address") or service_registry.service_base_url(config)
        if address:
            out.append({"id": sid, "name": record.get("name") or sid, "address": address})
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
    name = str(config.get("api_key_env") or config.get("api_key_ref") or "").strip()
    if not name and (record.get("secret_refs") or {}).get("api_key") == SECRET_PLACEHOLDER and not config.get("api_key_secret_id"):
        name = "AZIZAM_DOWNSTREAM_API_KEY"
    if name:
        return env(name), "env", origin
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        try:
            return secret_store.get_provider_secret(secret_id, db_path=db_path, expected_origin=origin or None), "stored", origin
        except Exception:  # noqa: BLE001 - an unreadable secret is "no key", never a crash
            return None, "stored", origin
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
    stored_env = stored_config.get("api_key_env") or stored_config.get("api_key_ref") or ""
    if not stored_env and ((record or {}).get("secret_refs") or {}).get("api_key") == SECRET_PLACEHOLDER and not stored_config.get("api_key_secret_id"):
        stored_env = "AZIZAM_DOWNSTREAM_API_KEY"
    if env_name and env_name != str(stored_env):
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


def model_rows(models: Any) -> list[dict[str, Any]]:
    """A record's models as rows. Older records keep a mapping of name to settings; read it as rows."""
    if isinstance(models, Mapping):
        rows = []
        for name, settings in models.items():
            row = dict(settings) if isinstance(settings, Mapping) else {"upstream_model": str(settings or name)}
            ident = row.get("id") or name
            row["id"] = ident
            row["model_group"] = row.get("model_group") or ident
            row["upstream_model"] = row.get("upstream_model") or row.get("deployment") or row.get("model") or ident
            rows.append(row)
        return rows
    return [dict(row) for row in models or () if isinstance(row, Mapping)]


def _models_config(definition: Mapping[str, Any], values: Mapping[str, Any], models: Any,
                   stored: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Model rows for the record. A row the record already holds keeps every key this form does not edit."""
    stored_rows = model_rows(stored)

    def stored_row(upstream: str, public: str) -> dict[str, Any]:
        for row in stored_rows:
            if row.get("upstream_model") == upstream or row.get("model_group") == public:
                return copy.deepcopy(row)
        return {}

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
        # A public name or upstream edit does not create a new offering.
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


# The config keys each optional field owns. A definition without the field leaves them as stored.
_FIELD_CONFIG_KEYS = {
    "path_prefix": ("path_prefix",),
    "transforms": ("transforms",),
    "headers": ("headers",),
    "reply_timeout_s": ("timeout",),
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
    # The definition writes one host/port/path connection. Older URL aliases
    # must not override it after an owner saves a different address.
    for alias in ("base_url", "api_base", "default_base_url"):
        raw.pop(alias, None)
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
    if "transforms" in has:
        transforms_value = values.get("transforms")
        # A new engine gets the kind's default adjustments (local chat: reasoning-effort-final-chat,
        # AGENTS.md D-1). An owner who removes them keeps an explicit empty list, so the choice stays.
        raw["transforms"] = transforms_value if transforms_value is not None else list(definition["record"].get("transforms") or [])
    if "path_prefix" in has:
        if values.get("path_prefix"):
            raw["path_prefix"] = values["path_prefix"]
        else:
            raw.pop("path_prefix", None)
    if "reply_timeout_s" in has:
        # The model router waits this long for a reply (litellm_substrate reads ``timeout``).
        raw["timeout"] = int(values["reply_timeout_s"])
    if "headers" in has:
        # Sent with every chat and embedding request (litellm_substrate) and every direct call (proxy).
        headers = copy.deepcopy(values.get("headers") or [])
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
    if tests:
        raw["engine_tests"] = dict(tests)
    else:
        raw.pop("engine_tests", None)
    raw.pop("api_key_secret_id", None)
    raw.pop("api_key_env", None)
    raw.pop("api_key_ref", None)
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


def engine_view(catalog: EngineCatalog, record: Mapping[str, Any], *, db_path: Path,
                hop_timeout: float = 600, env: Callable[[str], str | None] = os.environ.get) -> dict[str, Any]:
    config = record.get("config") or {}
    definition = catalog.definition(infer_definition(catalog, record))
    assert definition is not None
    address = str(config.get("address") or service_registry.service_base_url(config) or "")
    effective_base = service_registry.service_base_url(config)
    if address and effective_base:
        projected = _split_address(address, definition, str(config.get("path_prefix") or ""))
        if comparable_address(service_registry.service_base_url(projected)) != comparable_address(effective_base):
            if definition["record"]["base_path"] != "from_address":
                address = origin_of(effective_base)
            else:
                prefix = "/" + str(config.get("path_prefix") or "").strip("/")
                address = effective_base[:-len(prefix)] if prefix != "/" and effective_base.endswith(prefix) else effective_base
    values: dict[str, Any] = {"address": address}
    env_name = config.get("api_key_env") or config.get("api_key_ref") or ""
    if not env_name and (record.get("secret_refs") or {}).get("api_key") == SECRET_PLACEHOLDER and not config.get("api_key_secret_id"):
        env_name = "AZIZAM_DOWNSTREAM_API_KEY"
    values["api_key_env"] = env_name
    values["path_prefix"] = config.get("path_prefix") or ""
    hop_routed = bool(config.get("gateway_lifecycle") or any(
        row.get("gateway_lifecycle") for row in model_rows(config.get("models"))
    ))
    effective_timeout = config.get("timeout") or (hop_timeout if hop_routed else None)
    values["reply_timeout_s"] = effective_timeout if effective_timeout is not None else ""
    timeout_source = ("saved on this engine" if config.get("timeout") else
                      "default for engines Azizam starts" if hop_routed else "model router default (no saved timeout)")
    values["headers"] = []
    for header in config.get("headers") or ():
        if not isinstance(header, Mapping):
            continue
        name, value = str(header.get("name") or ""), str(header.get("value") or "")
        if header.get("secret") or header_problem(name, value):
            values["headers"].append({"name": name, "value": engine_redaction.REDACTED, "secret": True, "not_sent": True})
        else:
            values["headers"].append({"name": name, "value": value})
    values["transforms"] = copy.deepcopy(config.get("transforms") or [])
    lifecycle = service_registry.legacy_lifecycle_from_service(record)
    driver_config = lifecycle.get("driver_config") or {}
    if lifecycle.get("driver") == "command":
        values.update(lifecycle_mode="managed", lifecycle_start=engine_redaction.redact_command(driver_config.get("start", "")),
                      lifecycle_stop=engine_redaction.redact_command(driver_config.get("stop", "")),
                      lifecycle_ready_s=driver_config.get("health_timeout_s", 120))
    else:
        values["lifecycle_mode"] = "external"
    lifecycle_editable = lifecycle.get("driver") in (None, "", "command", "external") and not any(
        isinstance(driver_config.get(key), list) for key in ("start", "stop")
    )
    locked = service_registry.is_litellm_service_record(record.get("id"), record)
    editable = not locked
    model_use = (config.get("engine_check") or {}).get("model_use") or {}
    models = [{"id": m.get("upstream_model"), "public_name": m.get("model_group") or m.get("id"),
               "context_window": m.get("context_window"), "enabled": m.get("enabled", True),
               "use": model_use.get(m.get("upstream_model"), "")}
              for m in model_rows(config.get("models"))]
    secret_id = str(config.get("api_key_secret_id") or "")
    key: dict[str, Any] = {"state": "none"}
    if env_name:
        key = {"state": "configured", "source": "env", "env_name": env_name, "origin": config.get("credential_origin") or ""}
    elif secret_id:
        key = {"state": "configured", "source": "stored", "origin": config.get("credential_origin") or ""}
    caps = config.get("engine_capabilities")
    if not isinstance(caps, list):
        caps = _caps_from_record(catalog, definition, record)
    field_keys = {f["key"] for f in definition["fields"]}
    schema_matches = not config.get("protocol") or config["protocol"] == definition["record"]["protocol"]
    declared_offerings = isinstance(record.get("offerings"), list) or isinstance(config.get("offerings"), list)
    unsupported_fields = {} if schema_matches else {
        key: "The stored protocol has no matching definition schema, so this page cannot edit this setting safely."
        for key in field_keys
    }
    values = {k: v for k, v in values.items() if k in field_keys}
    other = {k: v for k, v in config.items() if k not in _KNOWN_CONFIG_KEYS and k != "engine_capabilities"}
    other = engine_redaction.redact_service({"config": other})["config"]
    request_base = inference_base_url(config)
    operations = []
    for cap_id in caps if schema_matches else ():
        route = (catalog.capability(cap_id) or {}).get("route") or {}
        if route.get("path"):
            base = request_base
            operations.append({"capability": cap_id, "method": "POST", "url": join_url(base, route["path"])})
    v2 = service_registry.service_record_v2(record)
    v2["connection"]["address"] = address
    v2 = _redact_record_value(v2)
    return {
        "service_id": record.get("id"),
        "definition": definition["definition"],
        "definition_version": definition["definition_version"],
        "inferred": not config.get("definition"),
        "name": record.get("name"),
        "enabled": bool(record.get("enabled", True)),
        "locked": locked,
        "editable": editable,
        "lifecycle_editable": lifecycle_editable,
        "values": values,
        "reply_timeout": {"seconds": effective_timeout, "source": timeout_source},
        "attention": ([{"code": "engine_credential_unavailable",
                        "message": f"Key not available: {env_name} is not set"}] if env_name and not env(str(env_name)) else []),
        "key": key,
        "capabilities": caps,
        "models": models,
        "tests": copy.deepcopy(config.get("engine_tests") or {}),
        "check": copy.deepcopy(config.get("engine_check") or {}),
        "calls": call_line(definition, values),
        "other_settings": _redact_record_value(other),
        "record": redacted_record(record),
        "v2": v2,
        "request_path": {"mode": "gateway_lifecycle" if hop_routed else "direct", "source": "service record",
                         "url": operations[0]["url"] if operations else "", "operations": operations},
        "credential_destination": key.get("origin") or origin_of(address),
        "ignored_headers": [str(row.get("name") or "(unnamed)") for row in config.get("headers") or ()
                            if isinstance(row, Mapping) and (row.get("secret") or header_problem(str(row.get("name") or ""), str(row.get("value") or "")))],
        "unsupported_fields": unsupported_fields,
        "can_edit_offerings": schema_matches and not declared_offerings,
        "offering_edit_reason": "These offerings declare selection or workflow settings that the model-list editor cannot preserve. They are shown as stored." if declared_offerings else None,
    }


def _restore_lifecycle_markers(values: dict[str, Any], shown: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """Put the stored start and stop commands back behind the markers the page was shown.

    The page shows a command with its credential arguments hidden. A save that leaves every
    lifecycle field as shown needs nothing (the stored block is kept whole); a save that edits one
    keeps each marker's stored value, and refuses a marker the stored command does not hold.
    """
    if all(str(values.get(key, "")) == str(shown.get(key, "")) for key in _LIFECYCLE_FIELDS):
        return values
    stored = service_registry.legacy_lifecycle_from_service(record).get("driver_config") or {}
    restored = dict(values)
    for field, source in (("lifecycle_start", "start"), ("lifecycle_stop", "stop")):
        try:
            restored[field] = engine_redaction.restore_command(values.get(field), stored.get(source))
        except ValueError as exc:
            raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "invalid_engine", str(exc), field=field) from None
    return restored


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


def _redact_record_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        masked = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "env" and isinstance(item, Mapping):
                masked[key] = {name: engine_redaction.REDACTED for name in item}
            elif lowered.endswith("_secret_id") or lowered in ("api_key", "secret_ref"):
                masked[key] = "configured" if item else item
            elif lowered == "value" and "name" in value and (value.get("secret") or header_problem(str(value.get("name") or ""), str(item))):
                masked[key] = "configured"
            else:
                masked[key] = _redact_record_value(item)
        return masked
    if isinstance(value, list):
        return [_redact_record_value(item) for item in value]
    return value


def redacted_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The stored record as the page shows it, with references and environment values masked."""
    out = copy.deepcopy({k: v for k, v in record.items() if k in _RECORD_KEYS})
    if "lifecycle" not in out:
        out["lifecycle"] = copy.deepcopy(dict(service_registry.legacy_lifecycle_from_service(record)))
    out = _redact_record_value(out)
    if isinstance(out.get("secret_refs"), Mapping):
        out["secret_refs"] = {k: "configured" for k in out["secret_refs"]}
    return engine_redaction.redact_service(out)


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


def _header_credentials_are_the_owners(ctx: Any, values: Any) -> None:
    """One rule on every route that takes values (check, test, create, update), before anything is sent.

    A header row whose name carries a credential (Authorization, Proxy-Authorization,
    any name with key, token, secret, auth or cookie) or that the client marks
    secret is the owner's, whatever the flag says. An administrator gets 403; the
    owner then meets ``_clean_headers``, which sends the key to the API key field.
    """
    if _is_owner(ctx) or not isinstance(values, Mapping) or not isinstance(values.get("headers"), list):
        return
    for row in values["headers"]:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        if row.get("secret") or name.lower() in ("authorization", "proxy-authorization") or CREDENTIAL_HEADER_RE.search(name):
            raise _owner_only("Only the owner can send a key, and only in the API key field.")


def _admin_limits(ctx: Any, values: Mapping[str, Any], definition: Mapping[str, Any], *, before: Mapping[str, Any] | None) -> None:
    if _is_owner(ctx):
        return
    pasted = str(values.get("api_key") or "")
    if pasted and pasted != SECRET_MASK:
        raise _owner_only("Only the owner can add a key.")
    before_values = (before or {}).get("values") or {}
    if values.get("api_key_env") and values.get("api_key_env") != before_values.get("api_key_env"):
        raise _owner_only("Only the owner can read a key from a server environment variable.")
    changed_lifecycle = any(_same_text(values.get(k, "")) != _same_text(before_values.get(k, _field_default(definition, k, "")))
                            for k in _LIFECYCLE_FIELDS if k in values)
    if changed_lifecycle and (values.get("lifecycle_mode") == "managed" or before_values.get("lifecycle_mode") == "managed"):
        raise _owner_only("Only the owner can change how Azizam starts and stops a server.")


class EngineService:
    """The create and edit steps. The store persists; the r47 rules are re-checked there."""

    def __init__(self, store: Any, *, catalog_loader: Callable[[], EngineCatalog] = load_engine_catalog,
                 fetch: Fetch | None = None, env: Callable[[str], str | None] | None = None,
                 hop_timeout: Callable[[], float] | None = None) -> None:
        self.store = store
        self.catalog_loader = catalog_loader
        self.fetch = fetch or make_http_fetch()
        self.env = env or (lambda name: os.environ.get(name) or None)
        self.hop_timeout = hop_timeout or (lambda: 600.0)

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
        _header_credentials_are_the_owners(ctx, body.get("values"))
        clean = clean_values(definition, values, stored_headers=((record or {}).get("config") or {}).get("headers") or ())
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        existing = _existing_engines(registry, exclude=service_id)
        if record is not None:
            saved = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)["values"]
            if comparable_address(clean["address"]) == comparable_address(saved.get("address") or ""):
                # Checking an engine where it already is: the duplicate rule is for moving onto another's address.
                existing = []
        return await run_check(catalog, definition, clean, key=key, fetch=self.fetch, existing=existing,
                               viewer_role="owner" if _is_owner(ctx) else "admin")

    async def test(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        registry = self.store.get_service_registry()
        service_id = body.get("service_id")
        record = (registry.get("services") or {}).get(service_id) if service_id else None
        _header_credentials_are_the_owners(ctx, body.get("values"))
        clean = clean_values(definition, values, stored_headers=((record or {}).get("config") or {}).get("headers") or ())
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        capability = str(body.get("capability") or "")
        model = str(body.get("model") or "").strip() or None
        result = await run_real_test(catalog, definition, clean, capability, key=key, fetch=self.fetch, model=model,
                                     viewer_role="owner" if _is_owner(ctx) else "admin")
        result["key_sent"] = bool(key.value)
        result["key_withheld"] = key.withheld
        if record is not None:
            result["via"] = "saved"
        return result

    def access(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        """Who could use these models, from the stored access plans. Saves nothing.

        ``models`` holds public names, or ``{"name", "capabilities"}`` when the work a
        model does is known (the check's model list says so).
        """
        catalog = self.catalog()
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
            raise DraftError("capabilities is a list of what the engine does.", field="capabilities")
        unknown = [c for c in capabilities if c not in catalog.data["capabilities"]]
        if unknown:
            raise DraftError(f"No kind of work called {unknown[0]!r}.", field="capabilities")
        rows = body.get("models")
        if not isinstance(rows, list) or len(rows) > MAX_ACCESS_MODELS:
            raise DraftError("models is a list of the names apps will use.", field="models")
        names: list[str] = []
        works: dict[str, list[str]] = {}
        for row in rows:
            name, known = (row, None) if isinstance(row, str) else (
                (row.get("name"), row.get("capabilities")) if isinstance(row, Mapping) else (None, None))
            if not isinstance(name, str) or not name.strip() or len(name) > 256:
                raise DraftError("models is a list of the names apps will use.", field="models")
            if known is not None:
                if not isinstance(known, list) or not all(isinstance(c, str) for c in known):
                    raise DraftError(f"The work of “{name.strip()}” is a list of capability ids.", field="models")
                works[name.strip()] = known
            names.append(name.strip())
        return self._access_answer(ctx, names, capabilities, model_works=works)

    def _access_answer(self, ctx: Any, models: list[str], capabilities: list[str], *,
                       model_works: Mapping[str, list[str]] | None = None,
                       remote_provider: str | None = None) -> dict[str, Any]:
        plans, holders = [], {}
        unreadable = 0
        for record in self.store.list_access_plans():
            try:
                plans.append(plan_from_document(record["plan"], plan_id=str(record["id"])))
            except PlanError:
                unreadable += 1  # Deny by default: a plan that cannot be read includes nothing.
                continue
            holders[str(record["id"])] = record.get("used_by") or {}
        viewer = None
        if getattr(ctx, "actor_kind", "user") == "user" and getattr(ctx, "actor_id", None):
            viewer = admin_store.person_plan_id(str(ctx.actor_id), path=self.db_path)
        rules = category_enforcement.rules_state()
        answer = who_can_use(models, capabilities, plans, holders=holders, viewer_plan_id=viewer,
                             category_rules=rules == "governs", category_rules_unreadable=rules == "unreadable",
                             model_works=model_works, remote_provider=remote_provider)
        answer["unreadable_plans"] = unreadable
        return answer

    def view(self, service_id: str, ctx: Any = None) -> dict[str, Any]:
        registry = self.store.get_service_registry()
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", f"No engine called {service_id!r}.")
        catalog = self.catalog()
        view = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
        definition = _definition(catalog, view["definition"])
        # Every request names its work; the router serves an engine's models for any of
        # the work it routes, so an engine that names none is asked about all of it.
        works = list(view["capabilities"]) or list(definition["capabilities"])
        labels = {catalog.capability(cap_id)["label"]: cap_id for cap_id in definition["capabilities"]}
        model_works = {}
        for row in view["models"]:
            known = [labels[word] for word in str(row.get("use") or "").split(", ") if word in labels]
            if known and row.get("public_name"):
                model_works[row["public_name"]] = known
        served, provider = routed_models(service_id, record)
        if not served and not provider:
            served = [row["public_name"] for row in view["models"] if row.get("enabled", True) and row.get("public_name")]
        view["access"] = self._access_answer(ctx, served, works, model_works=model_works, remote_provider=provider)
        return view

    async def create(self, ctx: Any, body: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            raise DraftError("Give the engine a name.", field="name")
        _header_credentials_are_the_owners(ctx, body.get("values"))
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
        before = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
        definition = _definition(catalog, body.get("definition") or before["definition"])
        submitted = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        if before.get("offering_edit_reason"):
            offering_keys = {field["key"] for field in definition["fields"] if field.get("items") == "transform"
                             or (not field.get("advanced") and field.get("type") in ("multiselect", "number"))}
            if "models" in body or "capabilities" in body or any(
                key in submitted and submitted[key] != before["values"].get(key) for key in offering_keys
            ):
                raise DraftError(before["offering_edit_reason"], field="models")
        if not submitted and not any(key in body for key in ("models", "capabilities", "tests")) and not body.get("recheck"):
            metadata = {key: body[key] for key in ("name", "enabled") if key in body}
            if body.get("disabled"):
                metadata["enabled"] = False
            return self.store.update_service(service_id=service_id, data=metadata, actor=ctx, expected_generation=expected_generation)
        if before["unsupported_fields"]:
            if submitted or "models" in body or "capabilities" in body:
                field = next(iter(submitted), "models" if "models" in body else "capabilities")
                raise DraftError("The stored protocol has no matching definition schema for this setting.", field=field)
            metadata = {key: body[key] for key in ("name", "enabled") if key in body}
            if body.get("disabled"):
                metadata["enabled"] = False
            return self.store.update_service(service_id=service_id, data=metadata, actor=ctx, expected_generation=expected_generation)
        values = {**before["values"], **(body.get("values") if isinstance(body.get("values"), Mapping) else {})}
        lifecycle_changed = any(
            _same_text(values.get(key, _field_default(definition, key, "")))
            != _same_text(before["values"].get(key, _field_default(definition, key, "")))
            for key in _LIFECYCLE_FIELDS
        )
        if lifecycle_changed and not before["lifecycle_editable"]:
            raise DraftError("The stored process uses argument lists or a different driver. These text fields cannot edit it safely.", field="lifecycle_mode")
        values = _restore_lifecycle_markers(values, before["values"], record)
        if "api_key" not in values:
            values["api_key"] = SECRET_MASK if before["key"]["state"] == "configured" and before["key"].get("source") == "stored" else ""
        _header_credentials_are_the_owners(ctx, body.get("values"))
        clean = clean_values(definition, values, stored_headers=(record.get("config") or {}).get("headers") or ())
        _admin_limits(ctx, {**values, **clean}, definition, before=before)
        name = str(body.get("name") or before["name"] or service_id).strip()
        disabled = bool(body.get("disabled"))
        enabled = False if disabled else bool(body.get("enabled", before["enabled"]))
        moved = comparable_address(clean["address"]) != comparable_address(before["values"]["address"] or clean["address"])
        connection_changed = (
            moved
            or (values.get("api_key") not in ("", SECRET_MASK, None))
            or clean.get("api_key_env") != (before["key"].get("env_name") or "")
            or clean.get("path_prefix", "") != (before["values"].get("path_prefix") or "")
            or values.get("headers") != before["values"].get("headers")
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
            check = await run_check(catalog, definition, clean, key=key, fetch=self.fetch, existing=_existing_engines(registry, exclude=service_id),
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
        return self._write(ctx, catalog, definition, clean, service_id=service_id, name=name, capabilities=capabilities,
                           models=models, enabled=enabled, check=check_summary, tests=tests, key=key, existing=record,
                           expected_generation=expected_generation, action="update", keep=keep,
                           extra_capabilities=extra_capabilities,
                           record_capabilities=None if capabilities_sent else list(record.get("capabilities") or ()),
                           keep_lifecycle=not lifecycle_changed,
                           probe_connection_changed=connection_changed,
                           keep_timeout=_same_text(values.get("reply_timeout_s")) == _same_text(before["values"].get("reply_timeout_s")),
                           key_dropped=bool(
                               key.withheld
                               and values.get("api_key") in ("", SECRET_MASK, None)
                               and origin_of(clean["address"]) != origin_of(before["values"].get("address") or clean["address"])
                           ))

    def _write(self, ctx: Any, catalog: EngineCatalog, definition: Mapping[str, Any], values: Mapping[str, Any], *,
               service_id: str, name: str, capabilities: list[Any], models: Any, enabled: bool, check: Mapping[str, Any],
               tests: Mapping[str, Any], key: KeyInput, existing: Mapping[str, Any] | None, expected_generation: int,
               action: str, keep: Mapping[str, Any] | None = None, key_dropped: bool = False,
               keep_lifecycle: bool = False, keep_timeout: bool = False, probe_connection_changed: bool = False,
               extra_capabilities: Iterable[str] = (),
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
            raw = build_raw(catalog, definition, env_values, name=name, capabilities=capabilities, models=models,
                            enabled=enabled, check=check, tests=tests, secret_id=secret_id, keep=keep,
                            stored_models=old_config.get("models") or (), extra_capabilities=extra_capabilities,
                            record_capabilities=record_capabilities)
            if existing is None or probe_connection_changed:
                # Existing records retain their original probes until the owner
                # edits or rechecks Connection. A name or unchanged echo grants
                # no new destination permission for an inference credential.
                raw["probe_with_inference_connection"] = True
            if existing is not None:
                if keep_timeout:
                    if "timeout" in old_config:
                        raw["timeout"] = copy.deepcopy(old_config["timeout"])
                    else:
                        raw.pop("timeout", None)
                for field in ("priority", "role"):
                    if existing.get(field) is not None:
                        raw[field] = existing[field]
                for config_key in ("protocol", "native_passthrough"):
                    # How the gateway talks to an engine is set when it is added; an edit keeps it, set or unset.
                    if config_key in old_config:
                        raw[config_key] = copy.deepcopy(old_config[config_key])
                    else:
                        raw.pop(config_key, None)
            kept_key = (
                (secret_id and secret_id == old_config.get("api_key_secret_id"))
                or (raw.get("api_key_env") and raw.get("api_key_env") == old_config.get("api_key_env"))
                or (raw.get("api_key_ref") and raw.get("api_key_ref") == old_config.get("api_key_ref"))
            )
            if existing is not None and old_origin and kept_key and not origin_moved:
                # The owner bound this key to this address; an edit that keeps both keeps the binding.
                raw["credential_origin"] = old_origin
            if existing is not None and keep_lifecycle:
                # Start and stop untouched: keep the stored block exactly (other drivers, status, cwd, env).
                # The config keeps its own copy of the block; carry that copy, so neither changes.
                stored = service_registry.legacy_lifecycle_from_service(existing)
                if isinstance(stored, Mapping):
                    raw["lifecycle"] = copy.deepcopy(dict(stored))
                else:
                    raw.pop("lifecycle", None)
            elif existing is not None and "lifecycle" in raw:
                old_lifecycle = service_registry.legacy_lifecycle_from_service(existing)
                old_driver_config = old_lifecycle.get("driver_config") or {}
                for extra in ("status", "cwd", "env", "timeout_s"):
                    if extra in old_driver_config and old_lifecycle.get("driver") == "command":
                        raw["lifecycle"]["driver_config"].setdefault(extra, copy.deepcopy(old_driver_config[extra]))
            record = self.store.write_engine(service_id=service_id, raw=raw, actor=ctx, expected_generation=expected_generation,
                                             replace=existing is not None, existing=existing,
                                             drop_inference_secret_ref=key_dropped or key.source == "pasted" or bool(values.get("api_key_env")))
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
    # Both endpoints consume one budget. Configure positive integers in the
    # gateway environment; changes take effect when the routes are registered.
    probe_limits = _EngineProbeLimiter(
        max_concurrent=_probe_limit("AZIZAM_ENGINE_CHECK_MAX_CONCURRENT", 2),
        per_minute=_probe_limit("AZIZAM_ENGINE_CHECKS_PER_MINUTE", 30),
    )

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
            with probe_limits.slot(ctx):
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
            with probe_limits.slot(ctx):
                body = await read_json(request)
                result = await service.test(ctx, body)
        except (EngineRouteError, DraftError, PermissionError) as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "as_response"):
                return exc.as_response()  # type: ignore[no-any-return]
            raise
        return JSONResponse(status_code=status.HTTP_200_OK, content={"test": result})

    @router.post("/engines/access")
    async def engine_access(request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            body = await read_json(request)
            result = await asyncio.to_thread(service.access, ctx, body)
        except (EngineRouteError, DraftError) as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001
            if hasattr(exc, "as_response"):
                return exc.as_response()  # type: ignore[no-any-return]
            raise
        return JSONResponse(status_code=status.HTTP_200_OK, content={"access": result})

    @router.get("/engines/{service_id}")
    async def get_engine(service_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            view = await asyncio.to_thread(service.view, service_id, ctx)
        except EngineRouteError as exc:
            return exc.as_response()
        response = JSONResponse(status_code=status.HTTP_200_OK, content={"engine": view})
        response.headers["Cache-Control"] = "no-store"
        return response
