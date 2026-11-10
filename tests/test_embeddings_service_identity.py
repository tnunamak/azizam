"""The embeddings service is the service selected for the embeddings capability, not the record of a fixed id.

An install may name its embeddings engine "vectors-one". The gateway then derives the same settings and probes the
same health as for a record named "embeddings" that declares the capability. The release defaults stay keyed by
role, and the published backend label stays "embeddings".
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

import proxy
from gateway import engine_record_defaults
from tests.support.config_snapshot import derived_patch

KEYS = ("EMBEDDINGS_HOST", "EMBEDDINGS_ENABLED", "EMBEDDINGS_TIMEOUT", "EMBEDDINGS_HEALTH_ENDPOINT", "EMBEDDINGS_BACKEND_MODEL")


def _config(**backends: Any) -> dict[str, Any]:
    """The test config with only *backends* as embeddings services."""
    cfg = dict(copy.deepcopy(proxy.config))
    others = dict(cfg.get("backends") or {})
    for service_id in proxy.backends_for_capability("embeddings", backends=others, include_disabled=True):
        others.pop(service_id)
    others.pop("embeddings", None)
    cfg["backends"] = {**others, **backends}
    return cfg


def _vectors(port: int, **extra: Any) -> dict[str, Any]:
    return {"host": "127.0.0.1", "port": port, "capabilities": ["openai", "embeddings"], **extra}


def _derived(cfg: dict[str, Any]) -> dict[str, Any]:
    derived = proxy._derive_backend_constants(cfg)
    return {key: derived[key] for key in KEYS}


def test_a_renamed_service_derives_the_same_settings_as_embeddings():
    full = {"timeout": 45, "health_endpoint": "/ready", "model_id": "vectors-model"}
    legacy = _derived(_config(embeddings=_vectors(18301, **full)))
    renamed = _derived(_config(**{"vectors-one": _vectors(18301, **full)}))
    assert renamed == legacy
    assert legacy == {
        "EMBEDDINGS_HOST": "http://127.0.0.1:18301", "EMBEDDINGS_ENABLED": True, "EMBEDDINGS_TIMEOUT": 45,
        "EMBEDDINGS_HEALTH_ENDPOINT": "/ready", "EMBEDDINGS_BACKEND_MODEL": "vectors-model",
    }


def test_a_renamed_record_that_names_no_values_takes_the_roles_release_defaults():
    defaults = engine_record_defaults.release_defaults()["embeddings"]
    derived = _derived(_config(**{"vectors-one": _vectors(18301)}))
    assert derived["EMBEDDINGS_TIMEOUT"] == int(defaults["timeout"])
    assert derived["EMBEDDINGS_HEALTH_ENDPOINT"] == defaults["health_endpoint"]
    assert derived["EMBEDDINGS_BACKEND_MODEL"] == defaults["model_id"]


def test_a_record_with_the_old_id_but_no_capability_is_not_the_embeddings_service():
    derived = _derived(_config(embeddings={"host": "127.0.0.1", "port": 18301, "capabilities": ["openai", "chat"]}))
    assert derived["EMBEDDINGS_HOST"] == "" and derived["EMBEDDINGS_ENABLED"] is False


def test_a_disabled_selected_service_is_off_and_no_other_takes_its_place():
    derived = _derived(_config(**{
        "vectors-one": _vectors(18301, priority=10, enabled=False), "embeddings": _vectors(18302, priority=20),
    }))
    assert derived["EMBEDDINGS_HOST"] == "http://127.0.0.1:18301" and derived["EMBEDDINGS_ENABLED"] is False


def test_the_highest_priority_service_is_selected_and_a_tie_goes_to_the_lower_id():
    derived = _derived(_config(**{"vectors-b": _vectors(18301, priority=50), "vectors-a": _vectors(18302, priority=60)}))
    assert derived["EMBEDDINGS_HOST"] == "http://127.0.0.1:18301"
    derived = _derived(_config(**{"vectors-b": _vectors(18303), "vectors-a": _vectors(18304)}))
    assert derived["EMBEDDINGS_HOST"] == "http://127.0.0.1:18304"


def test_the_derived_settings_come_from_the_candidate_not_the_live_config(monkeypatch):
    monkeypatch.setattr(proxy, "config", _config(embeddings=_vectors(18301, model_id="live-model")))
    derived = _derived(_config(**{"vectors-one": _vectors(18401, model_id="candidate-model")}))
    assert derived["EMBEDDINGS_HOST"] == "http://127.0.0.1:18401"
    assert derived["EMBEDDINGS_BACKEND_MODEL"] == "candidate-model"


def test_the_health_check_probes_the_selected_service(monkeypatch):
    probed: list[str] = []

    async def probe(service_id: str) -> bool:
        probed.append(service_id)
        return True

    monkeypatch.setattr(proxy, "check_service_health", probe)
    monkeypatch.setattr(proxy, "config", _config(**{"vectors-one": _vectors(18301)}))
    with derived_patch("EMBEDDINGS_ENABLED", True):
        assert asyncio.run(proxy.check_embeddings_health()) is True
    assert probed == ["vectors-one"]
    monkeypatch.setattr(proxy, "config", _config())
    with derived_patch("EMBEDDINGS_ENABLED", True):
        assert asyncio.run(proxy.check_embeddings_health()) is False
    assert probed == ["vectors-one"]
