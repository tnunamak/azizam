"""Resolve directly invocable workflows by their public model names."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


class AmbiguousWorkflowStem(ValueError):
    """Two records of one intent bind graph files with the same stem."""


def compatibility_name(contract: Mapping[str, Any]) -> str:
    """The public model name declared by the record or its short id."""
    workflow_id = str(contract.get("id") or "")
    if contract.get("compatibility_name"):
        return contract["compatibility_name"]
    for binding in (contract.get("bindings") or {}).values():
        path = binding.get("workflow") if isinstance(binding, Mapping) else None
        if isinstance(path, str) and path:
            return PurePosixPath(path).stem
    return workflow_id.removeprefix(f"{contract.get('intent')}.")


def matches_name(contract: Mapping[str, Any], name: str) -> bool:
    return name in (contract.get("id"), compatibility_name(contract), *(contract.get("aliases") or ()))


def _binding_stems(manifest: Mapping[str, Any]) -> set[str]:
    bindings = manifest.get("bindings")
    if not isinstance(bindings, Mapping):
        return set()
    stems = set()
    for binding in bindings.values():
        artifact = binding.get("workflow") if isinstance(binding, Mapping) else None
        if isinstance(artifact, str) and artifact:
            stems.add(PurePosixPath(artifact).stem)
    return stems


def find_workflow_record(records: Iterable[Any], intent: str, name: str) -> Any:
    """The record of ``intent`` whose binding file stem is ``name``; else the one whose id is ``intent.name``.

    When several records of the intent share the stem, the one among them whose id is ``intent.name`` wins; with no
    such record it raises ``AmbiguousWorkflowStem``, so a pick never depends on registry order. Returns None when
    neither exists.
    """
    by_stem = []
    declared = []
    by_id = None
    for record in records:
        manifest = record.manifest
        if manifest.get("intent") != intent:
            continue
        if name == manifest.get("id"):
            return record
        if name == manifest.get("compatibility_name") or name in (manifest.get("aliases") or ()):
            declared.append(record)
        if name in _binding_stems(manifest):
            by_stem.append(record)
        if manifest.get("id") == f"{intent}.{name}":
            by_id = record
    if len(declared) == 1:
        return declared[0]
    if len(declared) > 1:
        raise AmbiguousWorkflowStem(f"{intent} has more than one workflow named {name!r}")
    if len(by_stem) > 1 and by_id in by_stem:
        # Two packages may ship graphs with one file name; the record whose id is the name is the one it names.
        return by_id
    if len(by_stem) > 1:
        ids = sorted(str(r.manifest.get("id")) for r in by_stem)
        raise AmbiguousWorkflowStem(f"{intent} records {ids} share the graph file stem {name!r}")
    return by_stem[0] if by_stem else by_id


def other_intent_owns_stem(records: Iterable[Any], intent: str, name: str) -> bool:
    """Whether a record of some other intent binds a graph file with stem ``name``.

    A request for ``intent`` that names such a model asks for another kind of workflow, and a route refuses it
    rather than run that graph under the wrong role map.
    """
    return any(
        record.manifest.get("intent") != intent
        and (matches_name(record.manifest, name) or name in _binding_stems(record.manifest))
        for record in records
    )
