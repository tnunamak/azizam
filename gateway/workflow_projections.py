"""The production ``Projections`` (CODE_TO_DATA_S4_S8, I3, S5): pure functions of a registry entry or snapshot.

Nothing here reads a global, the live config, the clock or a cache. Viewer-specific and live data (authorization,
availability, recent activity, base URL) are applied by the caller after the static projection. The OpenAPI path names
come from the kernel's ``generated_names`` and nothing else, so the store's clash check sees every name emitted here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gateway.discovery import WorkflowRecord
from gateway.manifests import ManifestIdentity, accepts_of
from gateway.output_selectors import effective_count
from gateway.result_bundle import ResultLimits
from gateway.workflow_kernel import _thaw, generated_names
from gateway.workflow_openapi import workflow_inputs_schema
from gateway.workflow_runtime_types import (
    AdapterAvailability, BindingDef, ExecutionProfile, MediaAccept, OutputShape, ReceiptIdentity, RegistrySnapshot, WorkflowEntry,
)

NEEDS_POLICY = "needs_policy"
BINDING_UNKNOWN = "binding_unknown"
RUN_PATH = "/gateway/v1/workflows/{}/run"


def record_of(entry: WorkflowEntry) -> WorkflowRecord:
    """A plain ``WorkflowRecord`` view of an entry, for the builders that still read records (the OpenAPI schemas)."""
    identity = ManifestIdentity(entry.id, str(entry.contract.get("version") or "") or None, entry.contract_digest,
                                entry.provenance.trusted, entry.provenance.source)
    return WorkflowRecord(manifest=_thaw(entry.contract), identity=identity, source_path=Path(entry.provenance.source or ""))


def _binding(entry: WorkflowEntry, binding_key: str) -> BindingDef:
    try:
        return entry.bindings[binding_key]
    except KeyError:
        raise KeyError(f"workflow {entry.id} has no binding {binding_key!r}") from None


def _accept_of(name: str, spec: Mapping[str, Any], limits: ResultLimits) -> MediaAccept | None:
    kind = spec.get("type")
    leaf = spec.get("items") if kind == "array" else spec
    if not isinstance(leaf, Mapping) or leaf.get("type") != "asset":
        return None
    if kind == "array":
        low, high = int(spec.get("min_items") or 0), spec.get("max_items")
        return MediaAccept(accepts_of(leaf), leaf.get("max_bytes"), low, limits.max_assets if high is None else int(high))
    return MediaAccept(accepts_of(leaf), spec.get("max_bytes"), 1 if spec.get("required") else 0, 1)


def _shape_of(spec: Mapping[str, Any], limits: ResultLimits) -> OutputShape:
    media_type = str(spec.get("media_type") or "")
    if media_type == "application/json":
        return OutputShape("json", media_type, 1, 1)
    low, high = effective_count(spec, limits.max_assets)
    return OutputShape("assets", media_type, low, limits.max_assets if high is None else high)


class _Projections:
    def execution_profile(self, entry: WorkflowEntry, binding_key: str, *, limits: ResultLimits) -> ExecutionProfile:
        binding = _binding(entry, binding_key)
        accepts = {}
        for name, spec in (entry.contract.get("inputs") or {}).items():
            accept = _accept_of(name, spec, limits) if isinstance(spec, Mapping) else None
            if accept is not None:
                accepts[str(name)] = accept
        produces = {str(name): _shape_of(spec, limits) for name, spec in (entry.contract.get("outputs") or {}).items()
                    if isinstance(spec, Mapping)}
        return ExecutionProfile(adapter_id=binding.adapter_id, adapter_facet=str(binding.binding.get("facet") or ""),
                                resource_class=entry.facts.resource_class if entry.facts else "", accepts=accepts, produces=produces)

    def receipt_identity(self, entry: WorkflowEntry, binding_key: str) -> ReceiptIdentity:
        return ReceiptIdentity(entry.id, entry.record_revision, entry.contract_digest, binding_key, entry.intent)

    def entry_problems(self, entry: WorkflowEntry, available: AdapterAvailability, *,
                       binding_key: str | None = None) -> tuple[str, ...]:
        problems = [NEEDS_POLICY] if entry.needs_configuration else []
        key = binding_key if binding_key is not None else next(iter(entry.bindings), None)
        binding = entry.bindings.get(key) if key is not None else None
        if binding is None:
            return (*problems, BINDING_UNKNOWN)
        return (*problems, *self.binding_problems(binding, available))

    def binding_problems(self, binding: BindingDef, available: AdapterAvailability) -> tuple[str, ...]:
        problem = available.binding_problem(binding)
        return (problem,) if problem else ()

    def catalog_static(self, snapshot: RegistrySnapshot) -> tuple[Mapping[str, Any], ...]:
        return tuple({
            "id": entry.id, "intent": entry.intent, "state": entry.state, "published": entry.published,
            "needs_configuration": entry.needs_configuration, "record_revision": entry.record_revision,
            "contract_digest": entry.contract_digest, "bindings": list(entry.bindings),
            "display_name": entry.contract.get("display_name") or entry.id,
            "summary": entry.contract.get("summary") or entry.contract.get("description") or "",
        } for _, entry in sorted(snapshot.workflows.items()))

    def openapi_paths(self, snapshot: RegistrySnapshot) -> Mapping[str, Any]:
        paths: dict[str, Any] = {}
        for workflow_id, entry in sorted(snapshot.workflows.items()):
            paths[RUN_PATH.format(workflow_id)] = {"post": {
                "operationId": generated_names(workflow_id)["operation"],
                "summary": f"Run {entry.contract.get('display_name') or workflow_id}",
                "x-azizam-workflow-id": workflow_id,
            }}
        return paths

    def input_form(self, entry: WorkflowEntry) -> Mapping[str, Any]:
        return workflow_inputs_schema(record_of(entry), form=True)


PROJECTIONS = _Projections()
