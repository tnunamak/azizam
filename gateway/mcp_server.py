"""MCP server for the gateway: media meta-tools and optional owner operator commands.

Transport: Streamable HTTP at one endpoint (``POST /mcp``). Protocol: the current
revision (2026-07-28, stateless: every request carries its version and
identity in ``_meta``, no ``initialize``, no session) plus the handshake-based
revisions 2025-03-26 to 2025-11-25, which Claude Code and most SDKs still speak.
Legacy clients get a session-less ``initialize`` answer (we never mint
``Mcp-Session-Id``); ``GET`` and ``DELETE`` return 405 because the server has
no stream to offer. Responses are always plain JSON. 2026-07-28 requests must
carry their version and client capabilities in ``_meta`` (400/-32602 if not)
and do not have ``initialize`` or ``ping`` (404/-32601). JSON-RPC batches are
2025-03-26 only.

Authorization: every request is authenticated by ``McpDeps.authenticate``,
which accepts an API key or an OAuth access token issued for this endpoint
(gateway/mcp_oauth.py). A 401 carries an RFC 9728 ``resource_metadata``
challenge, and both protected-resource metadata documents name the
authorization server, so any spec-following client can sign in. Browser
``Origin`` headers must be on an explicit list.

Tools (about 200 standing tokens each for the media tools, generated from discovery, never one tool
per model):

* ``capabilities``: list or describe workflows, stable aliases and operations
  with typed inputs. Generated from ``x-azizam-workflows``.
* ``run``: start a workflow or an OpenAI-style media operation. Returns
  the result, or a job id when the work outlives ``wait_seconds``.
* ``job``: poll, wait for, or cancel a job; fetches the result.
* ``view_asset``: view a reduced preview of one of the caller's image assets,
  optionally cropped in original pixel coordinates.
* ``stt_measurement`` (when wired): owner long-audio settings and measurement jobs.
* ``rename_app`` and ``rename_key``: give one of the signed-in person's own Apps or keys a new name. They
  work for a client the person signed in (an OAuth connection) whose consent included renaming (the grant's
  ``mcp:rename`` scope), never for an App key, and change a label only: no new key, no new access.
* ``requests`` and ``retention``: read or delete personal request history, and read or set its retention choice.

The module owns no business logic. Authentication, profile policy, receipts and
job storage stay in the core routes: every action is dispatched in process to
those routes with the caller's own credentials (``McpDeps.call``). Media comes
back as signed asset links, with optional bounded image previews. A feature
the core cannot do returns an explicit tool error.
"""
from __future__ import annotations

from gateway import run_tag
from gateway.media_jobs import ERROR_FIELDS, PHASE_FIELDS, is_terminal

import asyncio
import base64
import binascii
import copy
import io
import json
import re
import time
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import quote, urlsplit

from PIL import Image, ImageOps

from gateway.problem_report_tool import INSTRUCTION as REPORT_INSTRUCTION, REPORT_HINT, TOOL as REPORT_TOOL
from gateway.stt_samples import RIGHTS_CAVEAT

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway.workflow_call import (
    BuiltRequest, CallError, asset_id_in, build_request, effective_schema, error_from, kind_for, sniff,
    validate_inputs,
)

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)

OLDEST_VERSION = LEGACY_VERSIONS[-1]

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

# JSON-RPC and MCP error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

SERVER_NAME = "azizam"
INSTRUCTIONS = (
    "Media generation gateway. Call `capabilities` to see workflows and aliases "
    "(image, video, music, sfx, speech), `capabilities` with `target` for typed "
    "inputs, `run` to generate or transcribe, `job` to poll slow work, and `view_asset` to inspect "
    "a reduced image preview. Results are signed URLs. "
    "`rename_app` and `rename_key` rename the signed-in person's own apps and keys. "
    + REPORT_INSTRUCTION
)

DEFAULT_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 120
SYNC_TIMEOUT_SECONDS = 300.0
MAX_ASSET_BYTES = 512 * 1024 * 1024
MCP_PREVIEW_SOURCE_MAX_BYTES = 32 * 1024 * 1024
MCP_PREVIEW_MAX_PIXELS = 32_000_000
MCP_PREVIEW_MAX_SIDE = 768
MCP_PREVIEW_MAX_BYTES = 192 * 1024
MCP_PREVIEW_TOTAL_IMAGE_BYTES = 512 * 1024
MCP_PREVIEW_MAX_COUNT = 4
MCP_PREVIEW_MAX_CANDIDATES = 8

@dataclass(frozen=True)
class McpTarget:
    """Immutable wire descriptor; JSON fields thaw to fresh objects for each call."""
    target: str
    intent: str
    operation: str
    method: str
    path: str
    body: str
    mode: str
    description: str
    summary: str
    input_schema_json: str
    defaults_json: str
    forced_json: str
    outputs_json: str
    response_kind: str
    accepted_modes: tuple[str, ...]
    filter_by_intent: bool
    choices_json: str
    file_input_json: str


@dataclass(frozen=True)
class McpJobProfile:
    family: str
    poll: tuple[str, str]
    content: tuple[str, str]
    cancel: tuple[str, str]
    wrap_intent: bool


@dataclass(frozen=True)
class McpRegistry:
    targets: tuple[McpTarget, ...]
    jobs: tuple[McpJobProfile, ...]


def _mcp_object(value: Any, label: str, allowed: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label}: expected an object")
    if allowed is not None and set(value) - allowed:
        raise ValueError(f"{label}: unknown fields {sorted(set(value) - allowed)}")
    return value


def _mcp_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: expected nonempty text")
    return value


def _mcp_path(value: Any, label: str) -> str:
    value = _mcp_text(value, label)
    if not value.startswith("/") or value.startswith("//") or any(char.isspace() or char in "?#" for char in value):
        raise ValueError(f"{label}: expected a local route path")
    return value


