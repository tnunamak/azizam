"""The checked-workflow cache of ``manifests.load_workflow_manifest`` and the document copy the reload path uses.

A reload reads every bundled workflow file again. The cache keeps each checked document by the hash of its bytes, so it
must check again whenever the bytes, the shipped binding descriptors or the bundled frontend rows change, never keep a
file that failed, and never let one caller change what the next one reads."""
from __future__ import annotations

import copy
import datetime
import uuid
from pathlib import Path

import pytest

from gateway import binding_descriptors, frontend_shapes, manifests
from gateway.data_copy import copy_document

BUNDLED = Path(__file__).resolve().parents[1] / "manifests" / "workflows"


def _workflow_text() -> str:
    """A bundled workflow file's text with a fresh comment, so no earlier call has cached these bytes."""
    source = sorted(BUNDLED.glob("*.yml"))[0]
    return source.read_text(encoding="utf-8") + f"\n# {uuid.uuid4().hex}\n"


def _counting_checks(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = manifests.enforce_contract
    monkeypatch.setattr(manifests, "enforce_contract",
                        lambda manifest, descriptors=None: calls.append(1) or real(manifest, descriptors))
    return calls


def test_unchanged_bytes_are_checked_once_and_each_caller_gets_its_own_copy(tmp_path, monkeypatch) -> None:
    path = tmp_path / "workflow.yml"
    path.write_text(_workflow_text(), encoding="utf-8")
    calls = _counting_checks(monkeypatch)
    for _ in range(2):   # the first call checks and keeps the document; the second is served from the cache
        served = manifests.load_workflow_manifest(path)
        served["id"] = "changed.by.a.caller"
        served["inputs"].clear()
    third = manifests.load_workflow_manifest(path)
    assert third["id"] != "changed.by.a.caller" and third["inputs"]
    assert len(calls) == 1


def test_changed_bytes_are_checked_again_and_a_failing_file_is_never_kept(tmp_path, monkeypatch) -> None:
    path = tmp_path / "workflow.yml"
    good = _workflow_text()
    path.write_text(good, encoding="utf-8")
    manifests.load_workflow_manifest(path)
    path.write_text(good.replace("schema_version: 2", "schema_version: 1", 1), encoding="utf-8")
    calls = _counting_checks(monkeypatch)
    for _ in range(2):
        with pytest.raises(Exception, match="schema version"):
            manifests.load_workflow_manifest(path)
    assert len(calls) == 2


def test_a_replaced_descriptor_table_or_rows_file_checks_again(tmp_path, monkeypatch) -> None:
    path = tmp_path / "workflow.yml"
    path.write_text(_workflow_text(), encoding="utf-8")
    manifests.load_workflow_manifest(path)
    calls = _counting_checks(monkeypatch)
    shipped = dict(binding_descriptors.cached_shipped_binding_descriptors())
    monkeypatch.setattr(binding_descriptors, "cached_shipped_binding_descriptors", lambda: shipped)
    manifests.load_workflow_manifest(path)
    assert len(calls) == 1
    rows = tuple(list(frontend_shapes.bundled_rows()))   # an equal table, but not the cached object
    monkeypatch.setattr(frontend_shapes, "bundled_rows", lambda: rows)
    manifests.load_workflow_manifest(path)
    assert len(calls) == 2


@pytest.mark.parametrize("document", [
    {"a": [1, 2.5, None, True, "x", b"y"], "b": {"c": (1, [2])}, "d": {1, 2}, "e": frozenset({"f"})},
    {"when": datetime.date(2026, 1, 2), "at": [datetime.datetime(2026, 1, 2, 3, 4)]},
    [], {}, "text", 7,
])
def test_the_document_copy_equals_deepcopy_and_shares_no_container(document) -> None:
    copied = copy_document(document)
    assert copied == copy.deepcopy(document)
    assert type(copied) is type(document)
    if isinstance(document, (dict, list)) and document:
        assert copied is not document
        inner = next(iter(document.values())) if isinstance(document, dict) else document[0]
        assert next(iter(copied.values())) is not inner


def test_the_document_copy_keeps_shared_and_cyclic_containers_as_deepcopy_does() -> None:
    shared = {"anchor": [1]}
    document = {"first": shared, "second": shared}
    copied = copy_document(document)
    assert copied["first"] is copied["second"] and copied["first"] is not shared
    cycle: list = []
    cycle.append(cycle)
    copied_cycle = copy_document(cycle)
    assert copied_cycle[0] is copied_cycle and copied_cycle is not cycle
