"""Catalog presentation must preserve placement evidence and hide registry IDs."""

import pytest

from gateway.capabilities import Capability
from gateway.models_catalog import build_models_catalog
from gateway.model_presentation import engine_label, model_locality


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:18001", "http://[::1]:18001", "http://localhost:18001"])
def test_direct_loopback_engine_is_local(endpoint):
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="capture-engine", model="demo")],
        service_records={"capture-engine": {"name": "Local test engine", "config": {"base_url": endpoint}}},
    )
    row = catalog["models"][0]
    assert row["engine_label"] == "Local test engine"
    assert row["locality"] == "local"


def test_explicit_remote_model_wins_over_local_routing_address():
    assert model_locality({"config": {"base_url": "http://localhost:18001"}}, {"locality": "remote"}) == "remote"
    assert model_locality({"config": {"base_url": "http://localhost:18001", "remote_provider": True}}, None) == "remote"


@pytest.mark.parametrize("service", [
    {"config": {"base_url": "http://10.0.0.3:18001"}},
    {"config": {"base_url": "https://example.test"}},
    {"config": {"host": "engine-container"}},
    {"kind": "llm-substrate", "config": {"base_url": "http://localhost:18001"}},
    {"kind": "litellm", "config": {"base_url": "http://localhost:18001"}},
    {"id": "litellm", "config": {"base_url": "http://localhost:18001"}},
    {"config": {"execution_substrate": "router", "base_url": "http://localhost:18001"}},
])
def test_unproven_placement_stays_unknown(service):
    assert model_locality(service, None) == "unknown"


def test_engine_label_never_falls_back_to_registry_id():
    assert engine_label({"name": "capture-engine"}, "capture-engine") is None
    assert engine_label({"name": "capture-engine", "config": {"display_name": "Chat engine"}}, "capture-engine") == "Chat engine"


def test_catalog_preserves_keyed_substrate_identity_without_claiming_local_cost():
    catalog = build_models_catalog(
        capabilities=[Capability(intent="chat.completions", backend="litellm", model="demo")],
        service_records={"litellm": {"config": {"base_url": "http://localhost:18001"}}},
    )
    row = catalog["models"][0]
    assert row["locality"] == "unknown"
    assert row["cost"]["kind"] == "unknown"