def _mcp_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def compile_mcp_registry(document: Mapping[str, Any]) -> McpRegistry:
    """Compile only inert frontend data; no imports, handlers or target-specific branches."""
    records = document.get("records")
    if not isinstance(records, list):
        raise ValueError("protocol frontends: expected records")
    targets: list[McpTarget] = []
    jobs: list[McpJobProfile] = []
    names: set[str] = set()
    families: set[str] = set()
    metadata_keys = {
        "target", "mode", "description", "summary", "input_schema", "defaults", "outputs",
        "response_kind", "accepted_modes", "filter_by_intent", "forced", "choices", "file_input",
    }
    for record in records:
        record = _mcp_object(record, "frontend")
        if record.get("enabled") is False:
            continue
        for raw in record.get("mcp_jobs") or []:
            raw = _mcp_object(raw, "MCP job profile", {"family", "poll", "content", "cancel", "wrap_intent"})
            family = _mcp_text(raw.get("family"), "job family")
            if family in families:
                raise ValueError(f"duplicate MCP job family {family!r}")
            routes = []
            for action in ("poll", "content", "cancel"):
                route = _mcp_object(raw.get(action), f"{family}.{action}", {"method", "path"})
                method = _mcp_text(route.get("method"), "job method")
                if method not in (("GET",) if action != "cancel" else ("POST", "DELETE")):
                    raise ValueError(f"{family}.{action}: unsupported method")
                path = _mcp_path(route.get("path"), "job path")
                if path.count("{job_id}") != 1 or re.search(r"[{}]", path.replace("{job_id}", "")):
                    raise ValueError(f"{family}.{action}: expected one job_id placeholder")
                routes.append((method, path))
            if type(raw.get("wrap_intent", False)) is not bool:
                raise ValueError(f"{family}.wrap_intent: expected boolean")
            jobs.append(McpJobProfile(family, *routes, raw.get("wrap_intent", False)))
            families.add(family)
        for row in record.get("registered_routes") or []:
            row = _mcp_object(row, "registered route")
            meta, reason = row.get("mcp"), row.get("mcp_not_exposed")
            if meta is not None and reason is not None:
                raise ValueError("a route cannot be both exposed and not exposed over MCP")
            if meta is None:
                if record.get("protocol") == "openai":
                    _mcp_text(reason, f"{row.get('canonical')}: mcp_not_exposed")
                continue
            meta = _mcp_object(meta, "MCP target", metadata_keys)
            target = _mcp_text(meta.get("target"), "MCP target")
            if target in names:
                raise ValueError(f"duplicate MCP target {target!r}")
            methods = row.get("methods")
            if not isinstance(methods, list) or len(methods) != 1 or methods[0] not in ("GET", "POST"):
                raise ValueError(f"{target}: expected one supported HTTP method")
            body = row.get("body", "json")
            if body not in ("json", "multipart") or meta.get("mode") != "sync":
                raise ValueError(f"{target}: unsupported body or mode")
            response_kind = meta.get("response_kind")
            if response_kind not in ("binary_asset", "json_result"):
                raise ValueError(f"{target}: unsupported response kind")
            schema = _mcp_object(meta.get("input_schema"), f"{target}.input_schema")
            properties = _mcp_object(schema.get("properties"), f"{target}.properties")
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError(f"{target}: expected a closed object input schema")
            required = schema.get("required", [])
            if not isinstance(required, list) or any(name not in properties for name in required):
                raise ValueError(f"{target}: required input is not declared")
            defaults = _mcp_object(meta.get("defaults", {}), f"{target}.defaults")
            forced = _mcp_object(meta.get("forced", {}), f"{target}.forced")
            if (set(defaults) | set(forced)) - set(properties):
                raise ValueError(f"{target}: default or forced input is not declared")
            outputs = meta.get("outputs")
            modes = meta.get("accepted_modes", ["auto", "sync"])
            if not isinstance(outputs, list) or not isinstance(modes, list) or not modes or any(mode not in ("auto", "sync", "async") for mode in modes):
                raise ValueError(f"{target}: invalid outputs or accepted modes")
            choices = _mcp_object(meta.get("choices", {}), f"{target}.choices")
            if choices:
                _mcp_object(choices, f"{target}.choices", {"name", "method", "path", "default_field", "items_field", "id_field"})
                if choices.get("method") != "GET":
                    raise ValueError(f"{target}: choices must be a read")
                _mcp_path(choices.get("path"), f"{target}.choices.path")
                for key in ("name", "default_field", "items_field", "id_field"):
                    _mcp_text(choices.get(key), f"{target}.choices.{key}")
            file_input = _mcp_object(meta.get("file_input", {}), f"{target}.file_input")
            if file_input:
                _mcp_object(file_input, f"{target}.file_input", {"field", "asset_prefix", "media_prefix", "max_bytes"})
                if body != "multipart" or file_input.get("field") not in required:
                    raise ValueError(f"{target}: multipart file must be a required input")
                for key in ("asset_prefix", "media_prefix"):
                    _mcp_text(file_input.get(key), f"{target}.file_input.{key}")
                maximum = file_input.get("max_bytes")
                if type(maximum) is not int or not 0 < maximum <= MAX_ASSET_BYTES:
                    raise ValueError(f"{target}: invalid file byte limit")
            elif body == "multipart":
                raise ValueError(f"{target}: multipart requires a file input")
            filter_by_intent = meta.get("filter_by_intent", True)
            if type(filter_by_intent) is not bool:
                raise ValueError(f"{target}: filter_by_intent must be boolean")
            targets.append(McpTarget(
                target, _mcp_text(row.get("intent"), "route intent"),
                _mcp_text(row.get("operation"), "route operation"), methods[0],
                _mcp_path(row.get("canonical"), "route path"), body, meta["mode"],
                _mcp_text(meta.get("description"), "description"), _mcp_text(meta.get("summary"), "summary"),
                _mcp_json(schema), _mcp_json(defaults), _mcp_json(forced), _mcp_json(outputs),
                response_kind, tuple(modes), filter_by_intent, _mcp_json(choices), _mcp_json(file_input),
            ))
            names.add(target)
    if not jobs:
        raise ValueError("MCP job profiles are not declared")
    return McpRegistry(tuple(targets), tuple(jobs))


@lru_cache(maxsize=1)
def load_mcp_registry() -> McpRegistry:
    path = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"
    return compile_mcp_registry(yaml.safe_load(path.read_text(encoding="utf-8")))


def _mcp_operation_body(descriptor: McpTarget, inputs: Mapping[str, Any], model: str | None) -> dict[str, Any]:
    defaults = json.loads(descriptor.defaults_json)
    body = {"model": model or inputs.get("model") or defaults.get("model"),
            **{key: value for key, value in inputs.items() if key != "model"}}
    if body["model"] is None:
        body.pop("model")
    for key, value in defaults.items():
        body.setdefault(key, value)
    body.update(json.loads(descriptor.forced_json))
    return body


