"""Member-facing workflow catalog.

This is a presentation adapter over the existing manifest registry,
capability policy view, and typed workflow OpenAPI helpers. It does not run
workflows or inspect backend files.
"""

from __future__ import annotations


import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response

from gateway.capabilities import Capability, summarize_capability
from gateway.discovery import WorkflowRecord, summarize_workflow
from gateway.model_metadata import asset_media_type
from gateway.model_presentation import CAPABILITY_DESCRIPTIONS
from gateway.workflow_openapi import (
    _execution,
    gateway_route,
    request_mapping,
    route_inputs,
    short_id,
    workflow_catalog,
    workflow_inputs_schema,
)

CatalogProvider = Callable[[Request], Mapping[str, Any] | Response | Awaitable[Mapping[str, Any] | Response]]


def create_workflows_catalog_router(
    catalog_provider: CatalogProvider,
    *,
    prefix: str = "/me/api",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["user-workflows"], include_in_schema=False)

    @router.get("/workflows", response_model=None)
    async def workflows_catalog(request: Request):
        try:
            payload = catalog_provider(request)
            if inspect.isawaitable(payload):
                payload = await payload
            if isinstance(payload, Response):
                return payload
        except Exception as exc:  # noqa: BLE001
            status_code = int(getattr(exc, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR))
            code = str(getattr(exc, "code", "workflows_catalog_unavailable"))
            message = str(
                getattr(
                    exc,
                    "message",
                    "Workflows catalog is unavailable" if status_code >= 500 else "Workflows catalog request failed",
                )
            )
            return JSONResponse(
                status_code=status_code,
                content={"error": {"code": code, "message": message}},
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=payload,
            headers={"cache-control": "no-store"},
        )

    return router


def build_workflows_catalog(
    *,
    records: Iterable[WorkflowRecord],
    capabilities: Iterable[Capability],
    aliases: Mapping[str, Any] | None = None,
    recent_receipts: Iterable[Mapping[str, Any]] = (),
    recent_jobs: Iterable[Mapping[str, Any]] = (),
    base_url: str = "",
) -> dict[str, Any]:
    """Build the policy-filtered member workflow catalog."""

    records_by_id = {record.id: record for record in records}
    allowed_caps = [cap for cap in capabilities if cap.workflow and cap.policy_available is not False]
    allowed_ids = {str(cap.workflow) for cap in allowed_caps}
    caps_by_workflow: dict[str, list[Capability]] = {}
    for cap in allowed_caps:
        caps_by_workflow.setdefault(str(cap.workflow), []).append(cap)

    openapi_rows = {row["id"]: row for row in workflow_catalog(records_by_id.values(), aliases)}
    receipt_rows = _recent_by_workflow(recent_receipts, "request_id")
    job_rows = _recent_by_workflow(recent_jobs, "job_id")

    workflows: list[dict[str, Any]] = []
    for workflow_id in sorted(allowed_ids):
        record = records_by_id.get(workflow_id)
        if record is None:
            continue
        workflow = _workflow_row(
            record,
            capability_rows=caps_by_workflow.get(workflow_id, []),
            openapi_row=openapi_rows.get(workflow_id, {}),
            aliases=aliases,
            receipts=receipt_rows.get(workflow_id, []),
            jobs=job_rows.get(workflow_id, []),
            base_url=base_url,
        )
        workflows.append(workflow)

    groups = [
        {
            "capability": capability,
            "title": capability.replace("_", " ").title(),
            "count": len(items),
            "workflow_ids": [str(item["id"]) for item in items],
        }
        for capability, items in sorted(_group_by_capability(workflows).items())
    ]

    return {
        "schema_version": 1,
        "object": "workflow.catalog",
        "discovery_url": _join_url(base_url, "/gateway/v1/workflows"),
        "workflows": workflows,
        "groups": groups,
        "counts": {
            "workflows": len(workflows),
            "capabilities": len(allowed_caps),
        },
        "links": {
            "self": "/me/api/workflows",
            "gateway_discovery": "/gateway/v1/workflows",
            "openapi": "/openapi.json",
        },
    }


