"""The mesh pool capacity block survives a registry write and a read-then-write echo exactly.

The one-time mesh upgrade of an old registry (docs/design/CONFIG_HOMES.md 8.5) is gone: the bundled release layer
carries the ``mesh`` frontend, and an engine such as ``trellis`` is an owner record the owner adds.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

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