async def _mcp_file(deps: McpDeps, request: Request, auth: Any, value: Any,
                    spec: Mapping[str, Any]) -> tuple[str, bytes, str]:
    """Resolve only an owned full asset ID or bounded inline data, never a path/URL."""
    maximum = spec["max_bytes"]
    if not isinstance(value, str):
        raise CallError("file must be an owned asset id or a bounded data URI")
    if re.fullmatch(re.escape(spec["asset_prefix"]) + r"[0-9a-f]{64}", value):
        raw = await asyncio.to_thread(deps.read_asset, request, auth, value)
        if raw is None:
            raise CallError("the asset is missing or is not owned by this caller")
        hint = ""
    elif value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        match = re.fullmatch(r"data:([A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+);base64", header)
        if not separator or not match or not match[1].startswith(spec["media_prefix"]):
            raise CallError("file must be a base64 data URI of the declared media kind")
        if len(encoded) > ((maximum + 2) // 3) * 4:
            raise CallError("inline file exceeds the byte limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise CallError("file contains invalid base64") from None
        hint = match[1]
    else:
        raise CallError("file must be an owned asset id or a bounded data URI; paths and URLs are not accepted")
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        raise CallError("file is empty or exceeds the byte limit")
    media_type, suffix = sniff(raw, hint)
    # Container magic is ambiguous (for example audio-only ISO-BMFF), and some
    # supported audio formats have no magic. The owned namespace / URI declares
    # the input kind; the HTTP route remains responsible for audio validation.
    if hint:
        media_type = hint
    elif not media_type.startswith(spec["media_prefix"]):
        media_type = "application/octet-stream"
    return ("input" + suffix, raw, media_type)

@dataclass
class McpDeps:
    authenticate: Callable[[Request, str], Any]
    # (request, method, path, kwargs: json=, data=, files=, params=) -> httpx.Response
    call: Callable[..., Awaitable[Any]]
    catalog: Callable[[], list[dict[str, Any]]]
    aliases: Callable[[], Mapping[str, Any]]
    # (request, auth, content, kind "img|aud|vid", suffix) -> {"asset_id", "url", "media_type", "size_bytes"}
    store_asset: Callable[[Request, Any, bytes, str, str], dict[str, Any]]
    # (request, auth, asset_id) -> bytes, or None when missing or not the caller's
    read_asset: Callable[[Request, Any, str], bytes | None]
    public_base_url: Callable[[Request], str | None]
    # Optional bounded read for image previews. Like read_asset, it must check ownership.
    read_preview_asset: Callable[[Request, Any, str], bytes | None] | None = None
    server_version: str = "1"
    # Browser origins that may call the endpoint (DNS-rebinding defence). The
    # request's own Host is not evidence: a rebinding page controls it.
    allowed_origins: Callable[[Request], list[str]] = lambda _request: []
    poll_interval: float = 1.0
    # (request, auth, entry) -> whether this caller may see the entry. An entry is a
    # workflow summary ({"id", "intent"}) or a speech name ({"id", "operation": "speech"}).
    # The default shows everything; the gateway passes the same check discovery uses.
    visible: Callable[[Request, Any, Mapping[str, Any]], bool] = lambda _request, _auth, _entry: True
    # OAuth (RFC 9728): issuers listed in the protected-resource metadata and
    # the scopes the 401 challenge asks for. Empty means API keys only.
    authorization_servers: Callable[[Request], list[str]] = lambda _request: []
    scopes: tuple[str, ...] = ()
    stt_measurement: Callable[..., Awaitable[dict]] | None = None
    # The owner-only operator tool is omitted from discovery unless this check passes.
    owner_visible: Callable[[Request, Any], bool] = lambda _request, _auth: False
    # (request, auth, tool name, arguments) -> the result, or a ``CallError`` the model can act on. The gateway
    # decides who may rename what; ``None`` means this host offers no renaming.
    rename: Callable[[Request, Any, str, Mapping[str, Any]], dict[str, Any]] | None = None
    # (request, workflow ids) -> {id: {"available", "reason", "last_healthy_at"}} from the gateway's engine health record
    # (``gateway.availability.workflow_availability``). A failure leaves the rows without it; ``None`` means this host has none.
    availability: Callable[[Request, list[str]], Awaitable[Mapping[str, Mapping[str, Any]]]] | None = None
    retention_ledger: Callable[[Request, Any, str | None], dict[str, Any] | None] | None = None
    retention_visible: Callable[[Request, Any], bool] = lambda _request, _auth: False
    # ``report_problem`` (gateway/problem_reports.py): (request, auth, arguments) -> the reply, or a ``CallError``.
    # ``None`` means this host takes no reports and the tool is not listed. ``note_failure`` is told about each failed
    # ``run`` or ``job`` so a later report can carry it (request, auth, facts).
    problem_report: Callable[[Request, Any, Mapping[str, Any]], Awaitable[dict[str, Any]]] | None = None
    note_failure: Callable[[Request, Any, Mapping[str, Any]], None] | None = None


# ------------------------------------------------------------------ JSON-RPC


def rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _decode_header_value(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?"):-2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return value
    return value


def _normal_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.scheme and parts.netloc else ""


def _origin_allowed(request: Request, allowed: list[str]) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # not a browser
    origin = _normal_origin(origin)
    return bool(origin) and origin in {_normal_origin(value) for value in allowed}


# ---------------------------------------------------------------- tool schemas

TOOLS: list[dict[str, Any]] = [
    {
        "name": "capabilities",
        "title": "List gateway capabilities",
        "description": (
            "List the media workflows, stable aliases (image, video, music, sfx, ...) and operations "
            "(speech and transcription) this gateway can run. Pass `target` for one entry's typed inputs, outputs and "
            "expected run time. Pass `query` to filter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Workflow id, short id, alias, or an operation target. Returns its typed inputs."},
                "query": {"type": "string", "description": "Case-insensitive filter on id, alias, intent and description."},
                "intent": {"type": "string", "description": "Filter by intent, e.g. image.generate, video.generate, audio.generate."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "run",
        "title": "Run a workflow or media operation",
        "description": (
            "Run a workflow, stable alias or operation. `inputs` follow the schema from `capabilities` "
            "with `target`. Fast work returns the result. Slow work (video, long music) returns a "
            "`job_id` after `wait_seconds`; call `job` to continue. Media comes back as signed URLs; "
            "available image assets also have reduced image previews. "
            "Image and video inputs take a data URI or an asset id from an earlier result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Workflow id, short id, alias, or an operation target from capabilities."},
                "inputs": {"type": "object", "description": "Typed inputs for the target. See `capabilities`."},
                "mode": {"type": "string", "enum": ["auto", "sync", "async"], "default": "auto"},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": DEFAULT_WAIT_SECONDS,
                                 "description": "How long to wait for a job before returning its id."},
                "tag": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9._:-]+$",
                        "description": "Optional label kept with this request. `requests` echoes it and can filter by it."},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "job",
        "title": "Poll, wait for or cancel a job",
        "description": "Get the status of a `job_id` from `run`, optionally waiting; returns the result when done. `cancel` stops it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "job_profile": {"type": "string", "description": "Declared lifecycle profile returned by run or job; pass it back unchanged."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 0},
                "cancel": {"type": "boolean", "default": False},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "view_asset",
        "title": "View an image asset",
        "description": (
            "View a reduced preview of an image asset from an earlier result. Pass its asset_id, never a URL. "
            "An optional crop is [left, top, right, bottom] in original pixels; right and bottom are exclusive. "
            "The preview is limited to 768 pixels per side and 192 KiB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "minLength": 68, "maxLength": 68,
                             "pattern": "^img_[0-9a-f]{64}$",
                             "description": "Image asset id returned by this gateway."},
                "crop": {"type": "array", "minItems": 4, "maxItems": 4,
                         "items": {"type": "integer", "minimum": 0},
                         "description": "Optional [left, top, right, bottom] box in original pixels; right and bottom are exclusive."},
            },
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "rename_app",
        "title": "Rename one of your apps",
        "description": (
            "Give one of the signed-in person's own apps a new name. Only the name changes: its keys and access stay."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "The app's current name (or its id if two share a name)."},
                "name": {"type": "string", "minLength": 1, "maxLength": 80, "description": "The new name."},
            },
            "required": ["app", "name"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "rename_key",
        "title": "Rename one of your keys",
        "description": (
            "Give an active key of one of the signed-in person's own apps a new name. Only the label changes: the key "
            "still works and no new key is made."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "The app's current name (or its id)."},
                "key": {"type": "string", "description": "The key's current name (or its id if two share a name)."},
                "name": {"type": "string", "minLength": 1, "maxLength": 80, "description": "The new name."},
            },
            "required": ["app", "key", "name"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]

TOOLS.append({
    "name": "stt_measurement", "title": "Long-audio operator settings and measurement",
    "description": "Owner only (MCP OAuth). Configure saved cutting settings independently, or fetch public human-transcribed samples and measure through the gateway. Start returns a durable job id; status reports progress, deletions by piece length, seam excess versus noise and a recommendation. Cancel stops work. Apply is an explicit separate action; configure/apply require the current registry revision from options. " + RIGHTS_CAVEAT,
    "inputSchema": {"type": "object", "properties": {
        "service_id": {"type": "string"}, "action": {"type": "string", "enum": ["options", "configure", "start", "status", "cancel", "apply"]},
        "job_id": {"type": "string"}, "revision": {"type": "string"},
        "inputs": {"type": "object", "properties": {"sample_ids": {"type": "array", "items": {"type": "string"}},
            "lengths": {"type": "array", "items": {"type": "number"}}, "search_widths": {"type": "array", "items": {"type": "number"}}, "model": {"type": "string"}}, "additionalProperties": False},
        "settings": {"type": "object", "properties": {"target_seconds": {"type": "number", "exclusiveMinimum": 0},
            "search_seconds": {"type": "number", "minimum": 0}, "max_segment_seconds": {"type": "number", "exclusiveMinimum": 0},
            "native_long_audio_offline": {"type": "boolean"},
            "native_long_audio_streaming": {"type": "boolean"}}, "additionalProperties": False},
    }, "required": ["service_id", "action"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
})

TOOLS.append({
    "name": "requests", "title": "Read or delete your request history",
    "description": "List, read or delete your own request history. Actions: list, get, delete, delete_all.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "get", "delete", "delete_all"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "cursor": {"type": "string"},
        "tag": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9._:-]+$",
                "description": "With list: only requests that carried this run tag."},
        "record_id": {"type": "string", "minLength": 1, "maxLength": 128},
    }, "required": ["action"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
})

TOOLS.append({
    "name": "retention", "title": "Read or set your request history setting",
    "description": "Read your request history setting, or set it when this connection has privacy permission. "
                   "Actions: mine_get, mine_set.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["mine_get", "mine_set"]},
        "keep": {"type": "boolean"},
        "retention": {"type": "string"},
        "parts": {"type": "array", "items": {"type": "string"}},
    }, "required": ["action"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False},
})

TOOLS.append(REPORT_TOOL)

# ------------------------------------------------------------------ result I/O


def _asset(item: Mapping[str, Any], name: str) -> dict[str, Any]:
    out = {k: item[k] for k in ("asset_id", "url", "media_type", "size_bytes") if item.get(k) is not None}
    media_type = str(out.get("media_type") or "")
    out["kind"] = media_type.split("/")[0] if media_type else item.get("kind", "file")
    out["name"] = name
    return out


@dataclass(frozen=True)
class ImagePreview:
    media_type: str
    content: bytes
    source_width: int
    source_height: int
    width: int
    height: int
    crop: tuple[int, int, int, int] | None


def _reduced_image_preview(raw: bytes, media_type: str,
                           crop: tuple[int, int, int, int] | None = None) -> ImagePreview | None:
    """Return a metadata-free, size-bounded raster preview, preserving transparency."""
    if len(raw) > MCP_PREVIEW_SOURCE_MAX_BYTES or not media_type.lower().startswith("image/"):
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}:
                    return None
                raw_width, raw_height = source.size
                if raw_width <= 0 or raw_height <= 0 or raw_width * raw_height > MCP_PREVIEW_MAX_PIXELS:
                    return None
                source.seek(0)
                source.load()
                has_alpha = "A" in source.getbands() or "transparency" in source.info
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                if crop is not None:
                    left, top, right, bottom = crop
                    if right > width or bottom > height:
                        raise CallError("crop is outside the image's original pixel bounds",
                                        image_width=width, image_height=height)
                cropped = oriented.crop(crop) if crop is not None else oriented
                preview_source = cropped.convert("RGBA" if has_alpha else "RGB")

                for side in (MCP_PREVIEW_MAX_SIDE, 512, 384, 256):
                    preview = preview_source.copy()
                    preview.thumbnail((side, side), Image.Resampling.LANCZOS)
                    qualities = (None,) if has_alpha else (72, 58)
                    for quality in qualities:
                        output = io.BytesIO()
                        if has_alpha:
                            preview.save(output, format="PNG", optimize=True)
                            result_type = "image/png"
                        else:
                            preview.save(output, format="JPEG", quality=quality, optimize=True,
                                         progressive=False, subsampling=2)
                            result_type = "image/jpeg"
                        encoded = output.getvalue()
                        if len(encoded) <= MCP_PREVIEW_MAX_BYTES:
                            return ImagePreview(result_type, encoded, width, height,
                                                preview.width, preview.height, crop)
    except CallError:
        raise
    except Exception:  # noqa: BLE001 - previews are optional; keep the signed asset link on any decode failure.
        return None
    return None


