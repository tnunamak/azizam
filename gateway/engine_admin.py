"""Add engine routes: definitions, check, draft test, who can use the models, engine view, create and edit.

The dialog, a script and an MCP tool use the same routes. The check and the
gate run on the server: a create or an edit that changes the connection re-runs
the check and refuses on an error, or on a warning the caller did not
acknowledge, unless the engine is saved turned off.

Keys: an owner pastes a key and it goes into the provider secret store, bound
to the address it was entered for. It is write-only: the record holds a secret
id and the view shows configured metadata. Existing keys change through stable
entry commands with a revision check. A stored key is never sent to an address other than the one it
was stored for. Administrators may add keyless engines; attaching a key,
reading an environment variable and lifecycle commands stay with the owner
(r47).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
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

from gateway import log_scrub
from gateway import admin_store, category_enforcement, egress, projection, record_carriers, secret_commands, secret_store, service_registry, stt_chunking, transforms, url_binding
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
    "timeout", "headers", "probe_with_inference_connection", "api_key_secret_id", "api_key_env", "api_key_ref", "credential_origin",
    "engine_check", "engine_tests", "model_policy_schema_version",
    "name", "enabled", "capabilities", "role", "priority", "managed", "lifecycle",
})
_RECORD_KEYS = frozenset({"id", "name", "kind", "source", "enabled", "priority", "role", "capabilities", "config",
                          "secret_refs", "lifecycle", "process", "observations", "residency", "resources",
                          "connection", "offerings", "protocol_operations", "control_file_roots",
                          "provenance", "editable"})
# An engine lists at most this many models in one access question.
MAX_ACCESS_MODELS = 500
# Receipts expire with this process; their fingerprints must not allow offline guesses of a key.
_EVIDENCE_BINDING_KEY = secrets.token_bytes(32)
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
        error: dict[str, Any] = {"code": self.code, "message": projection.scrub_text(self.message)}
        if self.field:
            error["field"] = self.field
        content: dict[str, Any] = {"error": error}
        if self.check is not None:
            content["check"] = dict(self.check)
        return egress.plain_json_response(response_class="error", status_code=self.status_code, content=content, headers=self.headers)


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
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise DraftError(f"Header {index + 1} needs a name and a value.", field="headers")
        name = str(row.get("name") or "").strip()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise DraftError(f"Header {index + 1}: that is not a header name.", field="headers")
        value_text = str(row.get("value") or "")
        problem = "credential" if row.get("secret") else header_problem(name, value_text or "x")
        if problem == "transport":
            raise DraftError(f"Header {index + 1}: Azizam sets “{name}” itself.", field="headers")
        if problem == "credential":
            # Header values are stored as encrypted secret entries. A key goes in the API key field, which is stored encrypted.
            raise DraftError(f"Header {index + 1}: put a key in the API key field. Header values are stored as encrypted secret entries.",
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


def _command_value(value: Any) -> Any:
    """A command as stored: a list of arguments stays a list, a line is trimmed."""
    if isinstance(value, list):
        return list(value)
    return str(value or "").strip()


def lifecycle_block(values: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record's lifecycle block from the typed Start and stop fields; ``None`` for "leave it running"."""
    mode = str(values.get("lifecycle_mode") or _field_default(definition, "lifecycle_mode", "external"))
    if mode != "managed":
        return None
    if "lifecycle_start" in values and values["lifecycle_start"] is None:
        # The page read a command that holds a secret entry as ``null`` (it cannot be edited as text, decision D27) and sent
        # that back: the stored command is kept, which a record built without a lifecycle block does (``write_engine``).
        return None
    start = _command_value(values.get("lifecycle_start"))
    if not start:
        raise DraftError("Type the command that starts the server.", field="lifecycle_start")
    driver_config: dict[str, Any] = {"start": start}
    stop = _command_value(values.get("lifecycle_stop"))
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
    unknown = sorted(set(values) - set(fields) - {"dispatch", "protocol_description"})
    if unknown:
        raise DraftError("This kind of server has no field with one of the names sent.", field="values")
    out = {key: copy.deepcopy(value) for key, value in values.items()}
    dispatch = values.get("dispatch") or ""
    if dispatch not in ("", "protocol"):
        raise DraftError("Choose an installed protocol description or the existing route.", field="dispatch")
    if dispatch == "protocol":
        from gateway.engine_protocol import description

        protocol_id = values.get("protocol_description")
        protocols = description.load_registry()
        if not isinstance(protocol_id, str) or protocol_id not in protocols or protocols[protocol_id].executor != "http-v1":
            raise DraftError("Choose a valid installed HTTP protocol description.", field="protocol_description")

    out["address"] = normalize_address(values.get("address"), placeholder=fields["address"].get("placeholder", ""))
    if dispatch == "protocol":
        from urllib.parse import urlsplit

        endpoint = urlsplit(out["address"])
        if endpoint.username is not None or endpoint.password is not None or endpoint.query or endpoint.fragment:
            raise DraftError("Use a public protocol endpoint without URL credentials, query or fragment.", field="address")
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
    timeout_value = values.get("reply_timeout_s")
    out["reply_timeout_s"] = (None if timeout_value == ""
                              else _number(values, "reply_timeout_s", _field_default(definition, "reply_timeout_s", 600), low=1, high=3600))
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
        value = env(name)
        log_scrub.remember(value, f"env:{name}")
        return value, "env", origin
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        try:
            value = secret_store.get_provider_secret(secret_id, db_path=db_path, expected_origin=origin or None)
            log_scrub.remember(value, secret_id)
            return value, "stored", origin
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
        log_scrub.remember(value, f"env:{env_name}")
        if not value:
            raise DraftError("The variable named for the API key is not set on the gateway’s computer.", field="api_key_env")
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
    for position, cap_id in enumerate(caps, start=1):
        if cap_id not in definition["capabilities"]:
            raise DraftError(f"This kind of server does not offer the work at position {position}.", field="capabilities")
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
    if values.get("dispatch") == "protocol":
        connection = copy.deepcopy(raw.get("connection") or {})
        connection.update(dispatch="protocol", protocol=values["protocol_description"], address=values["address"], transport="http")
        raw["connection"] = connection
    elif "dispatch" in values and isinstance(raw.get("connection"), dict):
        raw["connection"].pop("dispatch", None)
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
    if "reply_timeout_s" in has and values.get("reply_timeout_s") is not None:
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


