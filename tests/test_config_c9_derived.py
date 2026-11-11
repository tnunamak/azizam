"""Registry-derived constants ride the published snapshot (docs/design/CONFIG_HOMES.md, 5.1 and 10.1, slice C9).

Class A (a speech, transcription, embeddings or graph service's address, switch and the chat host map), class B (their
timeouts, default model, workflow directories and adapter binding) and the route policies the protocol frontends describe
are computed by one function from the effective config and the stored registry. A registry edit then reaches every reader
without a restart, and a reload gives the values a fresh boot gives.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import service_registry
from tests.registry_test_helpers import seed_registry_document, write_service_registry_for_test
from tests.support.gateway import use_admin_db
from tests.support.speech_roles import declared

ROOT = Path(__file__).resolve().parent.parent

CLASS_A = (
    "LLM_BACKEND_HOSTS", "TTS_ENABLED", "TTS_HOST", "STT_ENABLED", "STT_HOST", "STT_TIMEOUT",
    "EMBEDDINGS_ENABLED", "EMBEDDINGS_HOST", "COMFY_ENABLED", "COMFY_HOST",
)
CLASS_B = (
    "TTS_TIMEOUT", "EMBEDDINGS_HEALTH_ENDPOINT", "EMBEDDINGS_TIMEOUT", "EMBEDDINGS_BACKEND_MODEL", "COMFY_TIMEOUT",
    "COMFY_DEFAULT_MODEL", "COMFY_VIDEO_TIMEOUT", "COMFY_WORKFLOWS_DIR", "COMFY_VIDEO_WORKFLOWS_DIR", "_GRAPH_BINDING",
)
ROUTE_POLICIES = (
    "_PROTOCOL_ROUTE_REGISTRY", "CANONICAL_PROTOCOL_ROUTE_ALIASES", "_RUNTIME_ROUTE_POLICY",
    "_ASSET_CONTENT_ROUTE_POLICY_BY_KIND", "_AZIZAM_POLICY",
)


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The live gateway on a fresh admin store. The published snapshot is put back after the test (tests/conftest.py)."""
    monkeypatch.setattr(proxy, "config", proxy.config)
    monkeypatch.setattr(proxy, "VIDEO_ALIASES", proxy.VIDEO_ALIASES)
    monkeypatch.setattr(proxy, "AUDIO_ALIASES", proxy.AUDIO_ALIASES)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", proxy.REGISTRY_APPLY_PROBLEM)
    db = use_admin_db(monkeypatch, tmp_path)
    return db


def _backends(rng: random.Random, workflows: Path) -> dict[str, dict[str, Any]]:
    """The four services the constants read, with random addresses, timeouts, models and directories."""
    def port() -> int:
        return rng.randrange(20000, 29000)

    return {
        "tts": {"kind": "tts", "capabilities": ["audio", "speech"], "host": f"203.0.113.{rng.randrange(2, 250)}",
                "port": port(), "timeout": rng.randrange(5, 900), "enabled": rng.random() < 0.7},
        "stt": {"kind": "stt", "capabilities": ["audio", "transcriptions"], "host": f"198.51.100.{rng.randrange(2, 250)}",
                "port": port(), "timeout": rng.randrange(5, 900), "enabled": rng.random() < 0.7},
        "embeddings": {"kind": "embeddings", "capabilities": ["embeddings"], "host": f"192.0.2.{rng.randrange(2, 250)}",
                       "port": port(), "timeout": rng.randrange(5, 900), "enabled": rng.random() < 0.7,
                       "model_id": f"embed-{rng.randrange(1000)}", "health_endpoint": f"/health{rng.randrange(9)}"},
        "comfyui": {"kind": "comfyui", "capabilities": ["images", "videos"], "host": f"203.0.113.{rng.randrange(2, 250)}",
                    "port": port(), "timeout": rng.randrange(5, 900), "enabled": rng.random() < 0.7,
                    "video_timeout": rng.randrange(5, 4000), "default_model": f"model-{rng.randrange(1000)}",
                    "workflows_dir": str(workflows / f"images-{rng.randrange(9)}"),
                    "video_workflows_dir": f"./workflows-{rng.randrange(9)}"},
        "chat-engine": {"kind": "llama", "capabilities": ["chat", "completions"], "host": "198.51.100.77", "port": port(),
                        "enabled": rng.random() < 0.7},
    }


def _store(db: Path, backends: dict[str, Any]) -> None:
    write_service_registry_for_test(seed_registry_document({"backends": backends}), path=db)


def _derived_now(names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(proxy.DERIVED, name) for name in names}