async def _image_preview_blocks(deps: McpDeps, request: Request, auth: Any,
                                payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Read only caller-owned generated image assets and make bounded MCP image blocks."""
    if payload.get("status") != "succeeded":
        return []
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return []

    blocks: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    candidates = 0
    response_bytes = 0
    preview_count = 0
    for asset in assets:
        if not isinstance(asset, Mapping) or not str(asset.get("media_type") or "").lower().startswith("image/"):
            continue
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or asset_id_in(asset_id) != asset_id or asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        candidates += 1
        if candidates > MCP_PREVIEW_MAX_CANDIDATES or preview_count >= MCP_PREVIEW_MAX_COUNT:
            break
        claimed_size = asset.get("size_bytes")
        if isinstance(claimed_size, int) and not isinstance(claimed_size, bool) and claimed_size > MCP_PREVIEW_SOURCE_MAX_BYTES:
            continue
        if deps.read_preview_asset is None:
            return []
        try:
            raw = await asyncio.to_thread(deps.read_preview_asset, request, auth, asset_id)
            if not isinstance(raw, bytes):
                continue
            preview = await asyncio.to_thread(_reduced_image_preview, raw, str(asset.get("media_type") or ""))
        except Exception:  # noqa: BLE001 - optional previews must not hide the existing asset link.
            continue
        if preview is None:
            continue
        if response_bytes + len(preview.content) > MCP_PREVIEW_TOTAL_IMAGE_BYTES:
            continue
        blocks.append({"type": "text", "text": f"Reduced preview for asset {asset_id}."})
        blocks.append({"type": "image", "data": base64.b64encode(preview.content).decode("ascii"),
                       "mimeType": preview.media_type})
        preview_count += 1
        response_bytes += len(preview.content)
    return blocks


def _view_asset_crop(value: Any) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(edge, bool) or not isinstance(edge, int) for edge in value)):
        raise CallError("`crop` must be four integer pixel coordinates: [left, top, right, bottom]")
    left, top, right, bottom = value
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise CallError("`crop` must have non-negative left/top coordinates and positive width/height")
    return left, top, right, bottom


async def _view_asset(deps: McpDeps, request: Request, auth: Any,
                      args: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    unknown = sorted(set(args) - {"asset_id", "crop"})
    if unknown:
        raise CallError(f"unknown argument {unknown[0]!r}")
    asset_id = args.get("asset_id")
    if (not isinstance(asset_id, str) or len(asset_id) != 68
            or asset_id_in(asset_id) != asset_id or not asset_id.startswith("img_")):
        raise CallError("`asset_id` must be an image asset id returned by this gateway")
    crop = _view_asset_crop(args.get("crop"))
    if deps.read_preview_asset is None:
        raise CallError("bounded image previews are unavailable on this gateway")
    try:
        raw = await asyncio.to_thread(deps.read_preview_asset, request, auth, asset_id)
    except Exception:  # noqa: BLE001 - don't disclose asset store details.
        raw = None
    if not isinstance(raw, bytes):
        raise CallError("the image is missing, unavailable to this caller, or too large to preview")
    preview = await asyncio.to_thread(_reduced_image_preview, raw, "image/*", crop)
    if preview is None:
        raise CallError("the image could not be previewed within the supported format and size limits")
    payload: dict[str, Any] = {
        "status": "succeeded",
        "asset_id": asset_id,
        "preview": {
            "media_type": preview.media_type,
            "width": preview.width,
            "height": preview.height,
            "source_width": preview.source_width,
            "source_height": preview.source_height,
            "crop": list(preview.crop) if preview.crop is not None else None,
        },
    }
    block = {"type": "image", "data": base64.b64encode(preview.content).decode("ascii"),
             "mimeType": preview.media_type}
    return payload, block


async def _assets_from_response(deps: McpDeps, request: Request, auth: Any, resp: Any, stem: str) -> tuple[list[dict[str, Any]], Any]:
    """Assets (and any non-media JSON) from a core response."""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, list) and data and all(isinstance(i, Mapping) for i in data):
            assets: list[dict[str, Any]] = []
            for index, item in enumerate(data):
                name = f"{stem}-{index + 1}"
                if item.get("b64_json"):
                    raw = base64.b64decode(item["b64_json"])
                    media_type, suffix = sniff(raw)
                    stored = await asyncio.to_thread(deps.store_asset, request, auth, raw, kind_for(media_type) or "img", suffix)
                    assets.append(_asset(stored, name + suffix))
                elif item.get("url") or item.get("content_url"):
                    asset_id = item.get("asset_id") or asset_id_in(str(item.get("url") or item.get("content_url")))
                    assets.append(_asset({**item, "url": item.get("url") or item.get("content_url"), "asset_id": asset_id}, name))
            if assets:
                return assets, None
        return [], payload
    raw = resp.content
    if len(raw) > MAX_ASSET_BYTES:
        raise CallError("the result is larger than this adapter will hand out", size_bytes=len(raw))
    media_type, suffix = sniff(raw, ctype)
    kind = kind_for(media_type)
    if not kind:
        raise CallError(f"the gateway returned an unexpected content type: {ctype or 'none'}")
    stored = await asyncio.to_thread(deps.store_asset, request, auth, raw, kind, suffix)
    return [_asset(stored, stem + suffix)], None


_STATUS_MAP = {"queued": "queued", "running": "running", "in_progress": "running", "processing": "running",
               "succeeded": "succeeded", "completed": "succeeded", "failed": "failed",
               "canceled": "canceled", "cancelled": "canceled"}


def _job_profile(family: str) -> McpJobProfile:
    for profile in load_mcp_registry().jobs:
        if profile.family == family:
            return profile
    raise CallError("unknown job profile; use the profile returned by run or job")


def _job_paths(family: str, job_id: str) -> dict[str, tuple[str, str]]:
    profile = _job_profile(family)
    encoded = quote(job_id, safe="")
    return {action: (method, path.format(job_id=encoded))
            for action in ("poll", "content", "cancel")
            for method, path in (getattr(profile, action),)}


def _normalize_job(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_status = str(payload.get("status") or "queued")
    err = payload.get("error")
    if isinstance(err, Mapping):
        err = err.get("message") or err.get("code")
    return {
        "status": _STATUS_MAP.get(raw_status, raw_status),
        "progress": payload.get("progress"),
        "error": str(err) if err else None,
    }


# ------------------------------------------------------------------- the tools


class Tools:
    def __init__(self, deps: McpDeps, request: Request, auth: Any) -> None:
        self.deps, self.request, self.auth = deps, request, auth
        self._tag: str | None = None
        self.registry = load_mcp_registry()
        self._job_profiles: dict[str, str] = {}

    # -- resolution -----------------------------------------------------

    def _operation_names(self, descriptor: McpTarget) -> list[str]:
        section = (self.deps.aliases().get("operations") or self.deps.aliases()).get(descriptor.operation) or {}
        return [str(item["alias"]) for item in section.get("stable") or [] if item.get("alias")
                and self.deps.visible(self.request, self.auth,
                                      {"id": str(item["alias"]), "operation": descriptor.operation})]

    def _catalog(self) -> list[dict[str, Any]]:
        """The workflows this caller may see: what discovery lists, and no hidden ones."""
        return [entry for entry in self.deps.catalog()
                if self.deps.visible(self.request, self.auth, {"id": entry["id"], "intent": entry["intent"]})]

    def resolve(self, target: str, inputs: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any] | McpTarget | None, str | None]:
        """(kind, workflow/operation descriptor, selected model alias).

        An alias can name a generate and an edit workflow (``image``). The caller's
        inputs pick the one that accepts them all; ties go to the generate workflow.
        """
        t = target.strip()
        for descriptor in self.registry.targets:
            if t == descriptor.target or t in self._operation_names(descriptor):
                return "operation", descriptor, None if t == descriptor.target else t
        entries = self._catalog()
        matches = [e for e in entries if t in (e["id"], e["short_id"]) or t in (e.get("aliases") or [])]
        if matches:
            def rank(entry: Mapping[str, Any]) -> tuple[int, int]:
                props = effective_schema(entry).get("properties") or {}
                accepts = all(name in props for name in (inputs or {}))
                return (0 if accepts else 1, 0 if entry["intent"].endswith(".generate") else 1)
            return "workflow", min(matches, key=rank), None
        names = sorted({e["short_id"] for e in entries} | {a for e in entries for a in e.get("aliases") or []})
        raise CallError(f"unknown target {target!r}; call `capabilities` to list valid targets",
                        targets=names + [name for descriptor in self.registry.targets
                                         for name in (descriptor.target, *self._operation_names(descriptor))])

    # -- capabilities -----------------------------------------------------

    async def capabilities(self, args: Mapping[str, Any]) -> dict[str, Any]:
        target = args.get("target")
        if target:
            kind, entry, model = self.resolve(str(target))
            if kind == "operation":
                assert isinstance(entry, McpTarget)
                detail: dict[str, Any] = {
                    "target": entry.target, "intent": entry.intent, "aliases": self._operation_names(entry),
                    "description": entry.description, "inputs": json.loads(entry.input_schema_json),
                    "outputs": json.loads(entry.outputs_json), "mode": entry.mode,
                }
                choices = json.loads(entry.choices_json)
                if choices:
                    try:
                        resp = await self.deps.call(self.request, choices["method"], choices["path"])
                        if resp.status_code == 200:
                            listing = resp.json()
                            detail[choices["name"]] = {
                                "default": listing.get(choices["default_field"]),
                                "ids": [item.get(choices["id_field"]) for item in listing.get(choices["items_field"]) or []],
                            }
                    except Exception:  # noqa: BLE001 - optional discovery supplement
                        pass
                return detail
            return self._describe(entry)
        query = str(args.get("query") or "").lower()
        intent = str(args.get("intent") or "")
        rows = []
        entries = self._catalog()
        health = await self._availability([entry["id"] for entry in entries])
        for entry in entries:
            hay = " ".join([entry["id"], entry.get("description") or "", entry["intent"], *(entry.get("aliases") or [])]).lower()
            if query and query not in hay or intent and entry["intent"] != intent:
                continue
            rows.append({
                "target": entry["short_id"], "intent": entry["intent"], "aliases": entry.get("aliases") or [],
                "description": (entry.get("description") or "")[:160],
                "typical_seconds": entry.get("typical_seconds"), "mode": self._mode_for(entry, "auto"),
                "availability": health.get(entry["id"]),
                "status": entry.get("status") or "current", "superseded_by": entry.get("superseded_by"),
            })
        for descriptor in self.registry.targets:
            if query and query not in descriptor.target.lower():
                continue
            if intent and (not descriptor.filter_by_intent or intent != descriptor.intent):
                continue
            rows.append({"target": descriptor.target, "intent": descriptor.intent,
                         "aliases": self._operation_names(descriptor), "description": descriptor.summary,
                         "typical_seconds": None, "mode": descriptor.mode})
        # An agent asks "what should I use": everything to choose today comes before the workflows it replaced.
        rows.sort(key=lambda row: row.get("status") == "superseded")
        return {"workflows": rows, "hint": (
            "Call capabilities with `target` for typed inputs; run with `target` and `inputs`. "
            "Speech and transcription also run over the OpenAI-shaped routes. "
            "GET /health at the gateway root is the public health route."
        )}

    async def _availability(self, workflow_ids: list[str]) -> Mapping[str, Mapping[str, Any]]:
        """Each workflow's availability from the host's engine health record; empty when the host has none or it fails."""
        if self.deps.availability is None:
            return {}
        try:
            return await self.deps.availability(self.request, workflow_ids)
        except Exception:  # noqa: BLE001 - availability is a nicety; the listing must not fail because of it
            return {}

    def _describe(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        execution = entry.get("execution") or {}
        return {
            "target": entry["short_id"], "id": entry["id"], "intent": entry["intent"],
            "display_name": entry.get("display_name"), "description": entry.get("description"),
            "aliases": entry.get("aliases") or [], "typical_seconds": entry.get("typical_seconds"),
            "status": entry.get("status") or "current", "superseded_by": entry.get("superseded_by"),
            "mode": self._mode_for(entry, "auto"),
            "modes_available": [m for m in ("sync", "async") if execution.get(m)],
            "inputs": effective_schema(entry), "outputs": entry.get("outputs") or [],
        }

    @staticmethod
    def _mode_for(entry: Mapping[str, Any], requested: str) -> str:
        execution = entry.get("execution") or {}
        if requested in ("sync", "async"):
            if not execution.get(requested):
                raise CallError(f"mode {requested!r} is not available for {entry['short_id']}; "
                                f"available: {[m for m in ('sync', 'async') if execution.get(m)]}")
            return requested
        return execution.get("recommended") or ("sync" if execution.get("sync") else "async")

    # -- run ------------------------------------------------------------

    async def run(self, args: Mapping[str, Any]) -> dict[str, Any]:
        target = args.get("target")
        if not isinstance(target, str) or not target:
            raise CallError("`target` is required")
        inputs = args.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            raise CallError("`inputs` must be an object")
        mode = args.get("mode") or "auto"
        if mode not in ("auto", "sync", "async"):
            raise CallError("`mode` must be auto, sync or async")
        wait = self._wait(args.get("wait_seconds"), DEFAULT_WAIT_SECONDS)
        self._tag = self._run_tag(args.get("tag"))
        kind, entry, model = self.resolve(target, inputs)
        if kind == "operation":
            assert isinstance(entry, McpTarget)
            return await self._operation(entry, inputs, model, mode)
        assert entry is not None
        validate_inputs(effective_schema(entry), inputs)
        chosen = self._mode_for(entry, mode)
        execution = entry["execution"]
        stem = entry["short_id"]
        if chosen == "sync":
            route = execution["sync"]
            built = await build_request(self.deps.read_asset, self.request, self.auth, entry, route, inputs, wrap_intent=None)
            resp = await self._send(built, SYNC_TIMEOUT_SECONDS)
            if resp.status_code >= 400:
                raise error_from(resp)
            assets, result = await _assets_from_response(self.deps, self.request, self.auth, resp, stem)
            job_id = result.get("job_id") if isinstance(result, Mapping) and not assets else None
            if isinstance(job_id, str) and job_id:  # the route answered with a job, not the output: wait as an async run does
                return await self._await(job_id, wait, entry)
            return self._done(entry, assets, result)
        route = execution["async"]
        profile = _job_profile(route["kind"])
        wrap = route["submit"]["body"]["intent"] if profile.wrap_intent else None
        submit = {**route["submit"], "fields": route["fields"], "content_type": route["submit"].get("content_type", "")}
        built = await build_request(self.deps.read_asset, self.request, self.auth, entry, submit, inputs, wrap_intent=wrap)
        resp = await self._send(built, 60.0)
        if resp.status_code >= 400:
            raise error_from(resp)
        payload = resp.json()
        job_id = payload.get("job_id") or payload.get("id")
        if not job_id:
            raise CallError("the gateway accepted the job but returned no id")
        return await self._await(str(job_id), wait, entry, profile.family)

    async def _operation(self, descriptor: McpTarget, inputs: Mapping[str, Any],
                         model: str | None, mode: str) -> dict[str, Any]:
        if mode not in descriptor.accepted_modes:
            raise CallError(f"mode {mode!r} is not available for {descriptor.target}; available: {[descriptor.mode]}")
        validate_inputs(json.loads(descriptor.input_schema_json), inputs)
        body = _mcp_operation_body(descriptor, inputs, model)
        if descriptor.body == "multipart":
            file_input = json.loads(descriptor.file_input_json)
            field = file_input["field"]
            upload = await _mcp_file(self.deps, self.request, self.auth, body.pop(field), file_input)
            data = {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in body.items()}
            built = BuiltRequest(descriptor.method, descriptor.path, data=data, files=[(field, upload)])
        else:
            built = BuiltRequest(descriptor.method, descriptor.path, json=body)
        resp = await self._send(built, SYNC_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            raise error_from(resp)
        if descriptor.response_kind == "binary_asset":
            assets, _ = await _assets_from_response(self.deps, self.request, self.auth, resp, descriptor.target)
            return {"status": "succeeded", "target": descriptor.target, "assets": assets}
        try:
            result = resp.json()
        except (ValueError, TypeError):
            raise CallError("the gateway returned a non-JSON result for this operation") from None
        return {"status": "succeeded", "target": descriptor.target, "assets": [], "result": result}

    async def _send(self, built: BuiltRequest, timeout: float) -> Any:
        kwargs: dict[str, Any] = {}
        if built.json is not None:
            kwargs["json"] = built.json
        if built.data is not None:
            kwargs["data"] = built.data
        if built.files:
            kwargs["files"] = built.files
        if built.headers or self._tag:
            kwargs["headers"] = {**(built.headers or {}), **({run_tag.TAG_HEADER: self._tag} if self._tag else {})}
        try:
            return await asyncio.wait_for(self.deps.call(self.request, built.method, built.path, **kwargs), timeout)
        except asyncio.TimeoutError:
            raise CallError("the gateway did not answer in time; retry with mode=async if the workflow offers it") from None

    @staticmethod
    def _run_tag(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return run_tag.validate(value)
        except run_tag.InvalidTag as exc:
            raise CallError(f"`tag` is not valid: {exc}") from None

    @staticmethod
    def _wait(value: Any, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WAIT_SECONDS:
            raise CallError(f"`wait_seconds` must be an integer from 0 to {MAX_WAIT_SECONDS}")
        return value

    @staticmethod
    def _done(entry: Mapping[str, Any] | None, assets: list[dict[str, Any]], result: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"status": "succeeded", "target": entry["short_id"] if entry else None, "assets": assets}
        if result is not None:
            out["result"] = result
        return out

    # -- jobs -------------------------------------------------------------

    async def _poll(self, job_id: str, family: str | None = None) -> dict[str, Any]:
        family = family or self._job_profiles.get(job_id)
        if family is not None:
            method, path = _job_paths(family, job_id)["poll"]
            resp = await self.deps.call(self.request, method, path)
            if resp.status_code >= 400:
                raise error_from(resp)
        else:
            # Old callers have only an opaque id. Resolve by authenticated reads,
            # never by id spelling, and never choose between colliding families.
            matches = []
            missing = None
            for profile in self.registry.jobs:
                method, path = _job_paths(profile.family, job_id)["poll"]
                candidate = await self.deps.call(self.request, method, path)
                if candidate.status_code == 404:
                    missing = candidate
                    continue
                if candidate.status_code >= 400:
                    raise error_from(candidate)
                matches.append((profile.family, candidate))
            if len(matches) > 1:
                raise CallError("job id is ambiguous; supply the declared job_profile returned by run")
            if not matches:
                if missing is not None:
                    raise error_from(missing)
                raise CallError("no declared job profiles are available")
            family, resp = matches[0]
        self._job_profiles[job_id] = family
        payload = resp.json()
        return {**_normalize_job(payload), "raw": payload}

    async def _await(self, job_id: str, wait: int, entry: Mapping[str, Any] | None,
                     family: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + wait
        state = await self._poll(job_id, family)
        while not is_terminal(state["status"]) and time.monotonic() < deadline:
            await asyncio.sleep(min(self.deps.poll_interval, max(0.0, deadline - time.monotonic())))
            state = await self._poll(job_id)
        out: dict[str, Any] = {"status": state["status"], "job_id": job_id,
                               "job_profile": self._job_profiles[job_id]}
        out.update({name: state["raw"].get(name) for name in (*PHASE_FIELDS, "attempt_number", "previous_attempts", "queue_position")})
        if entry:
            out["target"] = entry["short_id"]
        elif state["raw"].get("workflow"):
            out["target"] = str(state["raw"]["workflow"]).split(".")[-1]
        if state["progress"] is not None:
            out["progress"] = state["progress"]
        if state["status"] == "succeeded":
            try:
                out["assets"] = await self._job_assets(job_id, out.get("target") or "result")
            except CallError as exc:
                # The work is done and kept; only handing it over failed. Say so, or an
                # agent reads a refusal at ~90% as a failed render and starts again.
                raise CallError(
                    f"The render finished, but the gateway refused to deliver it: {exc} "
                    f"The result is kept: call `job` with job_id {job_id} to fetch it again; do not start the render over.",
                    **{**exc.details, "stage": "delivery", "job_status": "succeeded", "job_id": job_id},
                ) from exc
        elif state["status"] in ("failed", "canceled"):
            out["error"] = state["error"] or state["status"]
            # Who refused: the gateway (an access plan, inputs, capacity) or the upstream work.
            out.update({name: state["raw"][name] for name in (*ERROR_FIELDS, "last_healthy_at") if state["raw"].get(name) is not None})
        else:
            out["next"] = "Call `job` with this job_id to keep waiting (set wait_seconds)."
            out["next_arguments"] = {"job_id": job_id, "job_profile": self._job_profiles[job_id]}
        return out

    async def _job_assets(self, job_id: str, stem: str) -> list[dict[str, Any]]:
        method, path = _job_paths(self._job_profiles[job_id], job_id)["content"]
        resp = await self.deps.call(self.request, method, path)
        if resp.status_code >= 400:
            raise error_from(resp)
        assets, _ = await _assets_from_response(self.deps, self.request, self.auth, resp, stem)
        return assets

    async def job(self, args: Mapping[str, Any]) -> dict[str, Any]:
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise CallError("`job_id` is required")
        wait = self._wait(args.get("wait_seconds"), 0)
        family = args.get("job_profile")
        if family is not None:
            if not isinstance(family, str):
                raise CallError("job_profile must be text")
            _job_profile(family)
        if args.get("cancel"):
            await self._poll(job_id, family)  # resolve ownership and ambiguity before any mutation
            method, path = _job_paths(self._job_profiles[job_id], job_id)["cancel"]
            resp = await self.deps.call(self.request, method, path)
            if resp.status_code >= 400:
                raise error_from(resp)
            return await self._await(job_id, 0, None)
        return await self._await(job_id, wait, None, family)


    async def rename(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """``rename_app`` and ``rename_key``: the gateway renames; this adapter only checks the arguments are text."""
        fields = ("app", "name") if tool == "rename_app" else ("app", "key", "name")
        for field in fields:
            if not isinstance(args.get(field), str) or not args[field].strip():
                raise CallError(f"`{field}` is required")
        unknown = sorted(set(args) - set(fields))
        if unknown:
            raise CallError(f"unknown argument {unknown[0]!r}")
        if self.deps.rename is None:
            raise CallError("this gateway does not offer renaming over MCP")
        return await asyncio.to_thread(self.deps.rename, self.request, self.auth, tool, dict(args))


def tool_result(payload: Mapping[str, Any], *, is_error: bool = False, version: str = MODERN_VERSION,
                image_previews: list[dict[str, str]] | None = None,
                image_preview_note: str | None = None) -> dict[str, Any]:
    """MCP tool result: JSON text for any client, structured content, links for media.

    2025-03-26 has neither ``structuredContent`` nor ``resource_link`` (both
    arrived in 2025-06-18), but standard image blocks work in every supported
    version. Errors lead with a plain message; JSON preserves details in every era.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}]
    if is_error:
        content.insert(0, {"type": "text", "text": str(payload.get("error") or "Tool execution failed")})
    if image_previews:
        content.extend(image_previews)
        content.append({"type": "text", "text": image_preview_note or
                        "Only the labelled images are included as reduced previews; other assets are links only."})
    elif payload.get("status") == "succeeded" and any(
        isinstance(asset, Mapping) and str(asset.get("media_type") or "").lower().startswith("image/")
        for asset in (payload.get("assets") or [])
    ):
        content.append({"type": "text", "text": "No image pixels are included in this result; only asset links are available."})
    if version == OLDEST_VERSION:
        result: dict[str, Any] = {"content": content}
    else:
        for asset in payload.get("assets") or []:
            link: dict[str, Any] = {"type": "resource_link", "uri": asset["url"], "name": asset.get("name") or "asset"}
            if asset.get("media_type"):
                link["mimeType"] = asset["media_type"]
            if asset.get("size_bytes") is not None:
                link["size"] = asset["size_bytes"]
            content.append(link)
        result = {"content": content, "structuredContent": dict(payload)}
    if is_error:
        result["isError"] = True
    return result


