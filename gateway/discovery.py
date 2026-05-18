"""Workflow manifest registry and discovery summaries.

This module is a pure, side-effect-free layer that can back read-only
``/gateway/v1`` discovery endpoints. It does not import FastAPI, does not
talk to backends, and does not mutate live proxy state.

The registry loads workflow manifests from a directory, validates them via
:mod:`gateway.manifests`, computes manifest identity, and produces audience-
aware summaries.

Audiences:

* ``client`` — exposed to ordinary API callers. Includes the portable
  contract (inputs, outputs, supported backend names) but omits local
  filesystem paths, manifest content hashes, and raw backend binding
  internals such as ComfyUI node IDs.
* ``admin`` — adds manifest provenance (hash, source, trust flag, version,
  source path) and per-backend binding summaries that name backend
  artifacts (e.g. workflow JSON paths) without exposing arbitrary mapping
  internals.
* ``internal`` — everything ``admin`` sees plus the full raw binding map.
  Intended for gateway-internal diagnostics and route receipts, not for
  third parties.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from gateway.manifests import (
    ManifestError,
    ManifestIdentity,
    load_workflow_manifest,
    manifest_identity,
)

VALID_AUDIENCES = ("client", "admin", "internal")
MANIFEST_GLOBS = ("*.yml", "*.yaml")


@dataclass(frozen=True)
class WorkflowRecord:
    """A validated workflow manifest plus its on-disk provenance."""

    manifest: dict[str, Any]
    identity: ManifestIdentity
    source_path: Path

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def intent(self) -> str:
        return str(self.manifest["intent"])

    @property
    def supported_backends(self) -> list[str]:
        bindings = self.manifest.get("bindings") or {}
        return sorted(bindings.keys())


@dataclass(frozen=True)
class RegistryLoadError:
    """Reported when a manifest file failed to load. Held, not raised, so
    the registry can surface multiple problems at once without aborting
    discovery for healthy manifests."""

    source_path: Path
    message: str


class WorkflowRegistry:
    """Pure in-memory registry of workflow manifests.

    The registry is constructed empty and populated via :meth:`load_directory`
    or :meth:`add_manifest`. It is intentionally not a singleton so tests can
    build small fixture registries without touching disk twice.
    """

    def __init__(self) -> None:
        self._records: dict[str, WorkflowRecord] = {}
        self._errors: list[RegistryLoadError] = []

    @classmethod
    def from_directory(cls, directory: str | Path) -> "WorkflowRegistry":
        registry = cls()
        registry.load_directory(directory)
        return registry

    def load_directory(self, directory: str | Path) -> None:
        """Load every ``*.yml`` / ``*.yaml`` manifest in ``directory``.

        Malformed manifests are collected in :attr:`errors` so callers can
        decide whether to fail hard or proceed with healthy entries.
        """
        root = Path(directory)
        if not root.is_dir():
            raise ManifestError(f"manifest directory not found: {root}")

        for path in sorted(_iter_manifest_files(root)):
            try:
                manifest = load_workflow_manifest(path)
                self.add_manifest(manifest, source_path=path)
            except (ManifestError, OSError, ValueError) as exc:
                self._errors.append(
                    RegistryLoadError(source_path=path, message=str(exc))
                )

    def add_manifest(
        self,
        manifest: dict[str, Any],
        *,
        source_path: Path | str | None = None,
    ) -> WorkflowRecord:
        identity = manifest_identity(manifest)
        if identity.id in self._records:
            raise ManifestError(
                f"duplicate workflow manifest id: {identity.id}"
            )
        record = WorkflowRecord(
            manifest=copy.deepcopy(manifest),
            identity=identity,
            source_path=Path(source_path) if source_path else Path(""),
        )
        self._records[identity.id] = record
        return record

    @property
    def errors(self) -> list[RegistryLoadError]:
        return list(self._errors)

    def ids(self) -> list[str]:
        return sorted(self._records)

    def records(self) -> list[WorkflowRecord]:
        return [self._records[id_] for id_ in self.ids()]

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        return self._records.get(workflow_id)

    def by_intent(self, intent: str) -> list[WorkflowRecord]:
        return [r for r in self.records() if r.intent == intent]

    def summarize_workflow(
        self, workflow_id: str, audience: str = "client"
    ) -> dict[str, Any] | None:
        record = self.get(workflow_id)
        if record is None:
            return None
        return summarize_workflow(record, audience)

    def summarize_catalog(self, audience: str = "client") -> dict[str, Any]:
        return summarize_catalog(self.records(), audience)


def _iter_manifest_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in MANIFEST_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def summarize_workflow(record: WorkflowRecord, audience: str) -> dict[str, Any]:
    """Return a sanitized workflow summary for the given audience.

    The client summary intentionally avoids any field that could leak local
    filesystem layout or raw backend graph structure. Admin and internal
    summaries layer on provenance and backend binding detail.
    """
    _check_audience(audience)
    manifest = record.manifest

    summary: dict[str, Any] = {
        "id": record.id,
        "intent": record.intent,
        "version": manifest.get("version"),
        "schema_version": manifest.get("schema_version"),
        "display_name": manifest.get("display_name"),
        "description": manifest.get("description"),
        "exposure": _manifest_string(manifest, "exposure", default="default"),
        "trust_tier": _manifest_string(
            manifest,
            "trust_tier",
            default="stable" if record.identity.trusted else "experimental",
        ),
        "resource_tier": _manifest_string(
            manifest, "resource_tier", default="batch"
        ),
        "tags": _manifest_tags(manifest),
        "inputs": _portable_inputs(manifest.get("inputs") or {}),
        "outputs": _portable_outputs(manifest.get("outputs") or {}),
        "supported_backends": record.supported_backends,
    }

    if audience == "client":
        return summary

    summary["manifest_hash"] = record.identity.manifest_hash
    summary["source"] = record.identity.source
    summary["trusted"] = record.identity.trusted
    summary["min_gateway_version"] = manifest.get("min_gateway_version")
    summary["source_path"] = (
        str(record.source_path) if str(record.source_path) else None
    )
    summary["bindings"] = _binding_summaries(
        manifest.get("bindings") or {}, audience=audience
    )
    return summary


def summarize_catalog(
    records: Iterable[WorkflowRecord], audience: str
) -> dict[str, Any]:
    _check_audience(audience)
    items = [summarize_workflow(r, audience) for r in records]
    return {
        "schema_version": 1,
        "audience": audience,
        "workflows": items,
    }


def _portable_inputs(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Project manifest inputs to the portable contract fields only.

    Fields exposed: name, type, required, minimum/maximum, enum, default,
    description. Anything else stays internal (manifests may grow custom
    keys; we don't want to leak them by accident).
    """
    portable_keys = (
        "type",
        "required",
        "minimum",
        "maximum",
        "enum",
        "default",
        "description",
    )
    portable: list[dict[str, Any]] = []
    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "type": spec.get("type", "string"),
            "required": bool(spec.get("required", False)),
        }
        for key in portable_keys:
            if key in {"type", "required"}:
                continue
            if key in spec:
                entry[key] = spec[key]
        portable.append(entry)
    return portable


