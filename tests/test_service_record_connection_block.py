"""A service record may give its ``connection`` block at the top level (engine contract, section 2) or in its config.
The store keeps it in the config; it never drops it silently."""
from __future__ import annotations

import pytest

from gateway import service_registry

CONNECTION = {"transport": "http", "address": "http://127.0.0.1:7001", "control_address": "http://127.0.0.1:7002"}


def record(**extra):
    return {"id": "fixture", "kind": "llm", "config": {"address": "http://127.0.0.1:7001"}, **extra}


def test_a_top_level_connection_is_stored_in_the_config() -> None:
    stored = service_registry.normalize_service_record("fixture", record(connection=CONNECTION))
    assert stored["config"]["connection"] == CONNECTION
    assert "connection" not in stored


def test_the_same_block_in_both_places_is_stored_once() -> None:
    raw = record(connection=CONNECTION)
    raw["config"]["connection"] = dict(CONNECTION)
    assert service_registry.normalize_service_record("fixture", raw)["config"]["connection"] == CONNECTION


def test_two_different_blocks_are_refused_not_dropped() -> None:
    raw = record(connection=CONNECTION)
    raw["config"]["connection"] = dict(CONNECTION, control_address="http://127.0.0.1:7003")
    with pytest.raises(ValueError, match="top level and in config"):
        service_registry.normalize_service_record("fixture", raw)


def test_a_connection_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValueError, match="connection must be a mapping"):
        service_registry.normalize_service_record("fixture", record(connection="http://127.0.0.1:7001"))


def test_a_registry_write_keeps_a_top_level_connection() -> None:
    registry, stored = service_registry.upsert_service_in_registry(
        {"services": {}}, service_id="fixture", raw=record(connection=CONNECTION))
    assert registry["services"]["fixture"]["config"]["connection"]["control_address"] == "http://127.0.0.1:7002"