async def _history_tool(deps: McpDeps, request: Request, auth: Any, name: str, args: Mapping[str, Any],
                       version: str) -> dict[str, Any]:
    """Dispatch personal request-history actions to the same routes as REST clients."""
    action = args.get("action")
    method: str
    path: str
    kwargs: dict[str, Any] = {}
    deleted_record: str | None = None

    if name == "requests":
        if action == "ledger":
            if (deps.retention_ledger is None or not deps.retention_visible(request, auth)
                    or set(args) - {"action", "request_id"}):
                return tool_result({"status": "error", "error": "The retention ledger is unavailable for this caller."},
                                   is_error=True, version=version)
            request_id = args.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 128):
                return tool_result({"status": "error", "error": "request_id must be a saved request id."},
                                   is_error=True, version=version)
            try:
                view = await asyncio.to_thread(deps.retention_ledger, request, auth, request_id)
            except PermissionError:
                view = None
            return tool_result({"ledger": view} if view is not None else
                               {"status": "error", "error": "Ledger not found."},
                               is_error=view is None, version=version)

        if action == "list":
            if set(args) - {"action", "limit", "cursor", "tag"}:
                return _tool_argument_error("list accepts only limit, cursor and tag", version)
            limit = args.get("limit", 20)
            cursor = args.get("cursor")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                return _tool_argument_error("limit must be an integer from 1 to 100", version)
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                return _tool_argument_error("cursor must be a non-empty cursor string", version)
            tag = args.get("tag")
            if tag is not None:
                try:
                    run_tag.validate(tag)
                except run_tag.InvalidTag as exc:
                    return _tool_argument_error(str(exc), version)
            method, path = "GET", "/gateway/v1/requests"
            kwargs["params"] = {"limit": limit, **({"cursor": cursor} if cursor is not None else {}),
                                **({"tag": tag} if tag is not None else {})}
        elif action in {"get", "delete"}:
            if set(args) != {"action", "record_id"}:
                return _tool_argument_error(f"{action} requires only record_id", version)
            record_id = args.get("record_id")
            if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
                return _tool_argument_error("record_id must be a saved request record id", version)
            method = "GET" if action == "get" else "DELETE"
            path = f"/gateway/v1/requests/{quote(record_id, safe='')}"
            deleted_record = record_id if action == "delete" else None
        elif action == "delete_all":
            if set(args) != {"action"}:
                return _tool_argument_error("delete_all accepts only action", version)
            method, path = "DELETE", "/gateway/v1/requests"
        else:
            return _tool_argument_error("action must be list, get, delete, delete_all or an available ledger action",
                                        version)
    elif name == "retention":
        if action == "mine_get":
            if set(args) != {"action"}:
                return _tool_argument_error("mine_get accepts only action", version)
            method, path = "GET", "/gateway/v1/me/retention"
        elif action == "mine_set":
            if set(args) != {"action", "keep", "retention", "parts"}:
                return _tool_argument_error("mine_set requires keep, retention and parts", version)
            keep, period, parts = args.get("keep"), args.get("retention"), args.get("parts")
            if not isinstance(keep, bool) or not isinstance(period, str) or not isinstance(parts, list) \
                    or any(not isinstance(part, str) for part in parts):
                return _tool_argument_error("mine_set requires a boolean keep, string retention and string parts", version)
            method, path = "PUT", "/gateway/v1/me/retention"
            kwargs["json"] = {"keep": keep, "retention": period, "parts": parts}
        else:
            return _tool_argument_error("action must be mine_get or mine_set", version)
    else:  # pragma: no cover - caller only routes the two history tools here
        return _tool_argument_error("unknown history tool", version)

    try:
        response = await deps.call(request, method, path, **kwargs)
    except Exception:  # noqa: BLE001 - MCP must not leak a traceback or internal message.
        return tool_result({"status": "error", "error": "the gateway could not complete this history action"},
                           is_error=True, version=version)
    if response.status_code == 204:
        payload: Mapping[str, Any] = {"deleted": deleted_record} if deleted_record else {"status": "succeeded"}
    else:
        try:
            value = response.json()
            payload = value if isinstance(value, Mapping) else {"result": value}
        except (ValueError, TypeError):
            payload = {"status": "error", "error": "the gateway returned an invalid history response"}
    return tool_result(payload, is_error=response.status_code >= 400, version=version)


