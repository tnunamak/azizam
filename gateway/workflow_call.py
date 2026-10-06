"""Shared plumbing for adapters that run a catalog workflow from typed inputs.

Both the MCP server and the A1111 facade need the same few steps: check inputs
against the manifest-derived schema, turn them into the request the core route
expects (using the ``fields`` map from ``x-azizam-workflows``), and turn a
core error into something the adapter can report. Nothing here talks HTTP or
knows a vendor protocol.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from gateway.workflow_openapi import WORKFLOW_EXTENSION_NAMESPACE

ASSET_ID_RE = re.compile(r"\b((?:img|aud|vid)_[0-9a-f]{64})\b")

# (request, auth, asset_id) -> bytes, or None when missing or not the caller's
ReadAsset = Callable[[Any, Any, str], "bytes | None"]


class CallError(Exception):
    """A failure the model can act on. Becomes ``isError: true``, not a protocol error."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details




# ------------------------------------------------------------- input handling


def _type_error(name: str, value: Any, spec: Mapping[str, Any]) -> str | None:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return f"input {name!r} must be a string"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"input {name!r} must be an integer"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"input {name!r} must be a number"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return f"input {name!r} must be a boolean"
    if "enum" in spec and value not in spec["enum"]:
        return f"input {name!r} must be one of {spec['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            return f"input {name!r} must be at least {spec['minimum']}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"input {name!r} must be at most {spec['maximum']}"
    return None