def _test_binding(definition: Mapping[str, Any], values: Mapping[str, Any], key: KeyInput,
                  service_id: str, models: Any) -> str:
    """Fingerprint all tested connection inputs and model settings without retaining credentials."""
    connection = {name: value for name, value in values.items()
                  if name not in (*_LIFECYCLE_FIELDS, "api_key")}
    snapshot = {"service_id": service_id, "definition": definition["definition"],
                "connection": connection, "credential": key.value, "withheld": key.withheld,
                "models": model_rows(models)}
    return hmac.new(_EVIDENCE_BINDING_KEY, json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _stored_evidence_binding(record: Mapping[str, Any], *, db_path: Path,
                             env: Callable[[str], str | None]) -> str:
    config = record.get("config") or {}
    names = ("host", "port", "scheme", "base_path", "address", "path_prefix", "headers", "transforms", "timeout",
             "api_key_secret_id", "api_key_env", "api_key_ref", "credential_origin", "models", "protocol", "connection")
    snapshot = {name: config[name] for name in names if name in config}
    revisions = {}
    for reference in (str(config.get("api_key_secret_id") or ""),):
        if reference:
            revisions[reference] = secret_store.provider_secret_revision(reference, db_path=db_path)
    env_name = config.get("api_key_env") or config.get("api_key_ref")
    if not env_name and (record.get("secret_refs") or {}).get("api_key") == SECRET_PLACEHOLDER:
        env_name = "AZIZAM_DOWNSTREAM_API_KEY"
    snapshot["credential_revision"] = revisions
    snapshot["environment_revision"] = env(str(env_name)) if env_name else None
    return hmac.new(_EVIDENCE_BINDING_KEY, json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


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


def _projected_engine_record(stored: Mapping[str, Any]) -> dict[str, Any]:
    return projection.project("ServiceRecord", stored).tree or {}


def _stt_chunking_view(service_id: Any, config: Mapping[str, Any]) -> dict[str, Any] | None:
    """The long-audio cutting numbers in force for a transcription record; None for a record with no ``chunking``."""
    if not isinstance(config.get("chunking"), Mapping):
        return None
    try:
        execution = stt_chunking.SttExecutionConfig.from_service(
            service_id=str(service_id or ""), service_config=config, config_directory=Path.cwd())
    except stt_chunking.SttConfigurationError as error:
        return {"invalid": str(error)}
    return execution.chunking_view()


def engine_view(catalog: EngineCatalog, stored: Mapping[str, Any], *, db_path: Path,
                hop_timeout: float = 600, env: Callable[[str], str | None] = os.environ.get) -> dict[str, Any]:
    """What the record page reads. Everything a caller sees is read from the projection of the stored record
    (``gateway/projection_types.py``): a secret shows as an entry with an id and a configured flag, never a value."""
    record = _projected_engine_record(stored)
    config = record.get("config") or {}
    stored_config = stored.get("config") if isinstance(stored.get("config"), Mapping) else {}
    definition = catalog.definition(infer_definition(catalog, record))
    assert definition is not None
    address = str(config.get("address") or service_registry.service_base_url(config) or "")
    effective_base = service_registry.service_base_url(config)
    if address and effective_base:
        proposed = _split_address(address, definition, str(config.get("path_prefix") or ""))
        if comparable_address(service_registry.service_base_url(proposed)) != comparable_address(effective_base):
            if definition["record"]["base_path"] != "from_address":
                address = origin_of(effective_base)
            else:
                prefix = "/" + str(config.get("path_prefix") or "").strip("/")
                address = effective_base[:-len(prefix)] if prefix != "/" and effective_base.endswith(prefix) else effective_base
    values: dict[str, Any] = {"address": address}
    env_name = config.get("api_key_env") or config.get("api_key_ref") or ""
    if not env_name and (stored.get("secret_refs") or {}).get("api_key") == SECRET_PLACEHOLDER and not stored_config.get("api_key_secret_id"):
        env_name = "AZIZAM_DOWNSTREAM_API_KEY"
    values["api_key_env"] = env_name
    values["path_prefix"] = config.get("path_prefix") or ""
    hop_routed = bool(config.get("gateway_lifecycle") or any(row.get("gateway_lifecycle") for row in model_rows(config.get("models"))))
    effective_timeout = config.get("timeout") or (hop_timeout if hop_routed else None)
    values["reply_timeout_s"] = effective_timeout if effective_timeout is not None else ""
    timeout_source = ("saved on this engine" if config.get("timeout") else "default for engines Azizam starts" if hop_routed else "model router default (no saved timeout)")
    # A header is an entry: ``{"id", "name", "configured"}``, or ``{"id", "name", "value", "public": true}`` when a
    # write said it is public. A value is never here for a secret header (decisions D26, D27).
    values["headers"] = [dict(h) for h in config.get("headers") or () if isinstance(h, Mapping)]
    values["transforms"] = copy.deepcopy(config.get("transforms") or [])
    lifecycle = service_registry.legacy_lifecycle_from_service(record)
    driver_config = lifecycle.get("driver_config") or {}
    if lifecycle.get("driver") == "command":
        values.update(lifecycle_mode="managed", lifecycle_start=_command_view(driver_config.get("start", "")),
                      lifecycle_stop=_command_view(driver_config.get("stop", "")),
                      lifecycle_ready_s=driver_config.get("health_timeout_s", 120))
    else:
        values["lifecycle_mode"] = "external"
    lifecycle_editable = lifecycle.get("driver") in (None, "", "command", "external")
    locked = service_registry.is_litellm_service_record(stored.get("id"), stored)
    editable = not locked
    model_use = (config.get("engine_check") or {}).get("model_use") or {}
    models = [{"id": m.get("upstream_model"), "public_name": m.get("model_group") or m.get("id"),
               "context_window": m.get("context_window"), "enabled": m.get("enabled", True),
               "use": model_use.get(m.get("upstream_model"), "")}
              for m in model_rows(config.get("models"))]
    secret_id = str(stored_config.get("api_key_secret_id") or "")
    key: dict[str, Any] = {"state": "none"}
    if env_name:
        key = {"state": "configured", "source": "env", "env_name": env_name, "origin": config.get("credential_origin") or ""}
    elif secret_id:
        key = {"state": "configured", "source": "stored", "origin": config.get("credential_origin") or ""}
    if key["state"] == "configured":
        key["entry_id"] = secret_commands.provider_key_entry_id(stored)
        if env_name:
            key["availability"] = "available" if env(str(env_name)) else "unavailable"
        else:
            try:
                key["availability"] = "unknown" if secret_store.provider_secret_status(secret_id, db_path=db_path).state == "set" else "unavailable"
            except Exception:
                key["availability"] = "unavailable"
    caps = config.get("engine_capabilities")
    if not isinstance(caps, list):
        caps = _caps_from_record(catalog, definition, record)
    field_keys = {f["key"] for f in definition["fields"]}
    schema_matches = not config.get("protocol") or config["protocol"] == definition["record"]["protocol"]
    declared_offerings = isinstance(record.get("offerings"), list) or isinstance(config.get("offerings"), list)
    unsupported_fields = {} if schema_matches else {
        key: "This saved protocol does not match the settings this form supports, so these fields are read-only."
        for key in field_keys
    }
    values = {k: v for k, v in values.items() if k in field_keys}
    values["address"] = address
    from gateway.engine_contract_view import connection_config

    connection = projection.project("EngineConnection", connection_config(stored)).tree or {}
    protocol_mode = connection.get("dispatch") == "protocol"
    if protocol_mode:
        address = str(connection.get("address") or "")
        values["address"] = address
        protocol_timeouts = connection.get("timeouts") or {}
        effective_timeout = protocol_timeouts.get("total_s", 600)
        timeout_source = "saved protocol connection" if "total_s" in protocol_timeouts else "protocol executor default"
        for field in ("headers", "reply_timeout_s", "path_prefix", "transforms"):
            unsupported_fields[field] = "Protocol dispatch uses its connection and installed description. This legacy setting is not used."
    values["dispatch"] = connection.get("dispatch") or ""
    values["protocol_description"] = connection.get("protocol") or ""
    hidden = config.get(projection.HIDDEN_KEY) if isinstance(config.get(projection.HIDDEN_KEY), int) else 0
    other = {name: value for name, value in config.items()
             if name not in _KNOWN_CONFIG_KEYS and name not in ("engine_capabilities", projection.HIDDEN_KEY)}
    request_base = inference_base_url(config)
    operations = []
    protocol_attention = []
    if protocol_mode:
        from gateway.engine_protocol import description

        doc = description.load_registry().get(connection.get("protocol"))
        if doc is None or doc.executor != "http-v1":
            protocol_attention.append({"code": "engine_protocol_unavailable",
                                       "message": "The saved protocol description is unavailable. Choose an installed description."})
        else:
            for op in doc.operations.values():
                requests = [("request", op.request)] if op.request is not None else []
                if op.job is not None:
                    requests = [("submit", op.job.submit.request), ("poll", op.job.poll.request),
                                ("fetch", op.job.fetch), ("cancel", op.job.cancel)]
                for phase, request in requests:
                    if request is not None:
                        operations.append({"capability": op.name, "method": request.method,
                                           "url": join_url(address, request.path), "class": op.op_class, "phase": phase})
    for cap_id in caps if schema_matches and not protocol_mode else ():
        route = (catalog.capability(cap_id) or {}).get("route") or {}
        if route.get("path"):
            operations.append({"capability": cap_id, "method": "POST", "url": join_url(request_base, route["path"])})
    fingerprint = _stored_evidence_binding(stored, db_path=db_path, env=env)
    url_credentials = url_binding.credential_states(stored, lambda ref: _secret_origin(ref, db_path))
    v2 = service_registry.service_record_v2(stored)
    v2["connection"]["address"] = address
    if protocol_mode:
        v2["connection"] = {"transport": "http", "headers": [], "timeouts": {"total_s": effective_timeout},
                            **copy.deepcopy(connection),
                            "credentials": {"inference": None, "control": None, **(connection.get("credentials") or {})}}
    return {
        "service_id": record.get("id"),
        "definition": definition["definition"],
        "definition_version": definition["definition_version"],
        "inferred": not config.get("definition"),
        "name": record.get("name"),
        "enabled": bool(record.get("enabled", True)),
        "locked": service_registry.is_litellm_service_record(record.get("id"), stored),
        # False when the model router does not call this engine at its own address: the page would
        # save settings the gateway does not use, so it shows them and changes only name and on/off.
        "editable": editable,
        "lifecycle_editable": lifecycle_editable,
        "values": values,
        "reply_timeout": {"seconds": effective_timeout, "source": timeout_source},
        "stt_chunking": _stt_chunking_view(record.get("id"), config),
        "attention": protocol_attention + ([{"code": "engine_credential_unavailable",
                        "message": f"Key not available: {env_name} is not set"}] if env_name and not env(str(env_name)) else [])
                      + ([{"code": "engine_credential_unavailable", "message": "The stored key is unavailable. Replace it in Connection."}]
                         if secret_id and not env_name and key.get("availability") == "unavailable" else [])
                      + ([{"code": "engine_credential_origin_mismatch",
                           "message": "The saved key is bound to a different address and will not be sent to this engine."}]
                         if key["state"] == "configured" and key.get("origin") != origin_of(address) else [])
                      + [{"code": "engine_address_needs_confirmation", "carrier": row["carrier"], "destination": row["destination"],
                          "message": (f"The credentials stored with {row['carrier']} are bound to no address that matches it, so they are not sent. "
                                      + ("Enter them again: this URL is kept whole, so there is no address to review."
                                         if row["reenter"] else
                                         f"If {row['destination']} is where they should go, the owner confirms that address; otherwise enter them again."
                                         if row["destination"] else
                                         "This field has no address they could be bound to; enter them again."))}
                         for row in url_credentials if row["state"] == "needs_confirmation"],
        "key": key,
        "url_credentials": url_credentials,
        "capabilities": caps,
        "models": models,
        "tests": {cap: copy.deepcopy(row) for cap, row in (config.get("engine_tests") or {}).items()
                  if isinstance(row, Mapping) and row.get("via") == "saved" and row.get("evidence_token")
                  and fingerprint is not None and row.get("view_fingerprint") == fingerprint},
        "check": copy.deepcopy(config.get("engine_check") or {}),
        "calls": "" if protocol_mode else call_line(definition, values),
        "other_settings": copy.deepcopy(other),
        "hidden_settings": hidden,
        "record": redacted_record(stored),
        "v2": v2,
        "request_path": {"mode": "protocol" if protocol_mode else "gateway_lifecycle" if hop_routed else "direct", "source": "service record",
                         "url": operations[0]["url"] if operations else "", "operations": operations},
        "credential_destination": (key.get("origin") or "") if key["state"] == "configured" else origin_of(address),
        "ignored_headers": [str(row.get("name") or "(unnamed)") for row in config.get("headers") or ()
                            if isinstance(row, Mapping) and row.get("public") and header_problem(str(row.get("name") or ""), str(row.get("value") or ""))],
        "unsupported_fields": unsupported_fields,
        "can_edit_offerings": schema_matches and not declared_offerings,
        "offering_edit_reason": "These models or workflows have selection settings this form cannot safely edit. Their saved settings are shown below." if declared_offerings else None,
    }


def _secret_origin(ref: str, db_path: Path) -> str | None:
    """The origin a stored secret is bound to (metadata only), or None when it has none or cannot be read."""
    try:
        return secret_store.provider_secret_origin(ref, db_path=db_path)
    except Exception:  # noqa: BLE001 - an unreadable store reads as "bound to nothing"
        return None


def _command_view(command: Any) -> Any:
    """A command as the page shows it: a line or a list of arguments, or ``None`` when part of it is a secret entry
    (the page then cannot edit it as text; the secret-entries commands change it)."""
    if isinstance(command, list) and any(not isinstance(part, str) for part in command):
        return None
    return command


def _refuse_reserved_texts(values: Mapping[str, Any]) -> None:
    """A text typed into a field that is one of the reserved marker texts stands for nothing and is refused."""
    try:
        secret_commands.refuse_reserved_texts({k: v for k, v in values.items() if k != "api_key"}, where="the engine")
    except ValueError as exc:
        raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "invalid_engine", str(exc)) from None


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


def redacted_record(stored: Mapping[str, Any]) -> dict[str, Any]:
    """The stored record as the page shows it: the projection, which shows each secret as a configured entry."""
    record = _projected_engine_record(stored)
    out = copy.deepcopy({k: v for k, v in record.items() if k in _RECORD_KEYS})
    # The page reads a stored key as the word it has always shown: configured, or nothing.
    config = out.get("config")
    if isinstance(config, dict) and isinstance(config.get("api_key_secret_id"), dict):
        config["api_key_secret_id"] = "configured" if config["api_key_secret_id"].get("configured") else ""
    if isinstance(out.get("secret_refs"), dict):
        out["secret_refs"] = {name: "configured" for name in out["secret_refs"]}
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


def _refuse_protocol_probe(values: Mapping[str, Any]) -> None:
    if values.get("dispatch") == "protocol":
        raise EngineRouteError(status.HTTP_400_BAD_REQUEST, "unsupported_protocol_probe",
                               "The legacy connection check cannot execute a protocol description. Validate the installed description and save this engine off.",
                               field="dispatch")


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
        # Opaque receipts contain only server results and secret-free fingerprints.
        # They expire on restart and oldest-first; draft tests never get receipts.
        self._test_evidence: dict[str, tuple[str, dict[str, Any]]] = {}

    @property
    def db_path(self) -> Path:
        return Path(getattr(self.store, "path"))

    def catalog(self) -> EngineCatalog:
        try:
            return self.catalog_loader()
        except EngineDefinitionError as exc:
            raise EngineRouteError(status.HTTP_503_SERVICE_UNAVAILABLE, "engine_definitions_invalid", str(exc)) from None

    def _probe_record(self, catalog: EngineCatalog, definition: Mapping[str, Any],
                      registry: Mapping[str, Any], service_id: Any) -> Mapping[str, Any] | None:
        if not service_id:
            return None
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", f"No engine called {service_id!r}.")
        config = record.get("config") or {}
        if (infer_definition(catalog, record) != definition["definition"]
                or config.get("protocol") not in (None, "", definition["record"]["protocol"])):
            raise DraftError("This protocol cannot be checked from this page yet.", field="definition")
        return record

    def _saved_test_inputs(self, catalog: EngineCatalog, definition: Mapping[str, Any], record: Mapping[str, Any],
                           ctx: Any) -> tuple[dict[str, Any], KeyInput]:
        shown = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
        values = {**shown["values"], "headers": []}
        clean = clean_values(definition, values, stored_headers=(record.get("config") or {}).get("headers") or ())
        key = resolve_key(clean, ctx, record=record, db_path=self.db_path, env=self.env)
        return clean, key

    async def check(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        registry = self.store.get_service_registry()
        service_id = body.get("service_id")
        record = self._probe_record(catalog, definition, registry, service_id)
        _header_credentials_are_the_owners(ctx, body.get("values"))
        if record is not None:
            shown = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
            values = {**shown["values"], **values}
            if values.get("headers") == shown["values"].get("headers"):
                values["headers"] = []
        _refuse_protocol_probe(values)
        clean = clean_values(definition, values, stored_headers=((record or {}).get("config") or {}).get("headers") or ())
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        if record is not None and not _is_owner(ctx):
            saved_clean, saved_key = self._saved_test_inputs(catalog, definition, record, ctx)
            models = (record.get("config") or {}).get("models") or []
            if (_test_binding(definition, clean, key, str(service_id), models)
                    != _test_binding(definition, saved_clean, saved_key, str(service_id), models)):
                raise _owner_only("Only the owner can change service settings.")
        existing = _existing_engines(registry, exclude=service_id)
        if record is not None:
            saved = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)["values"]
            if comparable_address(clean["address"]) == comparable_address(saved.get("address") or ""):
                # Checking an engine where it already is: the duplicate rule is for moving onto another's address.
                existing = []
        result = await run_check(catalog, definition, clean, key=key, fetch=self.fetch, existing=existing,
                                 viewer_role="owner" if _is_owner(ctx) else "admin")
        return projection.scrub_value(result)

    async def test(self, ctx: Any, body: Mapping[str, Any]) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        registry = self.store.get_service_registry()
        service_id = body.get("service_id")
        record = self._probe_record(catalog, definition, registry, service_id)
        _header_credentials_are_the_owners(ctx, body.get("values"))
        if record is not None:
            shown = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
            values = {**shown["values"], **values}
            if values.get("headers") == shown["values"].get("headers"):
                values["headers"] = []
        _refuse_protocol_probe(values)
        clean = clean_values(definition, values, stored_headers=((record or {}).get("config") or {}).get("headers") or ())
        key = resolve_key({**values, "address": clean["address"]}, ctx, record=record, db_path=self.db_path, env=self.env)
        stored_models = ((record or {}).get("config") or {}).get("models") or []
        if "models" in body and not isinstance(body["models"], list):
            raise DraftError("models must be a list.", field="models")
        tested_models = (_models_config(definition, clean, body["models"], stored_models)
                         if "models" in body else stored_models)
        if record is not None and not _is_owner(ctx):
            saved_clean, saved_key = self._saved_test_inputs(catalog, definition, record, ctx)
            if (_test_binding(definition, clean, key, str(service_id), tested_models)
                    != _test_binding(definition, saved_clean, saved_key, str(service_id), stored_models)):
                raise _owner_only("Only the owner can change service settings.")
        capability = str(body.get("capability") or "")
        model = str(body.get("model") or "").strip() or None
        result = await run_real_test(catalog, definition, clean, capability, key=key, fetch=self.fetch, model=model,
                                     viewer_role="owner" if _is_owner(ctx) else "admin")
        result = projection.scrub_value(result)
        request = result.get("request")
        if isinstance(request, dict) and isinstance(request.get("headers"), Mapping):
            request["headers"] = {
                name: value if name.lower() == "content-type" and value == "application/json" else "configured"
                for name, value in request["headers"].items()
            }
        result["key_sent"] = bool(key.value)
        result["key_withheld"] = key.withheld
        result["capability"] = capability
        result["model"] = model
        result["via"] = "draft"
        if record is not None:
            saved_clean, saved_key = self._saved_test_inputs(catalog, definition, record, ctx)
            binding = _test_binding(definition, clean, key, str(service_id), tested_models)
            known_model = model is None or any(row.get("upstream_model") == model for row in model_rows(stored_models))
            if known_model and binding == _test_binding(definition, saved_clean, saved_key, str(service_id), stored_models):
                result["via"] = "saved"
                token = secrets.token_urlsafe(32)
                result["evidence_token"] = token
                stored_result = {name: copy.deepcopy(result[name]) for name in
                                 ("capability", "ok", "at", "via", "seconds", "model", "evidence_token") if name in result}
                stored_result["config_fingerprint"] = binding
                stored_result["view_fingerprint"] = _stored_evidence_binding(record, db_path=self.db_path, env=self.env)
                self._test_evidence[token] = (binding, stored_result)
                if len(self._test_evidence) > 256:
                    del self._test_evidence[next(iter(self._test_evidence))]
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
        unknown = [position for position, c in enumerate(capabilities, start=1) if c not in catalog.data["capabilities"]]
        if unknown:
            raise DraftError(f"The work at position {unknown[0]} is not a kind of work Azizam knows.", field="capabilities")
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
        snapshot_reader = getattr(self.store, "get_service_registry_snapshot", None)
        if callable(snapshot_reader):
            registry, generation = snapshot_reader()
        else:
            registry, generation = self.store.get_service_registry(), None
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", "No engine has that id.")
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
        view["registry_generation"] = generation
        view["revision"] = f'"os-{generation}"' if generation is not None else None
        return view

    def configure_stt(self, ctx: Any, service_id: str, body: Any, *, expected_generation: int, apply_recommendation: bool = False) -> dict[str, Any]:
        """Change only the owner's cutting fields, through the existing registry CAS/hot-reload path."""
        from gateway.stt_measurement import SETTING_KEYS, settings_patch
        if not _is_owner(ctx):
            raise _owner_only("Only the owner can change long-audio settings.")
        patch = settings_patch(body)
        registry, generation = self.store.get_service_registry_snapshot()
        if generation != expected_generation:
            raise EngineRouteError(409, "generation_mismatch", "The registry changed; refresh and try again.")
        record = registry.get("services", {}).get(service_id)
        if record is None:
            raise EngineRouteError(404, "service_not_found", "No engine has that id.")
        if not {"transcriptions", "audio.transcriptions"}.intersection(record.get("capabilities", [])):
            raise EngineRouteError(400, "not_transcription_engine", "Choose an engine that offers transcription.")
        config = copy.deepcopy(record.get("config") or {})
        saved_chunking = config.get("chunking")
        chunking = {**(saved_chunking if isinstance(saved_chunking, Mapping) else {}), **patch}
        config["chunking"] = chunking
        try:
            if apply_recommendation:
                chunking.update(enabled=True, threshold_seconds=chunking["max_segment_seconds"])
            elif "target_seconds" in patch and "threshold_seconds" not in patch:
                threshold = chunking.get("threshold_seconds", stt_chunking.bundled_chunking_defaults()["threshold_seconds"])
                if isinstance(threshold, (int, float)) and patch["target_seconds"] >= threshold:
                    # Resolve the effective maximum without saving derived defaults.
                    # A one-second gap keeps exact-cut records (maximum == target)
                    # valid too, without exposing floating-point epsilon values.
                    target = patch["target_seconds"]
                    provisional = stt_chunking.SttExecutionConfig.from_service(
                        service_id=service_id,
                        service_config={**config, "chunking": {**chunking, "threshold_seconds": target + max(1, target * .01)}},
                        config_directory=self.db_path.parent)
                    chunking["threshold_seconds"] = max(provisional.max_segment_seconds, target + 1)
            stt_chunking.SttExecutionConfig.from_service(service_id=service_id, service_config=config, config_directory=self.db_path.parent)
        except stt_chunking.SttConfigurationError:
            raise EngineRouteError(400, "invalid_stt_settings", "The target must fit the hard maximum and all cutting limits must be valid.") from None
        result = self.store.update_service(service_id=service_id, data={"chunking": chunking}, actor=ctx, expected_generation=expected_generation)
        return {"settings": {k: chunking[k] for k in SETTING_KEYS if k in chunking}, "revision": f'"os-{result["operator_settings_generation"]}"'}

    async def create(self, ctx: Any, body: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        catalog = self.catalog()
        definition = _definition(catalog, body.get("definition"))
        values = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        name = str(body.get("name") or "").strip()
        if not name:
            raise DraftError("Give the engine a name.", field="name")
        _header_credentials_are_the_owners(ctx, body.get("values"))
        _refuse_reserved_texts(values)
        clean = clean_values(definition, values)
        _admin_limits(ctx, {**values, **clean}, definition, before=None)
        registry = self.store.get_service_registry()
        base_id = service_registry.normalize_id(re.sub(r"[^a-z0-9 _.-]+", "", name.lower())) or "engine"
        service_id, n = base_id, 2
        while service_id in (registry.get("services") or {}):
            service_id, n = f"{base_id}-{n}", n + 1
        disabled = bool(body.get("disabled"))
        if not disabled:
            _refuse_protocol_probe(clean)
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
                             tests={}, key=key, existing=None,
                             expected_generation=expected_generation, action="create")
        return {**record, "service_id": service_id}

    async def update(self, ctx: Any, service_id: str, body: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        if body.get("recheck") is True and not _is_owner(ctx):
            raise _owner_only("Only the owner can recheck service settings.")
        catalog = self.catalog()
        registry = self.store.get_service_registry()
        record = (registry.get("services") or {}).get(service_id)
        if record is None:
            raise EngineRouteError(status.HTTP_404_NOT_FOUND, "service_not_found", "No engine has that id.")
        if service_registry.is_litellm_service_record(service_id, record):
            raise EngineRouteError(status.HTTP_403_FORBIDDEN, "service_not_editable", "Azizam runs this engine itself. Only its name and Enabled can change.")
        before = engine_view(catalog, record, db_path=self.db_path, hop_timeout=self.hop_timeout(), env=self.env)
        definition = _definition(catalog, body.get("definition") or before["definition"])
        sent = body.get("values") if isinstance(body.get("values"), Mapping) else {}
        if before.get("offering_edit_reason") and ("models" in body or "capabilities" in body):
            raise DraftError(before["offering_edit_reason"], field="models")
        if not sent and not any(key in body for key in ("models", "capabilities", "tests")) and not body.get("recheck"):
            metadata = {key: body[key] for key in ("name", "enabled") if key in body}
            if body.get("disabled"):
                metadata["enabled"] = False
            return self.store.update_service(service_id=service_id, data=metadata, actor=ctx, expected_generation=expected_generation)
        if "address" in before["unsupported_fields"]:
            raise DraftError("The stored protocol has no matching definition schema for this setting.", field=next(iter(sent), "models"))
        for field, reason in before["unsupported_fields"].items():
            if field in sent and sent[field] != before["values"].get(field):
                raise DraftError(reason, field=field)
        values = {**before["values"], **sent}
        # What the page was shown for a header or a start and stop command is a read of entries, not text to save:
        # headers are changed only when the body names them, and the commands only when their text differs.
        if "headers" not in sent or sent["headers"] == before["values"].get("headers"):
            values["headers"] = []
        lifecycle_changed = any(_same_text(values.get(key)) != _same_text(before["values"].get(key)) for key in _LIFECYCLE_FIELDS)
        if not before["lifecycle_editable"] and any(
            _same_text(values.get(key)) != _same_text(before["values"].get(key))
            for key in _LIFECYCLE_FIELDS if key != "lifecycle_ready_s"
        ):
            raise DraftError("The stored process uses a different driver. These lifecycle fields cannot edit it safely.", field="lifecycle_mode")
        stored_config = record.get("config") or {}
        _refuse_reserved_texts({key: value for key, value in values.items() if value != before["values"].get(key)})
        if "api_key" not in values:
            values["api_key"] = SECRET_MASK if before["key"]["state"] == "configured" and before["key"].get("source") == "stored" else ""
        _header_credentials_are_the_owners(ctx, body.get("values"))
        clean = clean_values(definition, values, stored_headers=(record.get("config") or {}).get("headers") or ())
        _admin_limits(ctx, {**values, **clean}, definition, before=before)
        if not _is_owner(ctx):
            saved_clean, saved_key = self._saved_test_inputs(catalog, definition, record, ctx)
            current_key = resolve_key(clean, ctx, record=record, db_path=self.db_path, env=self.env)
            old_models = (record.get("config") or {}).get("models") or []
            next_models = _models_config(definition, clean, body.get("models", before["models"]), old_models)
            if (_test_binding(definition, clean, current_key, service_id, next_models)
                    != _test_binding(definition, saved_clean, saved_key, service_id, old_models)
                    or body.get("capabilities", before["capabilities"]) != before["capabilities"]):
                raise _owner_only("Only the owner can change service settings.")
        name = str(body.get("name") or before["name"] or service_id).strip()
        disabled = bool(body.get("disabled"))
        enabled = False if disabled else bool(body.get("enabled", before["enabled"]))
        saved_clean, _ = self._saved_test_inputs(catalog, definition, record, ctx)
        moved = comparable_address(clean["address"]) != comparable_address(before["values"]["address"] or clean["address"])
        connection_changed = (
            moved
            or clean.get("dispatch", "") != before["values"].get("dispatch", "")
            or (clean.get("dispatch") == "protocol" and clean.get("protocol_description") != before["values"].get("protocol_description"))
            or (values.get("api_key") not in ("", SECRET_MASK, None))
            or clean.get("api_key_env") != (before["key"].get("env_name") or "")
            or clean.get("path_prefix", "") != (before["values"].get("path_prefix") or "")
            or [(h["name"], h["value"]) for h in clean.get("headers") or ()]
               != [(h["name"], h["value"]) for h in stored_config.get("headers") or () if isinstance(h, Mapping) and "value" in h]
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
            _refuse_protocol_probe(clean)
            check = await run_check(catalog, definition, clean, key=key, fetch=self.fetch, existing=_existing_engines(registry, exclude=service_id),
                                    viewer_role="owner" if _is_owner(ctx) else "admin")
            # The check ran against the stored address; its report must not repeat what the page is not shown.
            check = projection.scrub_value(check, projection.url_secret_parts(clean["address"]))
            _gate(check, acknowledge_warnings=bool(body.get("acknowledge_warnings")))
            check_summary = _check_summary(check, address=clean["address"], disabled=False)
        elif disabled and connection_changed:
            check_summary = _check_summary(None, address=clean["address"], disabled=True)
        capabilities_sent = isinstance(body.get("capabilities"), list)
        capabilities = body.get("capabilities") if capabilities_sent else before["capabilities"]
        models = body.get("models") if isinstance(body.get("models"), list) else before["models"]
        saved_clean, saved_key = self._saved_test_inputs(catalog, definition, record, ctx)
        stored_models = (record.get("config") or {}).get("models") or []
        proposed_models = _models_config(definition, clean, models, stored_models)
        binding = _test_binding(definition, clean, key, service_id, proposed_models)
        saved_binding = _test_binding(definition, saved_clean, saved_key, service_id, stored_models)
        tests = dict(before["tests"]) if binding == saved_binding else {}
        # A caller can return a receipt, but cannot choose its result, model, timestamp or binding.
        submitted_tests = body.get("tests")
        for cap_id, row in submitted_tests.items() if isinstance(submitted_tests, Mapping) else ():
            receipt = self._test_evidence.get(str(row.get("evidence_token") or "")) if isinstance(row, Mapping) else None
            if receipt and receipt[0] == binding and cap_id in definition["capabilities"]:
                result = receipt[1]
                if result["capability"] == cap_id:
                    tests[cap_id] = copy.deepcopy(result)
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
                secret_store.put_provider_secret(
                    secret_id, key.value, db_path=self.db_path, approved_origin=origin,
                    session=getattr(ctx, "browser_session", None))
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
            elif "probe_with_inference_connection" in old_config:
                raw["probe_with_inference_connection"] = copy.deepcopy(old_config["probe_with_inference_connection"])
            if existing is not None:
                if not values.get("headers") and "headers" in old_config:
                    raw["headers"] = copy.deepcopy(old_config["headers"])
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
            if raw.get("api_key_secret_id") or raw.get("api_key_env") or raw.get("api_key_ref"):
                # The page's key selection grants inference use at this origin.
                # A key withheld after a host move must not get a new binding.
                if not key.withheld:
                    raw.setdefault("credential_origin", origin)
                secret_commands.bind_protocol_inference(raw)
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
        return egress.plain_json_response(response_class="engine_definitions", status_code=status.HTTP_200_OK, content=payload)

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
        return egress.plain_json_response(response_class="engine_check", status_code=status.HTTP_200_OK, content={"check": result})

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
        return egress.plain_json_response(response_class="engine_check", status_code=status.HTTP_200_OK, content={"test": result})

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
        return egress.plain_json_response(response_class="engine_check", status_code=status.HTTP_200_OK, content={"access": result})

    @router.get("/engines/{service_id}")
    async def get_engine(service_id: str, request: Request) -> JSONResponse:
        ctx = await asyncio.to_thread(guard, request)
        if isinstance(ctx, JSONResponse):
            return ctx
        try:
            view = await asyncio.to_thread(service.view, service_id, ctx)
        except EngineRouteError as exc:
            return exc.as_response()
        response = egress.projected_response(status_code=status.HTTP_200_OK, content={"engine": view})
        response.headers["Cache-Control"] = "no-store"
        return response