def _workflow_row(
    record: WorkflowRecord,
    *,
    capability_rows: list[Capability],
    openapi_row: Mapping[str, Any],
    aliases: Mapping[str, Any] | None,
    receipts: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    summary = summarize_workflow(record, "client")
    routes = _execution(record, _typical_seconds(openapi_row))
    sync_route = routes.get("sync")
    async_route = routes.get("async")
    preferred = routes.get(str(routes.get("recommended") or "sync"))
    native_route = gateway_route(record)
    short = short_id(record)
    stable_aliases = list(openapi_row.get("aliases") or _aliases_for(record.id, aliases))
    effective_inputs = _input_rows(record, routes)
    example = _example_request(record, effective_inputs, preferred or sync_route or async_route, base_url)
    engine_binding = _engine_binding_label(summary.get("supported_backends") or [], capability_rows)
    availability = _availability(capability_rows)
    capability = _capability_from_intent(record.intent, record.manifest.get("capability"))
    manifest_purpose = summary.get("purpose")
    purpose = manifest_purpose.strip() if isinstance(manifest_purpose, str) else ""
    description = purpose or _workflow_description(summary.get("description"), capability)
    technical_notes = _workflow_technical_notes(summary.get("description"), description)

    row = {
        "id": record.id,
        "short_id": short,
        "capability": capability,
        "intent": record.intent,
        "title": summary.get("display_name") or record.id,
        "purpose": purpose or None,
        "description": description,
        "inputs": effective_inputs,
        "outputs": summary.get("outputs") or [],
        "engine_binding": engine_binding,
        "engine_binding_detail": {
            "backends": summary.get("supported_backends") or [],
            "available": any(cap.available for cap in capability_rows),
            "status": availability["status"],
            "measured": availability["measured"],
            "reason_code": availability["reason_code"],
        },
        "availability": availability,
        "typical_seconds": summary.get("typical_seconds"),
        "aliases": stable_aliases,
        "example_request": example,
        "examples": {
            "curl": example["curl"],
            "python": example["python"],
            "javascript": example["javascript"],
        },
        "native_route": native_route,
        "native_passthrough_route": native_route,
        "discovery_url": _join_url(base_url, f"/gateway/v1/workflows/{record.id}"),
        "machine_readable_discovery_url": _join_url(base_url, f"/openapi.json#x-azizam-workflows"),
        "recent_jobs_url": f"/me/api/media?workflow={record.id}",
        "try_url": f"/ui/try/?workflow={record.id}",
        "capabilities": [summarize_capability(cap, "client") for cap in capability_rows],
        "recent_jobs": jobs[:5],
        "recent_receipts": receipts[:5],
        "openapi": {
            "inputs_schema": openapi_row.get("inputs_schema"),
            "route_inputs": openapi_row.get("route_inputs") or route_inputs(record),
            "execution": openapi_row.get("execution") or routes,
        },
    }
    if technical_notes:
        row["technical_notes"] = technical_notes
    return row


def _workflow_description(value: Any, capability: str) -> str:
    raw = _first_sentence(value)
    if raw and len(raw.split()) <= 8 and not _is_technical_workflow_note(raw):
        return _sentence(raw)
    return CAPABILITY_DESCRIPTIONS.get(capability, "Run this configured workflow.")


def _workflow_technical_notes(value: Any, public_description: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return raw if raw != public_description else None


def _first_sentence(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]


def _sentence(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _is_technical_workflow_note(value: str) -> bool:
    patterns = (
        r"\b(?:contract|protocol|gateway-composed|engine|binding|bound|backend|schema|adapter|passthrough|checkpoint|workflow)\b",
        r"\b(?:comfyui|localhost|port|khz|flac|gguf|safetensors|ckpt)\b",
        r"[/:]",
        r"\b\w+\.\w+\b",
        r"[_`]",
        r"\b[\w-]+\.(?:png|jpe?g|webp|mp4|mov|wav|mp3|safetensors|ckpt|gguf)\b",
        r"\([^)]*\b(?:v?\d+(?:\.\d+)+|license|apache|mit|gpl|bsd|iclr)\b",
    )
    lowered = value.casefold()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _input_rows(record: WorkflowRecord, routes: Mapping[str, Any]) -> list[dict[str, Any]]:
    schema = workflow_inputs_schema(record)
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    manifest_inputs = record.manifest.get("inputs") if isinstance(record.manifest.get("inputs"), Mapping) else {}
    required = set(schema.get("required") or [])
    preferred = routes.get(str(routes.get("recommended") or "sync")) or routes.get("sync") or routes.get("async") or {}
    mode = "async" if preferred == routes.get("async") else "sync"
    mapping = request_mapping(record, mode)
    rows: list[dict[str, Any]] = []
    for name, spec in props.items():
        if not isinstance(spec, Mapping):
            continue
        mapped = mapping.get(str(name), {})
        location = str(mapped.get("in") or "extension")
        row = {
            "name": str(name),
            "type": str(spec.get("type") or "string"),
            "required": str(name) in required,
            "accepted": location != "none",
            "ignored": location == "none",
            "applies": _applies_label(location),
            "request_field": mapped.get("field"),
        }
        for optional in ("default", "minimum", "maximum", "enum", "description"):
            if optional in spec:
                row[optional] = spec[optional]
        declared = manifest_inputs.get(name) if isinstance(manifest_inputs.get(name), Mapping) else {}
        # The JSON schema calls an asset "string" (and flags it x-azizam-asset);
        # keep what it really is, so a page can tell a picture input from a text one.
        if declared.get("type") == "asset" or spec.get("x-azizam-asset"):
            row["asset"] = True
            media_type = asset_media_type(record.intent, declared)
            if media_type:
                row["media_type"] = media_type
        if isinstance(declared.get("label"), str) and declared["label"].strip():
            row["label"] = declared["label"].strip()
        rows.append(row)
    for extra in route_inputs(record):
        row = {
            "name": extra["name"],
            "type": extra.get("type", "string"),
            "required": False,
            "accepted": True,
            "ignored": False,
            "applies": "Route option",
            "request_field": extra.get("field", extra["name"]),
        }
        for optional in ("default", "minimum", "maximum", "enum", "description"):
            if optional in extra:
                row[optional] = extra[optional]
        rows.append(row)
    return rows


def _example_request(
    record: WorkflowRecord,
    inputs: list[Mapping[str, Any]],
    route: Mapping[str, Any] | None,
    base_url: str,
) -> dict[str, Any]:
    path = _route_path(route) or f"/gateway/v1/workflows/{short_id(record)}/run"
    method = str((route or {}).get("method") or "POST")
    content_type = str((route or {}).get("content_type") or "application/json")
    url = _join_url(base_url, path)
    mode = "async" if (route or {}).get("kind") == "video" else "sync"
    selected_model = _route_model(route)
    body = _example_body(record, inputs, mode=mode, model=selected_model)
    form: dict[str, Any] = {}
    files: dict[str, str] = {}
    if (route or {}).get("kind") == "media_job":
        submit = route.get("submit") if isinstance(route, Mapping) else {}
        wrapped_intent = _nested(submit or {}, "body", "intent") or record.intent
        body = {
            "intent": wrapped_intent,
            "request": _example_body(record, inputs, mode="sync", model=_route_model(submit)),
        }
        content_type = "application/json"
    elif content_type == "multipart/form-data":
        form, files = _example_multipart_parts(record, inputs, model=selected_model)
        body = {}
    binary_response = (record.intent == "audio.generate" and (route or {}).get("kind") != "media_job") or content_type == "multipart/form-data"
    curl = _curl_example(
        url, content_type=content_type, body=body, form=form, files=files,
        binary_response=binary_response,
    )
    return {
        "method": method,
        "path": path,
        "content_type": content_type,
        "body": body,
        "form": form,
        "files": files,
        "curl": curl,
        "python": _python_example(
            method, url, content_type=content_type, body=body, form=form, files=files,
            binary_response=binary_response,
        ),
        "javascript": _javascript_example(
            method, url, content_type=content_type, body=body, form=form, files=files,
            binary_response=binary_response,
        ),
    }


def _example_body(
    record: WorkflowRecord,
    inputs: list[Mapping[str, Any]],
    *,
    mode: str,
    model: str | None = None,
) -> dict[str, Any]:
    mapping = request_mapping(record, mode)
    values = {str(item.get("name")): _example_value(item) for item in inputs if item.get("accepted")}
    body: dict[str, Any] = {}
    if model or not str(record.intent).startswith("video.edit"):
        body["model"] = model or short_id(record)
    extension: dict[str, Any] = {}
    combined: dict[str, list[str]] = {}
    for name, placement in mapping.items():
        if name not in values or placement.get("in") == "none":
            continue
        location = str(placement.get("in") or "")
        field = str(placement.get("field") or name)
        if location == "body":
            if placement.get("combine"):
                combined[field] = [str(part) for part in placement["combine"]]
            else:
                body[field] = values[name]
        elif location == "extension":
            extension[field] = values[name]
    for field, parts in combined.items():
        if field == "size" and all(part in values for part in parts):
            body[field] = f"{values[parts[0]]}x{values[parts[1]]}"
    for item in route_inputs(record):
        name = str(item.get("name") or "")
        field = str(item.get("field") or name)
        if name and field not in body:
            body[field] = _example_value(item)
    if extension:
        body.setdefault("extensions", {})["com.azizam.workflow"] = extension
    return body


def _example_multipart_parts(
    record: WorkflowRecord,
    inputs: list[Mapping[str, Any]],
    *,
    model: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    mapping = request_mapping(record, "sync")
    values = {str(item.get("name")): _example_value(item) for item in inputs if item.get("accepted")}
    form: dict[str, Any] = {}
    if model and record.intent == "image.edit":
        form["model"] = model
    files: dict[str, str] = {}
    extension: dict[str, Any] = {}
    combined: dict[str, list[str]] = {}
    for name, placement in mapping.items():
        if name not in values or placement.get("in") == "none":
            continue
        location = str(placement.get("in") or "")
        field = str(placement.get("field") or name)
        if location == "form":
            if placement.get("combine"):
                combined[field] = [str(part) for part in placement["combine"]]
            else:
                form[field] = values[name]
        elif location == "file":
            files[field] = _example_file_for(name)
        elif location == "file+extension":
            files[field] = _example_file_for(name)
            extension[field] = {"multipart_field": field}
        elif location == "extension":
            extension[field] = values[name]
    for field, parts in combined.items():
        if field == "size" and all(part in values for part in parts):
            form[field] = f"{values[parts[0]]}x{values[parts[1]]}"
    if extension:
        form["extensions"] = json.dumps({"com.azizam.workflow": extension}, separators=(",", ":"))
    return form, files


def _example_file_for(name: str) -> str:
    return f"./{name.replace('_', '-')}.bin"


def _route_model(route: Mapping[str, Any] | None) -> str | None:
    if not isinstance(route, Mapping):
        return None
    value = route.get("model") or _nested(route, "submit", "model")
    return str(value) if value else None


def _example_value(item: Mapping[str, Any]) -> Any:
    if item.get("default") is not None:
        return item["default"]
    if item.get("enum"):
        return list(item["enum"])[0]
    if item.get("type") == "integer":
        return int(item["minimum"] if item.get("minimum") is not None else 1)
    if item.get("type") == "number":
        return float(item["minimum"] if item.get("minimum") is not None else 1)
    if item.get("type") == "boolean":
        return False
    if item.get("type") == "asset" or item.get("asset"):
        return "data:image/png;base64,..."
    if item.get("name") == "prompt":
        return "A small teal fish mark on a clean white card."
    if item.get("name") == "size":
        return "1024x1024"
    return "value"


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _curl_example(
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Mapping[str, str],
    binary_response: bool,
) -> str:
    lines = [
        f"curl {_shell_single_quote(url)} \\",
        "  -H 'Authorization: Bearer '$AZIZAM_API_KEY \\",
    ]
    if content_type == "multipart/form-data":
        for key, value in form.items():
            lines.append(f"  -F {_shell_single_quote(f'{key}={value}')} \\")
        for key, path in files.items():
            lines.append(f"  -F {_shell_single_quote(f'{key}=@{path}')} \\")
        if binary_response:
            lines.append("  --output workflow-output.bin \\")
        return "\n".join(lines).rstrip(" \\")
    lines.append("  -H 'Content-Type: application/json' \\")
    lines.append(f"  -d {_shell_single_quote(json.dumps(body, separators=(',', ':')))}" + (" \\" if binary_response else ""))
    if binary_response:
        lines.append("  --output workflow-output.bin")
    return "\n".join(lines)


def _python_example(
    method: str,
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Mapping[str, str],
    binary_response: bool,
) -> str:
    if content_type == "multipart/form-data":
        file_lines = "\n".join(
            f"    {key!r}: stack.enter_context(open({path!r}, 'rb')),"
            for key, path in files.items()
        )
        return (
            "import os\nfrom contextlib import ExitStack\nimport requests\n\n"
            "with ExitStack() as stack:\n"
            f"    files = {{\n{file_lines}\n    }}\n"
            f"    response = requests.request({method!r}, {url!r},\n"
            "        headers={'Authorization': f\"Bearer {os.environ.get('AZIZAM_API_KEY') or ''}\"},\n"
            f"        data={dict(form)!r}, files=files, timeout=300)\n"
            "response.raise_for_status()\n"
            + (
                "open('workflow-output.bin', 'wb').write(response.content)\n"
                if binary_response else
                "print(response.json() if response.headers.get('content-type', '').startswith('application/json') else response.content[:80])"
            )
        )
    return (
        "import os\nimport requests\n\n"
        f"response = requests.request({method!r}, {url!r},\n"
        "    headers={'Authorization': f\"Bearer {os.environ.get('AZIZAM_API_KEY') or ''}\"},\n"
        f"    json={dict(body)!r}, timeout=300)\n"
        "response.raise_for_status()\n"
        + (
            "open('workflow-output.bin', 'wb').write(response.content)\n"
            if binary_response else
            "print(response.json())"
        )
    )


def _javascript_example(
    method: str,
    url: str,
    *,
    content_type: str,
    body: Mapping[str, Any],
    form: Mapping[str, Any],
    files: Mapping[str, str],
    binary_response: bool,
) -> str:
    if content_type == "multipart/form-data":
        lines = [
            "import { readFile, writeFile } from 'node:fs/promises';",
            "const form = new FormData();",
            *[f"form.append({json.dumps(key)}, {json.dumps(str(value))});" for key, value in form.items()],
            *[
                (
                    f"form.append({json.dumps(key)}, "
                    f"new Blob([await readFile({json.dumps(path)})]), "
                    f"{json.dumps(path.rsplit('/', 1)[-1])});"
                )
                for key, path in files.items()
            ],
            f"const response = await fetch({json.dumps(url)}, {{",
            f"  method: {json.dumps(method)},",
            "  headers: { authorization: `Bearer ${process.env.AZIZAM_API_KEY}` },",
            "  body: form",
            "});",
            "if (!response.ok) throw new Error(await response.text());",
            (
                "await writeFile('workflow-output.bin', Buffer.from(await response.arrayBuffer()));"
                if binary_response else
                "console.log(await response.text());"
            ),
        ]
        return "\n".join(lines)
    result_line = (
        "await writeFile('workflow-output.bin', Buffer.from(await response.arrayBuffer()));"
        if binary_response else
        "console.log(await response.json());"
    )
    import_line = "import { writeFile } from 'node:fs/promises';\n" if binary_response else ""
    return (
        f"{import_line}const response = await fetch({json.dumps(url)}, {{\n"
        f"  method: {json.dumps(method)},\n"
        "  headers: { authorization: `Bearer ${process.env.AZIZAM_API_KEY}`, 'content-type': 'application/json' },\n"
        f"  body: JSON.stringify({json.dumps(dict(body), indent=2)}),\n"
        f"}});\nif (!response.ok) throw new Error(await response.text());\n{result_line}"
    )


def _recent_by_workflow(rows: Iterable[Mapping[str, Any]], id_field: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        keys = [
            row.get("workflow"),
            row.get("requested_workflow"),
            row.get("resolved_workflow"),
            _nested(row, "metadata", "workflow"),
            _nested(row, "metadata", "workflow_id"),
        ]
        for value in keys:
            if not value:
                continue
            item = {
                id_field: row.get(id_field) or row.get("id"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "latency_ms": row.get("latency_ms"),
            }
            out.setdefault(str(value), []).append({k: v for k, v in item.items() if v is not None})
    return out


def _group_by_capability(workflows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for workflow in workflows:
        out.setdefault(str(workflow.get("capability") or "workflow"), []).append(workflow)
    return out


def _route_path(route: Any) -> str | None:
    if not isinstance(route, Mapping):
        return None
    if route.get("path"):
        return str(route["path"])
    submit = route.get("submit")
    if isinstance(submit, Mapping) and submit.get("path"):
        return str(submit["path"])
    return None


def _nested(row: Mapping[str, Any], *path: str) -> Any:
    value: Any = row
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _aliases_for(workflow_id: str, aliases: Mapping[str, Any] | None) -> list[str]:
    if not aliases:
        return []
    out: list[str] = []
    for view in aliases.values():
        if not isinstance(view, Mapping):
            continue
        for row in view.get("stable") or []:
            if not isinstance(row, Mapping):
                continue
            target = str(row.get("resolves_to") or "")
            if target == workflow_id or workflow_id.endswith(f".{target}"):
                alias = row.get("alias")
                if isinstance(alias, str) and alias not in out:
                    out.append(alias)
    return out


def _capability_from_intent(intent: str, declared: Any = None) -> str:
    if isinstance(declared, str) and declared in {
        "image", "video", "speech", "transcribe", "music", "sfx", "audio"
    }:
        return declared
    if intent.startswith("image."):
        return "image"
    if intent.startswith("video."):
        return "video"
    if intent.startswith("audio.") or intent.startswith("speech."):
        return "audio"
    return "workflow"


def _availability_reason_code(capabilities: list[Capability]) -> str:
    if not capabilities:
        return "not_available"
    if any(cap.available for cap in capabilities):
        return "configured"
    caveats = [caveat for cap in capabilities for caveat in cap.caveats]
    return "policy_denied" if caveats else "unavailable"


def _availability(capabilities: list[Capability]) -> dict[str, Any]:
    projected = [cap for cap in capabilities if isinstance(cap.availability, Mapping) and cap.policy_available]
    if projected:
        rank = {"ready": 6, "idle": 5, "starting": 4, "busy": 3, "degraded": 2, "unknown": 1, "unavailable": 0}
        selected = max(projected, key=lambda cap: rank.get(str(cap.availability.get("status")), 0))
        result = dict(selected.availability)
        status = str(result.get("status") or "unknown")
        evidence = result.get("evidence") or {}
        # Only a gateway-composed workflow's reason is product copy: it names
        # the engine it waits on in plain language. Other reasons are raw.
        if evidence.get("source") != "dependencies":
            result.pop("reason", None)
            result.pop("engine", None)
        result["reason_code"] = {
            ("unavailable", "disabled"): "unavailable_disabled",
            ("unavailable", "unconfigured"): "unavailable_unconfigured",
        }.get((status, evidence.get("kind")), status)
        if evidence.get("source") == "dependencies":
            # A composed workflow carries its own code: an engine it needs
            # that is turned off or not set up is Off, not Down.
            result["reason_code"] = str(selected.availability.get("reason_code") or status)
        return result
    measured_caps = [cap for cap in capabilities if cap.health != "unknown" or cap.lifecycle != "unknown"]
    ready_caps = [
        cap for cap in measured_caps
        if cap.available and (
            cap.health == "healthy"
            or cap.lifecycle == "warm"
        )
    ]
    if ready_caps:
        return {
            "status": "ready",
            "available": True,
            "measured": True,
            "reason_code": "ready",
        }
    starting_caps = [
        cap for cap in measured_caps
        if cap.available and cap.lifecycle == "warming"
    ]
    if starting_caps:
        return {
            "status": "starting",
            "available": True,
            "measured": True,
            "reason_code": "starting",
        }
    if capabilities:
        unknown_eligible = [
            cap for cap in capabilities
            if cap.available and cap.health == "unknown" and cap.lifecycle == "unknown"
        ]
        if unknown_eligible:
            return {
                "status": "configured",
                "available": True,
                "measured": False,
                "reason_code": "configured",
            }
        measured_available = [cap for cap in measured_caps if cap.available]
        if measured_available:
            return {
                "status": "configured",
                "available": True,
                "measured": True,
                "reason_code": "configured",
            }
        if measured_caps:
            return {
                "status": "unavailable",
                "available": False,
                "measured": True,
                "reason_code": _availability_reason_code(capabilities),
            }
    return {
        "status": "not_available",
        "available": False,
        "measured": False,
        "reason_code": _availability_reason_code(capabilities),
    }


def _engine_binding_label(backends: Iterable[Any], capabilities: list[Capability]) -> str | None:
    names = [str(name) for name in backends if name]
    if not names:
        names = sorted({str(cap.backend) for cap in capabilities if cap.backend})
    return ", ".join(names) if names else None


def _applies_label(location: str) -> str:
    return {
        "body": "Request body",
        "form": "Multipart form",
        "file": "File upload",
        "file+extension": "File upload and workflow extension",
        "extension": "Workflow extension",
        "none": "Managed by gateway",
    }.get(location, location or "Workflow input")


def _typical_seconds(row: Mapping[str, Any]) -> float | int | None:
    value = row.get("typical_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _join_url(base_url: str, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