def _booted() -> dict[str, Any]:
    """What a fresh boot computes from the same store: the effective config, then the one derive function."""
    config = proxy._load_effective_runtime_config(proxy._bootstrap_config, boot=True)
    derived = proxy._derive_backend_constants(config, registry=proxy._stored_registry_for_boot())
    # The workflow alias tables are folded from `model_aliases` by the alias slice, not by this one; they stay module names.
    return {name: value for name, value in derived.items() if name not in {"VIDEO_ALIASES", "AUDIO_ALIASES"}}


@pytest.mark.parametrize("seed", range(6))
def test_after_a_registry_edit_and_no_restart_every_derived_value_is_what_a_fresh_boot_computes(live, tmp_path, seed):
    rng = random.Random(seed)
    for _round in range(3):
        _store(live, _backends(rng, tmp_path))
        proxy.reload_runtime_config()
        booted = _booted()
        assert set(booted) >= set(CLASS_A + CLASS_B + ROUTE_POLICIES)
        reloaded = _derived_now(tuple(booted))
        assert reloaded == booted


def test_class_a_a_registry_edit_moves_the_address_the_switch_and_the_host_map(live, tmp_path):
    rng = random.Random(1)
    backends = _backends(rng, tmp_path)
    backends["tts"].update(host="203.0.113.9", port=18880, enabled=False)
    backends["stt"].update(host="203.0.113.8", port=15092, timeout=77, enabled=True)
    backends["embeddings"].update(host="203.0.113.7", port=15061, enabled=True)
    backends["comfyui"].update(host="203.0.113.6", port=17890, enabled=False)
    backends["chat-engine"].update(host="203.0.113.5", port=15000, enabled=True)
    _store(live, backends)
    before = _derived_now(CLASS_A)

    proxy.reload_runtime_config()

    assert _derived_now(CLASS_A) != before
    assert proxy.DERIVED.TTS_HOST == "http://203.0.113.9:18880" and proxy.DERIVED.TTS_ENABLED is False
    assert proxy.DERIVED.STT_HOST == "http://203.0.113.8:15092" and proxy.DERIVED.STT_TIMEOUT == 77
    assert proxy.DERIVED.EMBEDDINGS_HOST == "http://203.0.113.7:15061" and proxy.DERIVED.EMBEDDINGS_ENABLED is True
    assert proxy.DERIVED.COMFY_HOST == "http://203.0.113.6:17890" and proxy.DERIVED.COMFY_ENABLED is False
    assert proxy.DERIVED.LLM_BACKEND_HOSTS["chat-engine"] == "http://203.0.113.5:15000"

    backends["tts"].update(host="203.0.113.4", port=18881, enabled=True)
    _store(live, backends)
    proxy.reload_runtime_config()
    assert proxy.DERIVED.TTS_HOST == "http://203.0.113.4:18881" and proxy.DERIVED.TTS_ENABLED is True


def test_class_b_a_registry_edit_moves_the_timeouts_the_default_model_the_directories_and_the_binding(live, tmp_path):
    rng = random.Random(2)
    backends = _backends(rng, tmp_path)
    backends["tts"]["timeout"] = 41
    backends["embeddings"].update(timeout=42, model_id="embed-edited", health_endpoint="/ready")
    backends["comfyui"].update(timeout=43, video_timeout=44, default_model="graph-edited",
                               workflows_dir=str(tmp_path / "image-flows"), video_workflows_dir="./video-flows")
    _store(live, backends)
    before = _derived_now(CLASS_B)

    proxy.reload_runtime_config()

    assert _derived_now(CLASS_B) != before
    assert proxy.DERIVED.TTS_TIMEOUT == 41
    assert proxy.DERIVED.EMBEDDINGS_TIMEOUT == 42
    assert proxy.DERIVED.EMBEDDINGS_BACKEND_MODEL == "embed-edited" and proxy.DERIVED.EMBEDDINGS_HEALTH_ENDPOINT == "/ready"
    assert proxy.DERIVED.COMFY_TIMEOUT == 43 and proxy.DERIVED.COMFY_VIDEO_TIMEOUT == 44
    assert proxy.DERIVED.COMFY_DEFAULT_MODEL == "graph-edited"
    assert proxy.DERIVED.COMFY_WORKFLOWS_DIR == tmp_path / "image-flows"
    assert proxy.DERIVED.COMFY_VIDEO_WORKFLOWS_DIR == (ROOT / "video-flows").resolve()
    assert proxy.DERIVED._GRAPH_BINDING == proxy._graph_adapter(proxy._GRAPH_CAPABILITY).binding