def _tool_argument_error(message: str, version: str) -> dict[str, Any]:
    return tool_result({"status": "error", "error": message}, is_error=True, version=version)


def _with_report_hint(deps: McpDeps, request: Request, auth: Any, tool: str, args: Mapping[str, Any],
                      payload: dict[str, Any], error: Any, code: Any, category: Any, http_status: Any) -> dict[str, Any]:
    """Tell the gateway about a failed ``run`` or ``job`` and put the reminder in the failure the agent is reading."""
    if deps.problem_report is None:
        return payload
    if deps.note_failure is not None:
        try:
            deps.note_failure(request, auth, {
                "tool": tool, "target": payload.get("target") or args.get("target"),
                "job_id": payload.get("job_id") or args.get("job_id"), "error": error, "code": code,
                "category": category, "http_status": http_status, "retryable": payload.get("retryable")})
        except Exception:  # noqa: BLE001 - noting a failure must never change the failure
            pass
    return {**payload, "report_hint": REPORT_HINT}


async def call_tool(deps: McpDeps, request: Request, auth: Any, name: str, args: Mapping[str, Any],
                    version: str = MODERN_VERSION) -> dict[str, Any]:
    tools = Tools(deps, request, auth)
    if name in {"requests", "retention"}:
        return await _history_tool(deps, request, auth, name, args, version)
    if name == "stt_measurement":
        if deps.stt_measurement is None:
            return tool_result({"status": "error", "error": "Long-audio operator commands are unavailable."}, is_error=True, version=version)
        try:
            return tool_result(await deps.stt_measurement(request, auth, args), version=version)
        except CallError as error:
            return tool_result({"status": "error", "error": str(error), **error.details}, is_error=True, version=version)
    if name == REPORT_TOOL["name"]:
        if deps.problem_report is None:
            return tool_result({"status": "error", "error": "This gateway does not take problem reports."},
                               is_error=True, version=version)
        try:
            return tool_result(await deps.problem_report(request, auth, args), version=version)
        except CallError as exc:
            return tool_result({"status": "error", "error": str(exc), **exc.details}, is_error=True, version=version)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            print(f"[mcp] tool {name} failed: {type(exc).__name__}", flush=True)
            return tool_result({"status": "error", "error": "the gateway could not take this report"},
                               is_error=True, version=version)
    if name == "view_asset":
        try:
            payload, preview = await _view_asset(deps, request, auth, args)
            return tool_result(payload, version=version, image_previews=[preview],
                               image_preview_note="Reduced image preview. Crop coordinates refer to original pixels; source bytes are not included in text.")
        except CallError as exc:
            return tool_result({"status": "error", "error": str(exc), **exc.details},
                               is_error=True, version=version)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback or asset-store detail to the client.
            print(f"[mcp] tool view_asset failed: {type(exc).__name__}: {exc}", flush=True)
            return tool_result({"status": "error", "error": "the adapter could not preview this image"},
                               is_error=True, version=version)
    handler = {"capabilities": tools.capabilities, "run": tools.run, "job": tools.job,
               "rename_app": lambda a: tools.rename("rename_app", a), "rename_key": lambda a: tools.rename("rename_key", a)}[name]
    try:
        # Both run and job expose generation failures to the caller as errors.
        # A canceled job is a valid outcome, including an explicit cancel request.
        payload = await handler(args)
        previews = await _image_preview_blocks(deps, request, auth, payload)
        if payload.get("status") == "failed":
            payload = _with_report_hint(deps, request, auth, name, args, payload, payload.get("error"),
                                        payload.get("error_code"), payload.get("error_category"), None)
        return tool_result(payload, is_error=payload.get("status") == "failed", version=version,
                           image_previews=previews)
    except CallError as exc:
        payload = {"status": "error", "error": str(exc), **exc.details}
        if exc.details.get("http_status") is not None:  # the core refused or failed it; a bad argument is not reported
            payload = _with_report_hint(deps, request, auth, name, args, payload, str(exc), exc.details.get("code"),
                                        None, exc.details.get("http_status"))
        return tool_result(payload, is_error=True, version=version)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
        print(f"[mcp] tool {name} failed: {type(exc).__name__}: {exc}", flush=True)
        return tool_result({"status": "error", "error": "the adapter hit an unexpected error; see the gateway log"},
                           is_error=True, version=version)


