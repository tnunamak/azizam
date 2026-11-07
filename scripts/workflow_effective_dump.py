#!/usr/bin/env python3
"""Dump what one tree's code makes of the bundled workflows (CODE_TO_DATA_S3, S3.5a, the equality check).

    python scripts/workflow_effective_dump.py TREE_ROOT CORPUS_JSON

Run by ``workflow_contract.py diff-effective`` once per side, with the interpreter that runs the tests and with TREE_ROOT put
first on ``sys.path``: the code that answers is the tree's own, so a tree from a git ref answers as that ref did. Prints one
JSON document: every projection of every workflow, the compiled engine request for every corpus case, and the form fields
of the multipart binding. It names no workflow, engine or service; it works from the documents and the corpus.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

AUDIENCES = ("client", "admin", "internal")


def plain(value: Any) -> Any:
    def encode(item: Any) -> Any:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.asdict(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item, key=str)
        return str(item)

    return json.loads(json.dumps(value, default=encode, sort_keys=True))


def projections(record: Any) -> dict[str, Any]:
    from gateway import capabilities, workflow_openapi
    from gateway.availability import project_capabilities
    from gateway.discovery import summarize_workflow
    from gateway.workflows_catalog import build_workflows_catalog

    caps = [capabilities.capability_from_workflow_record(record, backend=backend) for backend in record.supported_backends]
    out: dict[str, Any] = {f"summary:{audience}": summarize_workflow(record, audience) for audience in AUDIENCES}
    out["capabilities"] = caps
    out["projected"] = project_capabilities(caps, workflow_records=[record])
    out["schema"] = workflow_openapi.workflow_inputs_schema(record)
    out["route_inputs"] = workflow_openapi.route_inputs(record)
    out["mapping"] = {mode: workflow_openapi.request_mapping(record, mode) for mode in ("sync", "async")}
    out["openapi_catalog"] = workflow_openapi.workflow_catalog([record])
    out["member_catalog"] = build_workflows_catalog(records=[record], capabilities=caps, base_url="https://example.test")
    return {name: plain(value) for name, value in out.items()}


def engine_requests(root: Path, document: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request each corpus case compiles to, for every binding that holds a template (a node graph the engine runs)."""
    from gateway.manifests import bind_backend_request

    out: list[dict[str, Any]] = []
    for key, block in (document.get("bindings") or {}).items():
        template = block.get("workflow") if isinstance(block, dict) else None
        if not isinstance(template, str):
            continue
        graph = json.loads((root / template).read_text(encoding="utf-8"))
        for case in cases:
            result = bind_backend_request(document, key, graph, copy.deepcopy(case["values"]))
            out.append({"binding": key, "case": case["name"], "request": plain(result.request),
                        "applied": sorted(result.applied_inputs)})
    return out


def multipart_form(record: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The form fields the multipart binding sends for each corpus case, and the name of the file part."""
    from gateway import mesh_api, mesh_manifest

    document = record.manifest
    if not any(isinstance(block, dict) and "multipart" in str(block.get("transport", {}).get("kind", "")) or key == "http-multipart"
               for key, block in (document.get("bindings") or {}).items()):
        return []
    if hasattr(mesh_api, "mesh_workflow_from_record"):
        workflow = mesh_api.mesh_workflow_from_record(record)
    else:
        workflow = mesh_manifest.load_mesh_manifest(record.source_path)
    picture = mesh_manifest.ImageAsset(b"x", "input.png", "image/png")
    out = []
    for case in cases:
        supplied = {name: value for name, value in case["values"].items() if name in workflow.inputs and name != workflow.image_input}
        try:
            request = mesh_manifest.bind_request(workflow, supplied, picture)
            fields, part = mesh_manifest.downstream_form(workflow, request)
            out.append({"case": case["name"], "fields": fields, "part": part.filename, "image_input": workflow.image_input})
        except mesh_manifest.MeshInputError as error:
            out.append({"case": case["name"], "refused": error.code})
    return out


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve()
    corpus = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    sys.path.insert(0, str(root))
    os.chdir(root)
    from gateway.discovery import WorkflowRegistry

    registry = WorkflowRegistry.from_directory(root / "manifests" / "workflows")
    result: dict[str, Any] = {"errors": plain(registry.errors), "workflows": {}}
    for record in registry.records():
        cases = corpus.get(record.id, [])
        result["workflows"][record.id] = {
            "projections": projections(record),
            "requests": engine_requests(root, record.manifest, cases),
            "form": multipart_form(record, cases),
        }
    # A path under the tree is the tree's, not the workflow's: it must not differ between two trees.
    sys.stdout.write(json.dumps(result, sort_keys=True).replace(json.dumps(str(root))[1:-1], "<root>"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
