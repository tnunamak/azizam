"""`/api/ps` lists what the path-identity backends have loaded, with no health probe and no owner label.

The loaded ids come from every path-identity backend, so no one backend owns
them; the summaries carried the health winner as their owner, which named an
empty backend as the owner of another backend's model.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.protocols import ollama as ollama_protocol  # noqa: E402


@pytest.fixture
def inventory(monkeypatch):
    state = SimpleNamespace(
        paths={"a": "", "b": "/models/model-b.gguf"},
        active="a",
        health_reads=0,
        declaration_reads=[],
        litellm=False,
    )

    async def active():
        state.health_reads += 1
        return state.active

    def declarations(backend, path):
        state.declaration_reads.append((backend, path))
        return {"declared-" + backend}

    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: SimpleNamespace(enabled=state.litellm))
    monkeypatch.setattr(proxy, "_path_identity_backends", lambda: ["a", "b"])
    monkeypatch.setattr(proxy, "currently_loaded_model", lambda backend: state.paths.get(backend, ""))
    monkeypatch.setattr(proxy, "get_active_backend", active)
    monkeypatch.setattr(proxy, "USE_LOADED_MODEL_ALIASES", frozenset({"current", "current-fast"}))
    monkeypatch.setattr(
        proxy, "_role_catalog_ids_for_loaded_path",
        lambda *, backend, loaded_path, loaded_model_id: {"role-" + backend},
    )
    monkeypatch.setattr(proxy, "_declarations_for_loaded_model", declarations)
    monkeypatch.setattr(proxy, "_entry_created_ts", lambda path: 123)
    monkeypatch.setattr(proxy.os.path, "getsize", lambda path: 456)
    return state


def _summaries() -> list[dict[str, Any]]:
    return asyncio.run(proxy._ollama_loaded_model_summaries(cast(Any, object())))


def test_summaries_name_no_owner_and_make_no_health_probe(inventory):
    rows = {row["id"]: row for row in _summaries()}
    assert set(rows) == {"model-b.gguf", "current", "current-fast", "role-b"}
    for row in rows.values():
        assert "owned_by" not in row and "azizam_backend" not in row
        assert row["azizam_loaded"] is True
    assert rows["model-b.gguf"]["size"] == rows["model-b.gguf"]["size_vram"] == 456
    assert inventory.health_reads == 0
    assert inventory.declaration_reads == []


def test_ps_response_lists_the_loaded_names_and_sizes(inventory):
    models = ollama_protocol.ps_response_from_model_summaries(_summaries())["models"]
    by_name = {model["name"]: model for model in models}
    assert set(by_name) == {"model-b.gguf", "current", "current-fast", "role-b"}
    assert by_name["model-b.gguf"]["size"] == 456


def test_same_basename_on_two_backends_is_listed_once(inventory):
    inventory.paths = {"a": "/one/shared.gguf", "b": "/two/shared.gguf"}
    names = [row["id"] for row in _summaries()]
    assert names.count("shared.gguf") == 1


def test_nothing_loaded_lists_nothing(inventory):
    inventory.paths = {"a": "", "b": ""}
    assert _summaries() == []


def test_litellm_enabled_lists_nothing_without_reads(inventory, monkeypatch):
    inventory.litellm = True
    monkeypatch.setattr(proxy, "currently_loaded_model", lambda backend: pytest.fail("loaded-state read"))
    assert _summaries() == []
    assert inventory.health_reads == 0


def test_plain_id_read_skips_the_health_probe(inventory):
    ids = asyncio.run(proxy._current_loaded_llm_model_ids())
    assert ids == {"current", "current-fast", "/models/model-b.gguf", "model-b.gguf", "role-b"}
    assert inventory.health_reads == 0


def test_metadata_read_uses_the_active_backend_only(inventory):
    inventory.paths["a"] = "/models/model-a.gguf"
    metadata: set[str] = set()
    ids = asyncio.run(proxy._current_loaded_llm_model_ids(metadata_ids=metadata))
    assert {"/models/model-a.gguf", "/models/model-b.gguf", "role-a", "role-b"} <= ids
    assert inventory.health_reads == 1
    assert metadata == {"declared-a"}
    assert inventory.declaration_reads == [("a", "/models/model-a.gguf")]


def test_empty_active_backend_gets_no_metadata_from_another(inventory):
    metadata: set[str] = set()
    asyncio.run(proxy._current_loaded_llm_model_ids(metadata_ids=metadata))
    assert metadata == set()
    assert inventory.declaration_reads == []


def test_a_given_active_backend_is_not_probed_again(inventory):
    inventory.paths["b"] = "/models/model-b.gguf"
    metadata: set[str] = set()
    asyncio.run(proxy._current_loaded_llm_model_ids("b", metadata_ids=metadata))
    assert inventory.health_reads == 0
    assert metadata == {"declared-b"}