# ------------------------------------------------------------------- transport


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


def _bad_request(mid: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    return _json(400, rpc_error(mid, code, message, data))


def _validate_modern(request: Request, msg: Mapping[str, Any]) -> JSONResponse | None:
    """2026-07-28: every request carries its version and capabilities in ``_meta``
    and mirrors the version, method and tool name in headers."""
    mid = msg.get("id")
    params = msg.get("params")
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    if not isinstance(meta, Mapping):
        return _bad_request(mid, INVALID_PARAMS, "Invalid params: _meta is required")
    version = meta.get(META_VERSION)
    if not isinstance(version, str) or not version:
        return _bad_request(mid, INVALID_PARAMS, f"Invalid params: _meta.{META_VERSION} is required")
    if not isinstance(meta.get(META_CAPABILITIES), Mapping):
        return _bad_request(mid, INVALID_PARAMS, f"Invalid params: _meta.{META_CAPABILITIES} is required")
    header_version = request.headers.get("mcp-protocol-version")
    if version != header_version:
        return _bad_request(mid, HEADER_MISMATCH,
                            f"Header mismatch: MCP-Protocol-Version {header_version!r} does not match _meta {version!r}")
    if version != MODERN_VERSION:
        return _bad_request(mid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                            {"supported": list(SUPPORTED_VERSIONS), "requested": version})
    if request.headers.get("mcp-method") != msg.get("method"):
        return _bad_request(mid, HEADER_MISMATCH,
                            f"Header mismatch: Mcp-Method {request.headers.get('mcp-method')!r} does not match body {msg.get('method')!r}")
    if msg.get("method") == "tools/call":
        name = (params or {}).get("name")
        header = request.headers.get("mcp-name")
        if header is None or _decode_header_value(header) != name:
            return _bad_request(mid, HEADER_MISMATCH, f"Header mismatch: Mcp-Name {header!r} does not match body {name!r}")
    return None


def _valid_envelope(msg: Any) -> bool:
    return (isinstance(msg, Mapping) and msg.get("jsonrpc") == "2.0" and isinstance(msg.get("method"), str)
            and ("params" not in msg or isinstance(msg["params"], Mapping))
            and ("id" not in msg or (isinstance(msg["id"], (str, int)) and not isinstance(msg["id"], bool))))


async def handle_message(deps: McpDeps, request: Request, auth: Any, msg: Mapping[str, Any], version: str) -> dict[str, Any] | None:
    """One JSON-RPC message under ``version``. ``None`` means a notification (no response)."""
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}
    modern = version == MODERN_VERSION
    if "id" not in msg:
        return None
    if method == "ping" and not modern:
        result: dict[str, Any] = {}
    elif method == "initialize" and not modern:
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": deps.server_version},
            "instructions": INSTRUCTIONS,
        }
    elif method == "server/discover" and modern:
        result = {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": INSTRUCTIONS, "ttlMs": 3600000, "cacheScope": "public",
        }
    elif method == "tools/list":
        owner_can_measure = (deps.stt_measurement is not None
                             and deps.owner_visible(request, auth))
        can_read_ledger = deps.retention_ledger is not None and deps.retention_visible(request, auth)
        listed_tools = []
        for tool in TOOLS:
            if tool["name"] == "stt_measurement" and not owner_can_measure:
                continue
            if tool["name"] == REPORT_TOOL["name"] and deps.problem_report is None:
                continue
            if tool["name"] == "requests" and can_read_ledger:
                # Keep the old operator ledger action available only to callers who can read it.
                tool = copy.deepcopy(tool)
                tool["description"] += " The ledger action shows dated facts about where request content may remain."
                tool["inputSchema"]["properties"]["action"]["enum"].append("ledger")
                tool["inputSchema"]["properties"]["request_id"] = {
                    "type": "string", "description": "Optional saved request id; omit for today's facts.",
                }
            listed_tools.append(tool)
        result = {"tools": listed_tools}
        if modern:
            result.update(ttlMs=300000, cacheScope="private")
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
            return rpc_error(mid, INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments", {})
        if not isinstance(args, Mapping):
            return rpc_error(mid, INVALID_PARAMS, "arguments must be an object")
        result = await call_tool(deps, request, auth, name, args, version)
    else:
        return rpc_error(mid, METHOD_NOT_FOUND, f"Method not found: {method}")
    if modern:
        meta = {**(result.get("_meta") or {}), "io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": deps.server_version}}
        result = {"resultType": "complete", **result, "_meta": meta}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def bearer_challenge(metadata_url: str, scopes: tuple[str, ...], *, error: str | None = None,
                     description: str | None = None) -> str:
    """RFC 6750 challenge with the RFC 9728 ``resource_metadata`` pointer."""
    parts = []
    if error:
        parts.append(f'error="{_quoted(error)}"')
    if description:
        parts.append(f'error_description="{_quoted(description)}"')
    parts.append(f'resource_metadata="{_quoted(metadata_url)}"')
    if scopes:
        parts.append(f'scope="{_quoted(" ".join(scopes))}"')
    return "Bearer " + ", ".join(parts)


