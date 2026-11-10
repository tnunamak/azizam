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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from gateway.workflow_view import backend_ids, settable_inputs
from gateway.manifests import (
    BUNDLED_PROVENANCE,
    UNKNOWN_PROVENANCE,
    ManifestError,
    ManifestIdentity,
    Provenance,
    load_workflow_manifest,
    manifest_identity,
)
from gateway.workflow_policy import PolicyFacts, release_policy
from gateway import video_grid

VALID_AUDIENCES = ("client", "admin", "internal")
MANIFEST_GLOBS = ("*.yml", "*.yaml")
# The workflow manifests the release ships: the release layer under the owner's stored workflows
# (docs/design/CONFIG_HOMES.md, 3.4 and 8.4).
BUNDLED_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "manifests" / "workflows"


@dataclass(frozen=True)
class WorkflowRecord:
    """A validated workflow manifest, where it came from, and the facts about it that are not in the document.

    ``manifest`` is the schema 2 contract (CODE_TO_DATA_S3): the document as stored, never converted on load. The identity
    carries the provenance (``trusted``, ``source``), which the loader sets from the origin of the record. ``facts`` are the
    record's policy facts; ``None`` means the record states none, so it needs configuration and runs nothing (D2)."""

    manifest: dict[str, Any]
    identity: ManifestIdentity
    source_path: Path
    facts: PolicyFacts | None = field(default=None, compare=False, repr=False)

    @property
    def contract(self) -> dict[str, Any]:
        """What readers read: the stored document is the contract."""
        return self.manifest

    @property
    def needs_configuration(self) -> bool:
        return self.facts is None

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def intent(self) -> str:
        return str(self.contract["intent"])

    @property
    def supported_backends(self) -> list[str]:
        return sorted(backend_ids(self.contract))


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
    def from_directory(cls, directory: str | Path, *, policy: Mapping[str, PolicyFacts] | None = None) -> "WorkflowRegistry":
        registry = cls()
        registry.load_directory(directory, policy=policy)
        return registry

    def load_directory(
        self, directory: str | Path, *, provenance: Provenance = BUNDLED_PROVENANCE,
        policy: Mapping[str, PolicyFacts] | None = None,
    ) -> None:
        """Load every ``*.yml`` / ``*.yaml`` manifest in ``directory``.

        Malformed manifests are collected in :attr:`errors` so callers can
        decide whether to fail hard or proceed with healthy entries. The directory is the release's own, so each record has
        the given provenance (reviewed and local by default) and the facts that ``policy`` (the release policy file by
        default) holds for its id; an id the policy does not list needs configuration.
        """
        if policy is None:
            try:
                policy = release_policy()
            except ValueError as exc:   # a policy file that cannot be read states nothing
                self._errors.append(RegistryLoadError(source_path=Path(directory), message=str(exc)))
                policy = {}
        root = Path(directory)
        if not root.is_dir():
            raise ManifestError(f"manifest directory not found: {root}")

        for path in sorted(_iter_manifest_files(root)):
            try:
                manifest = load_workflow_manifest(path)
                self.add_manifest(
                    manifest, source_path=path, provenance=provenance, facts=policy.get(str(manifest["id"])),
                    _check_variants=False,  # checked once below; a per-file check would copy the registry each time
                )
            except (ManifestError, OSError, ValueError) as exc:
                self._errors.append(
                    RegistryLoadError(source_path=path, message=str(exc))
                )
        try:
            video_grid.check_video_variants(self.records())
        except ValueError as exc:   # two variants claim one reference: a registry error, not a request error
            self._errors.append(RegistryLoadError(source_path=root, message=str(exc)))

    def add_manifest(
        self,
        manifest: dict[str, Any],
        *,
        source_path: Path | str | None = None,
        replace: bool = False,
        provenance: Provenance = UNKNOWN_PROVENANCE,
        facts: PolicyFacts | None = None,
        _check_variants: bool = True,
    ) -> WorkflowRecord:
        """Add a manifest. A second one with the same id is refused, unless ``replace`` says it takes the place of the first.
        The provenance and the facts are the record's, set by whoever knows where it came from; none means not reviewed and
        needs configuration."""
        identity = manifest_identity(manifest, provenance)
        if identity.id in self._records and not replace:
            raise ManifestError(
                f"duplicate workflow manifest id: {identity.id}"
            )
        record = WorkflowRecord(
            manifest=copy.deepcopy(manifest),
            identity=identity,
            source_path=Path(source_path) if source_path else Path(""),
            facts=facts,
        )
        if _check_variants:
            candidate = dict(self._records)
            candidate[identity.id] = record
            video_grid.check_video_variants(candidate.values())
        self._records[identity.id] = record
        return record

    @property
    def errors(self) -> list[RegistryLoadError]:
        return list(self._errors)

    def ids(self) -> list[str]:
        return sorted(self._records)

    def records(self) -> list[WorkflowRecord]:
        current = self._records  # one read: a swap (``adopt``) between two reads must not drop an id
        return [current[id_] for id_ in sorted(current)]

    def contents(self) -> tuple[dict[str, WorkflowRecord], list[RegistryLoadError]]:
        """What the registry holds now, for ``adopt`` or a test that puts it back."""
        return self._records, self._errors

    def adopt(self, other: "WorkflowRegistry") -> None:
        """Take the records of ``other``. The object stays the same, so every holder of it serves the new records.

        A reload builds the next registry whole and swaps it in here, so a reader sees the old records or the new ones.
        """
        contents = other.contents()
        video_grid.check_video_variants(contents[0].values())
        self.restore(contents)

    def restore(self, contents: tuple[dict[str, WorkflowRecord], list[RegistryLoadError]]) -> None:
        self._records, self._errors = contents

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
    contract = record.contract
    facts = record.facts

    summary: dict[str, Any] = {
        "id": record.id,
        "intent": record.intent,
        "version": contract.get("version"),
        "schema_version": contract.get("schema_version"),
        "display_name": contract.get("display_name"),
        "purpose": contract.get("purpose"),
        "description": contract.get("description"),
        "exposure": facts.exposure if facts else None,
        "trust_tier": facts.trust_tier if facts else None,
        "resource_tier": facts.resource_tier if facts else None,
        "tags": list(facts.tags) if facts else [],
        "typical_seconds": _manifest_number(contract, "typical_seconds"),
        "inputs": _portable_inputs(settable_inputs(contract)),
        "outputs": _portable_outputs(contract.get("outputs") or {}),
        "supported_backends": record.supported_backends,
    }

    if facts is None:
        # No policy facts: the record is closed until its owner configures it (CODE_TO_DATA_S3, D2).
        summary["needs_configuration"] = True

    if audience == "client":
        return summary

    summary["manifest_hash"] = record.identity.manifest_hash
    summary["source"] = record.identity.source
    summary["trusted"] = record.identity.trusted
    summary["min_gateway_version"] = contract.get("min_gateway_version")
    summary["source_path"] = (
        str(record.source_path) if str(record.source_path) else None
    )
    summary["bindings"] = _binding_summaries(
        contract.get("bindings") or {}, audience=audience
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
    description, label (a short noun phrase such as "a face photo"). Anything else stays internal (manifests may grow custom
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
        "label",
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
        if "label" in spec:
            entry["label"] = spec["label"]
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
        entry: dict[str, Any] = {"backend": str(spec.get("service") or backend)}
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


def _manifest_number(manifest: dict[str, Any], key: str) -> float | int | None:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _check_audience(audience: str) -> None:
    if audience not in VALID_AUDIENCES:
        raise ValueError(
            f"audience must be one of {VALID_AUDIENCES!r}, got {audience!r}"
        )


def _manifest_signature(directory: Path) -> tuple[tuple[str, int, int], ...]:
    rows = []
    for path in sorted(_iter_manifest_files(directory)):
        stat = path.stat()
        rows.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(rows)


@lru_cache(maxsize=2)
def _bundled_manifests(directory: str, signature: tuple[tuple[str, int, int], ...]) -> dict[str, dict[str, Any]]:
    del signature  # part of the key: a changed file loads again
    return {record.id: record.manifest for record in WorkflowRegistry.from_directory(directory).records()}


def bundled_workflow_manifests(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{id: manifest}`` of the manifests the release ships. A fresh copy; a manifest that does not load is left out."""
    root = Path(directory or BUNDLED_WORKFLOWS_DIR)
    return copy.deepcopy(_bundled_manifests(str(root), _manifest_signature(root)))
