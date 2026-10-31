"""A registry seeded before the mesh release serves the mesh route after one upgrade, and an operator's removal stays removed.

The mesh release added the ``mesh`` protocol frontend and the ``trellis`` and ``vram-observer`` backends. A registry
seeded before it has neither. The registry keeps no tombstones, so the upgrade adds only the ids the release named,
once (``service_registry.MESH_UPGRADE_STEP``); a fresh seed records that step too.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from gateway import admin_store, exclusive_pool, mesh_api, mesh_manifest, route_registry, service_registry
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401  (gateway is a fixture)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = mesh_manifest.load_mesh_manifest(ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml")
PROXY_CONFIG = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
MESH_FRONTENDS = ("mesh", "native", "typesafe")
MESH_BACKENDS = ("trellis", "vram-observer")
REGISTRY_PATH = "/admin/api/operator/service-registry"


def _without_the_mesh_release(config: dict) -> dict:
    """``config`` as the release before the mesh one saw it: no trellis and no vram-observer backend."""
    backends = {key: value for key, value in config["backends"].items() if key not in MESH_BACKENDS}
    return {**config, "backends": backends}


def _seed_before_the_mesh_release(path: Path) -> None:
    """Write the registry an install seeded before the mesh release holds, with no upgrade step recorded."""
    seed = service_registry.build_service_registry_seed(_without_the_mesh_release(PROXY_CONFIG))
    for frontend_id in MESH_FRONTENDS:
        seed["protocol_frontends"].pop(frontend_id, None)
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        seed,
        expected_generation=0,
        updated_by="previous-release",
        audit_action="operator.service_registry.seed",
        audit_actor_kind="admin",
        audit_actor_id="previous-release",
        audit_actor_label="System",
        audit_target_kind="operator_setting",
        audit_target_id=service_registry.SERVICE_REGISTRY_SETTING_KEY,
        path=path,
    )


def _remove_service(path: Path, service_id: str) -> None:
    current = admin_store.get_service_registry_with_generation(path=path)
    value = service_registry.remove_service_from_registry(current["value"], service_id)
    admin_store.write_service_registry_with_audit(
        value,
        expected_generation=current["operator_settings_generation"],
        updated_by="operator",
        audit_action="operator.service_registry.remove",
        audit_actor_kind="admin",
        audit_actor_id="operator",
        audit_actor_label="Operator",
        audit_target_kind="service",
        audit_target_id=service_id,
        path=path,
    )


def test_the_upgrade_lets_a_pre_mesh_registry_serve_the_mesh_route(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    _seed_before_the_mesh_release(path)

    service_registry.ensure_service_registry(PROXY_CONFIG, path=path, actor="boot")

    registry = service_registry.load_service_registry(path=path)
    records = route_registry.protocol_frontend_records(registry=registry, proxy_config=PROXY_CONFIG)
    _row, route_problem = mesh_api.route_row(records, MANIFEST)
    assert route_problem is None
    problem, spec, _service = mesh_api.service_problem(registry["services"], MANIFEST)
    assert problem is None
    assert spec is not None


def test_the_upgrade_adds_only_the_named_backends_and_runs_once(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    _seed_before_the_mesh_release(path)
    # A backend ``proxy_config.yml`` names but the release did not add: the upgrade must not bring it back.
    config = copy.deepcopy(PROXY_CONFIG)
    config["backends"]["retired-by-operator"] = {"name": "Retired", "host": "127.0.0.1", "port": 9, "capabilities": []}

    service_registry.ensure_service_registry(config, path=path, actor="boot")
    services = service_registry.load_service_registry(path=path)["services"]
    assert "retired-by-operator" not in services
    assert all(key in services for key in MESH_BACKENDS)
    # The first boot after any seed writes the document once more in normalized form (its ``lifecycle`` is added on
    # read). After that pass a boot writes nothing, so the upgrade is not re-run and does not churn the revision.
    service_registry.ensure_service_registry(config, path=path, actor="boot")
    generation = admin_store.get_service_registry_with_generation(path=path)["operator_settings_generation"]
    service_registry.ensure_service_registry(config, path=path, actor="boot")
    assert admin_store.get_service_registry_with_generation(path=path)["operator_settings_generation"] == generation


def test_an_operator_removal_after_the_upgrade_stays_removed(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    _seed_before_the_mesh_release(path)
    service_registry.ensure_service_registry(PROXY_CONFIG, path=path, actor="boot")
    _remove_service(path, "vram-observer")

    service_registry.ensure_service_registry(PROXY_CONFIG, path=path, actor="boot")

    assert "vram-observer" not in service_registry.load_service_registry(path=path)["services"]


def test_a_fresh_seed_records_the_step_so_a_removal_stays_removed(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    service_registry.ensure_service_registry(PROXY_CONFIG, path=path, actor="boot")
    _remove_service(path, "trellis")

    service_registry.ensure_service_registry(PROXY_CONFIG, path=path, actor="boot")

    assert "trellis" not in service_registry.load_service_registry(path=path)["services"]
    assert admin_store.migration_step_recorded(service_registry.MESH_UPGRADE_STEP, path=path)


def test_a_registry_put_keeps_the_pool_capacity_exactly(gateway) -> None:
    owner, _, _, _ = owner_and_admin(gateway)
    seed = service_registry.build_service_registry_seed(
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
