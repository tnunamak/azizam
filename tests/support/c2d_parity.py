"""Shared helpers for the code-to-data parity goldens (tests/fixtures/c2d_parity/).

A golden is today's output stored as literal JSON. The tests only READ goldens; the
one writer is ``tests/fixtures/c2d_parity/capture.py``, so a refresh is a deliberate,
visible diff.

Serializer: ``canonical`` returns the data as JSON text with sorted keys and two-space
indent. Dict key order is never part of a contract here. List order is kept as the code
produces it, unless the capture function sorts a list itself and says so.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "c2d_parity"
MANIFESTS = ROOT / "manifests" / "workflows"


def jsonable(value: Any) -> Any:
    """Plain JSON data for a value: dataclasses to dicts, tuples/sets/frozensets to lists (sets sorted)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((jsonable(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def canonical(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def canonical_hash(value: Any) -> str:
    compact = json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def load_golden(name: str) -> Any:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def write_golden(name: str, data: Any) -> None:
    """Used by capture.py only. Tests never call this."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path(name).write_text(canonical(data), encoding="utf-8")


def as_golden(value: Any) -> Any:
    """What a golden file holds for this value: the value after a JSON round trip."""
    return json.loads(canonical(value))


@contextlib.contextmanager
def model_files_absent() -> Iterator[None]:
    """Stub the one disk read in gateway.model_files (``_is_installed``): every declared file is absent."""
    with mock.patch("gateway.model_files._is_installed", lambda path: False):
        yield


@contextlib.contextmanager
def model_files_present() -> Iterator[None]:
    """Stub ``gateway.model_files._is_installed``: every declared file is installed."""
    with mock.patch("gateway.model_files._is_installed", lambda path: True):
        yield


# A fixed policy for every shipped workflow, so no record depends on registry/workflow_policy.yml.
def fixed_policy(ids: list[str]) -> dict[str, Any]:
    from gateway.workflow_policy import PolicyFacts

    return {
        workflow_id: PolicyFacts(
            exposure="default", trust_tier="stable", resource_tier="batch", resource_class="media", tags=()
        )
        for workflow_id in ids
    }


def shipped_manifest_ids() -> list[str]:
    import yaml

    ids = []
    for path in sorted(MANIFESTS.glob("*.yml")):
        ids.append(str(yaml.safe_load(path.read_text(encoding="utf-8"))["id"]))
    return sorted(ids)


def shipped_registry():
    """The registry of every shipped workflow manifest, with the fixed policy above."""
    from gateway.discovery import WorkflowRegistry

    registry = WorkflowRegistry.from_directory(MANIFESTS, policy=fixed_policy(shipped_manifest_ids()))
    assert registry.errors == [], registry.errors
    return registry