def test_a_reader_inside_a_request_sees_the_snapshot_the_request_began_under(live, tmp_path):
    """One snapshot per request: a reload that lands mid-request does not move the values that request reads."""
    from gateway import config_runtime

    rng = random.Random(3)
    first = _backends(rng, tmp_path)
    first["tts"].update(host="198.51.100.81", port=18001)
    _store(live, first)
    proxy.reload_runtime_config()
    bound = proxy.CONFIG_HOME.holder.current()
    token = config_runtime._BOUND.set(bound)
    try:
        second = {**first, "tts": {**first["tts"], "host": "198.51.100.82", "port": 18002}}
        _store(live, second)
        proxy.reload_runtime_config()
        assert proxy.DERIVED.TTS_HOST == "http://198.51.100.81:18001"
    finally:
        config_runtime._BOUND.reset(token)
    assert proxy.DERIVED.TTS_HOST == "http://198.51.100.82:18002"


def _frontend_with_policies(path: str, intent: str) -> dict:
    return {
        "id": "acme", "name": "Acme", "enabled": True, "source": "imported",
        "registered_routes": [{
            "methods": ["GET"], "canonical": path, "alias": "/acme-alias/version", "intent": "version",
            "translator": "ollama",
            "runtime_policy": {"protocol": "acme.api", "intent": intent, "capability_resource_tier": "cheap"},
            "azizam_policy": {"protocol": "acme.api", "intent": intent},
        }],
        "asset_content_policies": {"acm": {"protocol": "acme.api", "intent": intent}},
    }


def _write_frontends(db: Path, *frontends: dict) -> None:
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["protocol_frontends"] = {item["id"]: item for item in frontends}
    write_service_registry_for_test(registry, path=db)


def test_route_policies_follow_a_frontend_change_without_a_restart(live):
    path = "/acme/api/version"
    assert path not in proxy.DERIVED._RUNTIME_ROUTE_POLICY

    _write_frontends(live, _frontend_with_policies(path, "acme.version"))
    proxy.reload_runtime_config()
    assert proxy.DERIVED._RUNTIME_ROUTE_POLICY[path]["intent"] == "acme.version"
    assert proxy.DERIVED._AZIZAM_POLICY[path] == ("acme.api", "acme.version")
    assert proxy.DERIVED._ASSET_CONTENT_ROUTE_POLICY_BY_KIND["acm"]["intent"] == "acme.version"
    assert any(entry["canonical"] == path for entry in proxy.DERIVED.CANONICAL_PROTOCOL_ROUTE_ALIASES)
    assert "acme" in proxy.DERIVED._PROTOCOL_ROUTE_REGISTRY["protocol_frontends"]

    _write_frontends(live, _frontend_with_policies(path, "acme.version2"))
    proxy.reload_runtime_config()
    assert proxy.DERIVED._RUNTIME_ROUTE_POLICY[path]["intent"] == "acme.version2"

    _write_frontends(live)
    proxy.reload_runtime_config()
    assert path not in proxy.DERIVED._RUNTIME_ROUTE_POLICY
    assert path not in proxy.DERIVED._AZIZAM_POLICY
    assert "acm" not in proxy.DERIVED._ASSET_CONTENT_ROUTE_POLICY_BY_KIND


def test_a_snapshot_built_without_values_is_derived_where_it_is_built():
    """``snapshot()`` derives a snapshot it is not handed values for, so a reader never meets one without them."""
    config = {"backends": declared({"tts": {"host": "198.51.100.71", "port": 1234}})}
    snapshot = proxy.CONFIG_HOME.snapshot(config)
    assert snapshot.derived["TTS_HOST"] == "http://198.51.100.71:1234"
    assert set(snapshot.derived) >= set(CLASS_A + CLASS_B + ROUTE_POLICIES)


def test_a_candidate_that_cannot_be_derived_leaves_the_old_snapshot_serving(live, monkeypatch):
    before = proxy.CONFIG_HOME.holder.current()
    monkeypatch.setattr(service_registry, "load_service_registry", lambda **_kw: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        proxy.reload_runtime_config()
    assert proxy.CONFIG_HOME.holder.current() is before


def _module_level_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_no_registry_derived_constant_is_a_module_name_any_more():
    """A module name is a copy a reload can leave behind; every one of these is read through ``DERIVED``."""
    assigned = _module_level_names((ROOT / "proxy.py").read_text(encoding="utf-8"))
    assert not assigned & set(CLASS_A + CLASS_B + ROUTE_POLICIES)