def _portable_outputs(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    portable: list[dict[str, Any]] = []
    for name, spec in outputs.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {"name": name}
        if "media_type" in spec:
            entry["media_type"] = spec["media_type"]
        if "description" in spec:
            entry["description"] = spec["description"]
        portable.append(entry)
    return portable


def _binding_summaries(
    bindings: dict[str, Any], *, audience: str
) -> list[dict[str, Any]]:
    """Summarize backend bindings.

    Admin audience sees backend names plus the workflow artifact reference
    (e.g. ComfyUI workflow JSON path) and the list of inputs each binding
    routes — but not the full map. Internal audience also gets the raw
    map so route receipts and diagnostics can show node-level detail.

    Client audience never sees this — :func:`summarize_workflow` short-
    circuits before calling here.
    """
    summaries: list[dict[str, Any]] = []
    for backend, spec in bindings.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {"backend": backend}
        if "workflow" in spec:
            entry["workflow_artifact"] = spec["workflow"]
        if "endpoint" in spec:
            entry["endpoint"] = spec["endpoint"]
        mapping = spec.get("map")
        if isinstance(mapping, dict):
            entry["mapped_inputs"] = sorted(mapping.keys())
            if audience == "internal":
                entry["map"] = copy.deepcopy(mapping)
        summaries.append(entry)
    return summaries


def _manifest_string(manifest: dict[str, Any], key: str, *, default: str) -> str:
    value = manifest.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _manifest_tags(manifest: dict[str, Any]) -> list[str]:
    value = manifest.get("tags")
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in tags:
            tags.append(item)
    return tags


def _check_audience(audience: str) -> None:
    if audience not in VALID_AUDIENCES:
        raise ValueError(
            f"audience must be one of {VALID_AUDIENCES!r}, got {audience!r}"
        )
