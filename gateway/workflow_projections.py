"""The production ``Projections`` (CODE_TO_DATA_S4_S8, I3, S5): pure functions of a registry entry or snapshot.

Nothing here reads a global, the live config, the clock or a cache. Viewer-specific and live data (authorization,
availability, recent activity, base URL) are applied by the caller after the static projection. The OpenAPI path names
come from the kernel's ``generated_names`` and nothing else, so the store's clash check sees every name emitted here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gateway.discovery import WorkflowRecord, summarize_catalog, summarize_workflow
from gateway.manifests import Provenance, accepts_of, manifest_identity
from gateway.output_selectors import effective_count
from gateway.result_bundle import ResultLimits
from gateway.workflow_policy import PolicyFacts
from gateway.workflow_closed import closed_reason
from gateway.workflow_kernel import _thaw, generated_names
from gateway.workflow_openapi import workflow_inputs_schema
from gateway.workflow_runtime_types import (
    AdapterAvailability, BindingDef, ExecutionProfile, MediaAccept, OutputShape, ReceiptIdentity, RegistrySnapshot, WorkflowEntry,
)

NEEDS_POLICY = "needs_policy"
BINDING_UNKNOWN = "binding_unknown"
RUN_PATH = "/gateway/v1/workflows/{}/run"


def record_of(entry: WorkflowEntry, source_path: str = "") -> WorkflowRecord:
    """A plain ``WorkflowRecord`` view of an entry, for the builders that still read records (the OpenAPI schemas). Its
    identity is the manifest identity receipts record, not the execution digest."""
    identity = manifest_identity(_thaw(entry.contract), Provenance(entry.provenance.trusted, entry.provenance.source))
    facts = None if entry.facts is None else PolicyFacts(
        entry.facts.exposure, entry.facts.trust_tier, entry.facts.resource_tier, entry.facts.resource_class, tuple(entry.facts.tags),
        entry.facts.selection_rank)
    return WorkflowRecord(manifest=_thaw(entry.contract), identity=identity, source_path=Path(source_path),
                          facts=facts)


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


class SnapshotRegistry:
    """The registry-shaped reader of one snapshot, for the catalog and extension builders that read records. It holds
    the snapshot it was made from, so every read of a request sees one revision. ``published_only`` hides unpublished
    workflows (5.9: an unpublished workflow is the owner's alone)."""

    def __init__(self, snapshot: RegistrySnapshot, *, published_only: bool = False) -> None:
        self._entries = snapshot.workflows
        self._records = {wid: record_of(entry, getattr(snapshot, "source_paths", {}).get(wid, "")) for wid, entry in sorted(snapshot.workflows.items())
                         if not published_only or (entry.published and not closed_reason(entry))}

    def records(self) -> list[WorkflowRecord]:
        return list(self._records.values())

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        return self._records.get(workflow_id)

    def by_intent(self, intent: str) -> list[WorkflowRecord]:
        return [r for r in self._records.values() if r.intent == intent]

    def summarize_workflow(self, workflow_id: str, audience: str = "client") -> dict[str, Any] | None:
        record = self.get(workflow_id)
        if record is None:
            return None
        result = summarize_workflow(record, audience)
        if audience != "client":
            result["state"] = self._entries[workflow_id].state
        return result

    def summarize_catalog(self, audience: str = "client") -> dict[str, Any]:
        catalog = summarize_catalog(self.records(), audience)
        if audience != "client":
            for row in catalog["workflows"]:
                row["state"] = self._entries[row["id"]].state
        return catalog


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


def graph_status_capabilities(path: Path, contracts: Mapping[str, Any]) -> dict[str, bool]:
    """Project declared status facts from the contract bound to a graph file.

    Undeclared graphs publish no capability promises. Graph inventory remains diagnostic only.
    """
    root = Path(__file__).resolve().parents[1]
    for contract in contracts.values():
        if any(isinstance(binding, Mapping) and binding.get("workflow")
               and (root / binding["workflow"]).resolve() == path.resolve()
               for binding in contract.get("bindings", {}).values()):
            facts = contract.get("annotations", {}).get("azizam.dev/status-capabilities", {})
            return {name: facts.get(name) is True for name in ("supports_i2v", "supports_audio", "two_stage")}
    return {name: False for name in ("supports_i2v", "supports_audio", "two_stage")}