def validate_inputs(schema: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    """Check ``inputs`` against a flat manifest-derived schema. Raises CallError."""
    props = schema.get("properties") or {}
    problems = [f"unknown input {name!r}" for name in inputs if name not in props]
    problems += [f"missing required input {name!r}" for name in schema.get("required") or [] if name not in inputs]
    for name, value in inputs.items():
        if name in props:
            problem = _type_error(name, value, props[name])
            if problem:
                problems.append(problem)
    if problems:
        raise CallError("; ".join(problems), invalid_inputs=problems, valid_inputs=sorted(props))


def effective_schema(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Manifest inputs plus the route-owned fields (``n``, ``size``, ``seconds``)."""
    base = entry.get("inputs") or {"type": "object", "properties": {}}
    props = dict(base.get("properties") or {})
    for extra in entry.get("route_inputs") or []:
        spec = {k: v for k, v in extra.items() if k in ("type", "minimum", "maximum", "description", "enum")}
        props.setdefault(extra["name"], spec)
    schema = {k: v for k, v in base.items() if k not in ("x-azizam-workflow-id", "description")}
    schema["properties"] = props
    return schema


def sniff(data: bytes, hint: str | None = None) -> tuple[str, str]:
    """(media type, suffix) from magic bytes, falling back to ``hint``."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg", ".mp3"
    if data[:4] == b"fLaC":
        return "audio/flac", ".flac"
    if data[:4] == b"OggS":
        return "audio/ogg", ".ogg"
    hint = (hint or "").split(";")[0].strip().lower()
    return hint or "application/octet-stream", {
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4",
        "video/webm": ".webm", "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
        "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/opus": ".ogg",
    }.get(hint, ".bin")


def kind_for(media_type: str) -> str:
    return {"image": "img", "audio": "aud", "video": "vid"}.get(media_type.split("/")[0], "")


def asset_id_in(value: str) -> str | None:
    match = ASSET_ID_RE.search(value)
    return match.group(1) if match else None


async def asset_bytes(read_asset: ReadAsset, request: Any, auth: Any, name: str, value: Any) -> tuple[bytes, str]:
    """Bytes for an asset input: a data URI or a gateway asset id or signed URL."""
    if not isinstance(value, str) or not value:
        raise CallError(f"input {name!r} must be a data URI or an asset id from an earlier result")
    if value.startswith("data:"):
        head, _, payload = value.partition(",")
        try:
            raw = base64.b64decode(payload, validate=True) if ";base64" in head else payload.encode()
        except (binascii.Error, ValueError):
            raise CallError(f"input {name!r} is not a valid base64 data URI") from None
        return raw, head[5:].split(";")[0]
    asset_id = asset_id_in(value)
    if asset_id is None:
        raise CallError(
            f"input {name!r}: only data URIs and gateway asset ids or signed asset URLs are accepted; "
            "this adapter does not fetch external URLs"
        )
    content = await asyncio.to_thread(read_asset, request, auth, asset_id)
    if content is None:
        raise CallError(f"input {name!r}: asset {asset_id} was not found for this key (it may have expired)")
    return content, ""


# ------------------------------------------------------------ request building


@dataclass
class BuiltRequest:
    method: str
    path: str
    json: dict[str, Any] | None = None
    data: dict[str, str] | None = None
    files: dict[str, tuple[str, bytes, str]] | None = None


def _combine_size(inputs: Mapping[str, Any], mapping: Mapping[str, Any], body: dict[str, Any]) -> None:
    groups: dict[str, tuple[str, list[str]]] = {}
    for name, m in mapping.items():
        if m.get("combine") and name in inputs:
            groups[m["field"]] = (m["field"], list(m["combine"]))
    for field, (_, parts) in groups.items():
        missing = [p for p in parts if p not in inputs]
        if missing:
            raise CallError(f"give {' and '.join(parts)} together; missing {missing}")
        body[field] = "x".join(str(int(inputs[p])) for p in parts)


async def build_request(
    read_asset: ReadAsset, request: Any, auth: Any, entry: Mapping[str, Any], route: Mapping[str, Any],
    inputs: Mapping[str, Any], *, wrap_intent: str | None, response_format: str | None = "url",
) -> BuiltRequest:
    """Turn typed inputs into the request the core route expects, per the manifest field map."""
    mapping: dict[str, Any] = dict(route.get("fields") or {})
    for extra in entry.get("route_inputs") or []:
        mapping.setdefault(extra["name"], {"in": extra.get("in", "body"), "field": extra.get("field", extra["name"])})
    multipart = route.get("content_type", "").startswith("multipart")
    body: dict[str, Any] = {}
    ext: dict[str, Any] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    consumed: set[str] = set()
    for name, value in inputs.items():
        m = mapping.get(name)
        if m is None:
            raise CallError(f"input {name!r} is not available on the {route.get('method', 'POST')} {route.get('path')} route")
        where = m.get("in")
        if where == "none":
            raise CallError(f"input {name!r} is managed by the gateway on this route and cannot be set")
        if m.get("combine"):
            consumed.add(name)
            continue
        if where in ("file", "file+extension"):
            raw, hint = await asset_bytes(read_asset, request, auth, name, value)
            media_type, suffix = sniff(raw, hint)
            part = m["field"] if where == "file" else name
            files[part] = (f"{name}{suffix}", raw, media_type)
            if where == "file+extension":
                ext[m["field"]] = {"multipart_field": part}
        elif where == "extension":
            ext[m["field"]] = value
        elif where in ("body", "form"):
            if isinstance(value, str) and m["field"] == "input_reference":
                value = asset_id_in(value) or value
            body[m["field"]] = value
        else:
            raise CallError(f"input {name!r} has an unsupported field mapping: {where!r}")
    _combine_size(inputs, mapping, body)
    model = route.get("model")
    if model:
        body = {"model": model, **body}
    if response_format and entry["intent"].startswith("image."):
        body.setdefault("response_format", response_format)
    if ext:
        body["extensions"] = {WORKFLOW_EXTENSION_NAMESPACE: ext}

    if wrap_intent:  # media-job submit
        return BuiltRequest("POST", route["path"], json={"intent": wrap_intent, "request": body})
    if multipart:
        data = {k: v if isinstance(v, str) else json.dumps(v) for k, v in body.items()}
        return BuiltRequest(route.get("method", "POST"), route["path"], data=data, files=files)
    return BuiltRequest(route.get("method", "POST"), route["path"], json=body)


def error_from(resp: Any) -> CallError:
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {}
    err = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(err, Mapping):
        message = str(err.get("message") or err.get("code") or "request failed")
        return CallError(message, http_status=resp.status_code, code=err.get("code"), type=err.get("type"))
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    return CallError(str(detail or f"gateway returned HTTP {resp.status_code}"), http_status=resp.status_code)


