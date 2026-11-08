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


def template_path(root: Path, package_dir: Path | None, template: str) -> Path:
    """A bundled workflow names its template from the tree root; a package workflow names it as ``package://<path>``."""
    if template.startswith("package://") and package_dir is not None:
        return package_dir / template.removeprefix("package://").lstrip("/")
    return root / template


def engine_requests(root: Path, package_dir: Path | None, document: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request each corpus case compiles to, for every binding that holds a template (a node graph the engine runs).

    A case holds the values the route would hand to the binding: the caller's, the defaults and the generated ones, already
    prepared by the comparison from this tree's own document; ``rejected`` holds what the input check refused."""
    from gateway.manifests import bind_backend_request

    out: list[dict[str, Any]] = []
    for key, block in (document.get("bindings") or {}).items():
        template = block.get("workflow") if isinstance(block, dict) else None
        if not isinstance(template, str):
            continue
        graph = json.loads(template_path(root, package_dir, template).read_text(encoding="utf-8"))
        for case in cases:
            result = bind_backend_request(document, key, graph, copy.deepcopy(case["values"]))
            out.append({"binding": key, "case": case["name"], "request": plain(result.request),
                        "applied": sorted(result.applied_inputs), "rejected": case.get("rejected", {})})
    return out


def multipart_form(record: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The form fields the multipart binding sends for each corpus case, and the name of the file part."""
    from gateway import multipart_adapter

    document = record.manifest
    blocks = [block for key, block in (document.get("bindings") or {}).items()
              if isinstance(block, dict) and (key == "http-multipart" or "multipart" in str(block.get("transport", {}).get("kind", "")))]
    if not blocks:
        return []
    mapping = blocks[0].get("map") or {}
    inputs = record.contract.get("inputs") or {}
    image_input = next((name for name in mapping if (inputs.get(name) or {}).get("type") == "asset"), None)
    out = []
    for case in cases:
        supplied = {name: value for name, value in case["values"].items() if name in inputs and name != image_input}
        out.append({"case": case["name"], "fields": multipart_adapter.scalar_fields(inputs, mapping, supplied),
                    "part": "input.png", "image_input": image_input, "rejected": case.get("rejected", {})})
    return out


def _load_sources() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("workflow_sources", Path(__file__).resolve().parent / "workflow_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_record_arguments(workflow_id: str, package_root: Path) -> dict[str, Any]:
    """What this tree's ``add_manifest`` takes for a package record: the package provenance and the release policy's facts,
    by the one rule the gateway uses for a stored package record (``release_facts``: a row that names a package belongs to
    that package's workflows alone). A tree whose code predates the facts keeps the document's own fields, so nothing is passed."""
    import inspect

    import yaml

    from gateway.discovery import WorkflowRegistry

    if "facts" not in inspect.signature(WorkflowRegistry.add_manifest).parameters:
        return {}
    from gateway.extension_packages import review_local_directory_package
    from gateway.manifests import Provenance
    from gateway.workflow_policy import release_facts

    # The same two things the installer records on a package's workflow (``imported_registry_records``): the id the package
    # states and the hash of its reviewed content. The row applies only when that hash is the shipped package's.
    review = review_local_directory_package(package_root)
    package_id = (yaml.safe_load((package_root / "extension.yml").read_text(encoding="utf-8")) or {}).get("id")
    return {"provenance": Provenance(trusted=False, source="package"),
            "facts": release_facts(workflow_id, package_id=package_id, package_hash=review.get("package_hash"), from_package=True)}


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve()
    corpus = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    sys.path.insert(0, str(root))
    os.chdir(root)
    from gateway.discovery import WorkflowRegistry
    sources = _load_sources()
    registry = WorkflowRegistry.from_directory(root / "manifests" / "workflows")
    # A package workflow is not in the directory: it is added the way a reviewed package record is, from the file the
    # package names, with the package provenance and the release policy's facts for its id (when this tree's code takes
    # them). The file lists come from one module, so both trees and the comparison enumerate the same way.
    documents = sources.read_documents(root)
    for workflow_id, (path, document, package_root) in documents.items():
        if package_root is not None:
            registry.add_manifest({key: value for key, value in document.items() if key != "kind"}, source_path=path,
                                  **package_record_arguments(workflow_id, package_root))
    result: dict[str, Any] = {"errors": plain(registry.errors), "workflows": {}}
    packages = {workflow_id: package_root for workflow_id, (_path, _document, package_root) in documents.items()}
    for record in registry.records():
        cases = corpus.get(record.id, [])
        result["workflows"][record.id] = {
            "projections": projections(record),
            "requests": engine_requests(root, packages.get(record.id), record.manifest, cases),
            "form": multipart_form(record, cases),
        }
    # A path under the tree is the tree's, not the workflow's: it must not differ between two trees.
    sys.stdout.write(json.dumps(result, sort_keys=True).replace(json.dumps(str(root))[1:-1], "<root>"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
