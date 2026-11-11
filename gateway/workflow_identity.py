"""Find the registry record a model name stands for, by data and not by spelling.

A model listed by ``/v1/models`` is named by its graph file stem (``flux2-klein-4b-edit``), while the
record id is the manifest id (``image.edit.flux2-klein-4b``). The two differ for shipped workflows, so
the stem is read from the record's own binding file, the same fact the listing reads.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


class AmbiguousWorkflowStem(ValueError):
    """Two records of one intent bind graph files with the same stem."""


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
    by_id = None
    for record in records:
        manifest = record.manifest
        if manifest.get("intent") != intent:
            continue
        if name in _binding_stems(manifest):
            by_stem.append(record)
        if manifest.get("id") == f"{intent}.{name}":
            by_id = record
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
        record.manifest.get("intent") != intent and name in _binding_stems(record.manifest)
        for record in records
    )
