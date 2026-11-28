"""The mesh pool capacity block survives a registry write and a read-then-write echo exactly.

The one-time mesh upgrade of an old registry (docs/design/CONFIG_HOMES.md 8.5) is gone: the bundled release layer
carries the ``mesh`` frontend, and an engine such as ``trellis`` is an owner record the owner adds.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from gateway import exclusive_pool, service_registry
from tests.registry_test_helpers import seed_registry_document
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401  (gateway is a fixture)

ROOT = Path(__file__).resolve().parent.parent
PROXY_CONFIG = yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8"))
MESH_BACKENDS = ("trellis", "vram-observer")
REGISTRY_PATH = "/admin/api/operator/service-registry"


def test_a_registry_put_keeps_the_pool_capacity_exactly(gateway) -> None:
    owner, _, _, _ = owner_and_admin(gateway)
    seed = seed_registry_document(
        {"backends": {key: PROXY_CONFIG["backends"][key] for key in MESH_BACKENDS}})
    sent = copy.deepcopy(seed["services"]["trellis"]["config"]["resource_capacity"])

    def etag() -> str:
        return owner.get(REGISTRY_PATH).headers.get("etag", '"os-0"')

    put = owner.put(REGISTRY_PATH, headers={**csrf_for(owner), "If-Match": etag()}, json=json.loads(json.dumps(seed)))
    assert put.status_code == 200, put.text
    stored = service_registry.load_service_registry(path=gateway.path)["services"]["trellis"]["config"]
    assert stored["resource_capacity"] == sent

    # The page writes back what it read; a read shows the block, and the write that follows keeps it exactly.
    read = owner.get(REGISTRY_PATH).json()["service_registry"]
    assert read["services"]["trellis"]["config"]["resource_capacity"] == sent
    echo = owner.put(REGISTRY_PATH, headers={**csrf_for(owner), "If-Match": etag()}, json=read)
    assert echo.status_code == 200, echo.text
    stored = service_registry.load_service_registry(path=gateway.path)["services"]["trellis"]["config"]
    assert stored["resource_capacity"] == sent


def test_an_absent_capacity_block_is_not_enabled_rather_than_malformed() -> None:
    assert exclusive_pool.parse_resource_capacity("trellis", None) is None


def _pool(devices=None):
    from tests.support.mesh_harness import GOOD_CAPACITY

    block = dict(GOOD_CAPACITY)
    if devices is not None:
        block["devices"] = devices
    return exclusive_pool.parse_resource_capacity("trellis", block)


def _speech(devices):
    return {"enabled": True, "config": {"resources": {"gpu": {"devices": devices}},
                                        "residency": {"unload": {"op": "unload_all", "verify": "none"}}}}


@pytest.mark.parametrize(("outcome", "answer"), [("freed", True), ("busy", "busy"), ("failed", False)])
def test_a_pool_without_devices_releases_only_the_graph_engine(monkeypatch, outcome, answer) -> None:
    """The pool lease's release hook is one generic call. A pool that declares no devices asks the graph engine
    and nothing else, as before; a busy engine answers the pool's ENGINE_BUSY, so the refusal keeps its wait."""
    import asyncio

    import proxy
    from gateway.exclusive_pool import ENGINE_BUSY

    seen: list[str] = []

    async def release(client, capability):
        seen.append(capability)
        return outcome

    async def unload(service_id):
        raise AssertionError("a pool without devices asks no declared engine")

    monkeypatch.setattr(proxy, "_release_graph_models", release)
    monkeypatch.setattr(proxy, "_unload_declared_engine", unload)
    monkeypatch.setattr(proxy.MESH_DEPS, "services", lambda: {"speech-a": _speech([0])})
    assert proxy.MULTIPART_HOST.release_idle is proxy._release_idle_engines
    assert answer != "busy" or answer == ENGINE_BUSY
    assert asyncio.run(proxy._release_idle_engines(_pool())) == answer
    assert seen == [proxy._GRAPH_CAPABILITY]


def _hook_rig(monkeypatch, *, graph="failed", busy=(), failing=()):
    import proxy

    unloaded: list[str] = []

    async def release(client, capability):
        return graph

    async def unload(service_id):
        if service_id in failing:
            raise RuntimeError("engine unreachable")
        unloaded.append(service_id)
        return True

    monkeypatch.setattr(proxy, "_release_graph_models", release)
    monkeypatch.setattr(proxy, "_unload_declared_engine", unload)
    monkeypatch.setattr(proxy, "_begin_engine_unload", lambda service_id: service_id not in busy)
    monkeypatch.setattr(proxy.MESH_DEPS, "services", lambda: {
        "speech-a": _speech([0]), "speech-b": _speech([0]), "other-card": _speech([1])})
    return unloaded


def test_a_pool_with_devices_also_frees_the_idle_declared_engines_on_those_devices(monkeypatch) -> None:
    import asyncio

    import proxy

    unloaded = _hook_rig(monkeypatch)
    assert asyncio.run(proxy._release_idle_engines(_pool([0]))) is True
    assert sorted(unloaded) == ["speech-a", "speech-b"]  # not the engine on card 1


def test_a_serving_engine_is_not_unloaded_and_the_pool_hears_busy(monkeypatch) -> None:
    import asyncio

    import proxy
    from gateway.exclusive_pool import ENGINE_BUSY

    unloaded = _hook_rig(monkeypatch, busy={"speech-a", "speech-b"})
    assert asyncio.run(proxy._release_idle_engines(_pool([0]))) == ENGINE_BUSY
    assert unloaded == []


def test_one_engine_failing_does_not_stop_the_others(monkeypatch) -> None:
    import asyncio

    import proxy

    unloaded = _hook_rig(monkeypatch, failing={"speech-a"})
    assert asyncio.run(proxy._release_idle_engines(_pool([0]))) is True
    assert unloaded == ["speech-b"]


def test_the_graph_engine_busy_and_a_speech_engine_freed_counts_as_freed(monkeypatch) -> None:
    import asyncio

    import proxy

    unloaded = _hook_rig(monkeypatch, graph="busy")
    assert asyncio.run(proxy._release_idle_engines(_pool([0]))) is True
    assert sorted(unloaded) == ["speech-a", "speech-b"]