def register_mcp_routes(app: FastAPI, deps: McpDeps, *, path: str = "/mcp") -> None:
    def base(request: Request) -> str:
        return (deps.public_base_url(request) or "").rstrip("/")

    def metadata_url(request: Request) -> str:
        return f"{base(request)}/.well-known/oauth-protected-resource{path}"

    async def dispatch(request: Request, auth: Any, body: Any) -> Response:
        header_version = request.headers.get("mcp-protocol-version")
        if header_version is not None and header_version not in SUPPORTED_VERSIONS:
            mid = body.get("id") if isinstance(body, Mapping) else None
            return _bad_request(mid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                                {"supported": list(SUPPORTED_VERSIONS), "requested": header_version})
        if isinstance(body, list):
            # JSON-RPC batches exist only in 2025-03-26, whose clients send no
            # version header.
            if header_version not in (None, OLDEST_VERSION) or not body:
                return _bad_request(None, INVALID_REQUEST, "Invalid Request: batches are not supported")
            replies = []
            for msg in body:
                if not _valid_envelope(msg):
                    replies.append(rpc_error(msg.get("id") if isinstance(msg, Mapping) else None, INVALID_REQUEST, "Invalid Request"))
                    continue
                reply = await handle_message(deps, request, auth, msg, OLDEST_VERSION)
                if reply:
                    replies.append(reply)
            return _json(200, replies) if replies else Response(status_code=202)
        if not _valid_envelope(body):
            return _bad_request(body.get("id") if isinstance(body, Mapping) else None, INVALID_REQUEST, "Invalid Request")
        params = body.get("params") or {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), Mapping) else {}
        modern = header_version == MODERN_VERSION or META_VERSION in meta
        if "id" not in body:  # notification
            return Response(status_code=202)
        if modern:
            failure = _validate_modern(request, body)
            if failure is not None:
                return failure
            version = MODERN_VERSION
        elif body["method"] == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        else:
            # 2025-06-18 and later send the version header after initialize;
            # a request without one is 2025-03-26.
            version = header_version or OLDEST_VERSION
        reply = await handle_message(deps, request, auth, body, version)
        status = 404 if modern and reply and reply.get("error", {}).get("code") == METHOD_NOT_FOUND else 200
        return _json(status, reply)

    @app.post(path, include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        if not _origin_allowed(request, deps.allowed_origins(request)):
            return _json(403, rpc_error(None, INVALID_REQUEST, "Origin not allowed"))
        auth = await asyncio.to_thread(deps.authenticate, request, path)
        if isinstance(auth, Response):
            if auth.status_code == 401:
                presented = bool(request.headers.get("authorization"))
                auth.headers["WWW-Authenticate"] = bearer_challenge(
                    metadata_url(request), deps.scopes,
                    error="invalid_token" if presented else None,
                    description="The access token is missing, expired or revoked" if presented else None,
                )
            return auth
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return _bad_request(None, PARSE_ERROR, "Parse error")
        return await dispatch(request, auth, body)

    @app.api_route(path, methods=["GET", "DELETE"], include_in_schema=False)
    async def mcp_no_stream(request: Request) -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    async def protected_resource(request: Request) -> JSONResponse:
        root = base(request)
        document: dict[str, Any] = {
            "resource": f"{root}{path}",
            "bearer_methods_supported": ["header"],
            "resource_name": SERVER_NAME,
            "resource_documentation": f"{root}/llms.txt",
        }
        servers = deps.authorization_servers(request)
        if servers:
            document["authorization_servers"] = servers
        if deps.scopes:
            document["scopes_supported"] = list(deps.scopes)
        return JSONResponse(document, headers={"access-control-allow-origin": "*", "cache-control": "no-store"})

    app.add_api_route(f"/.well-known/oauth-protected-resource{path}", protected_resource, methods=["GET"], include_in_schema=False)
    app.add_api_route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"], include_in_schema=False)
