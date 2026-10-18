"""One bad row, or one stored record that claims a builtin's id, never takes the frontend table down.

Review of the mount found three ways a stored or imported record could still stop the whole table:
a path Starlette rejects aborted the mount (and boot); a record with a builtin's id replaced the
builtin; and a failed hot reload was logged while the admin save answered success.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, frontend_mount, route_registry, service_registry
from gateway.admin_api import AdminContext, derive_admin_attention
from gateway.admin_runtime import AdminSQLiteStore, RegistryNotApplied


async def _served(request: Request) -> dict:
    return {"served": request.url.path}


def _translators() -> frontend_mount.Translators:
    return frontend_mount.Translators({"identity": {
        "models.list": frontend_mount.Operation(_served, frozenset({"GET"}), self_authenticates=True),
    }})


def _frontend(frontend_id: str, path: str, *, source: str = "imported", enabled: bool = True) -> dict:
    return {"id": frontend_id, "name": frontend_id, "enabled": enabled, "source": source,
            "registered_routes": [{"methods": ["GET"], "canonical": path, "translator": "identity",
                                   "intent": "models.list"}]}


def _mounts() -> frontend_mount.FrontendMounts:
    return frontend_mount.FrontendMounts(FastAPI(), _translators(), authenticate=None)


def _by_id(mounts: frontend_mount.FrontendMounts) -> dict[str, dict]:
    return {item["id"]: item for item in mounts.status()}


# --- a row Starlette rejects ------------------------------------------------------------------

@pytest.mark.parametrize("bad_path", ["/x/{p:nosuch}", "/x/{p:int}/{p:int}"])
def test_a_path_starlette_rejects_is_skipped_and_the_good_row_beside_it_mounts(bad_path):
    mounts = _mounts()

    mounts.apply([_frontend("bad", bad_path), _frontend("good", "/y/items")])

    status = _by_id(mounts)
    assert status["bad"]["status"] == "broken" and status["bad"]["errors"]
    assert status["bad"]["errors"][0]["route"] == bad_path
    assert status["good"]["status"] == "mounted"
    assert TestClient(mounts.app).get("/y/items").json() == {"served": "/y/items"}


def test_a_table_of_one_bad_path_mounts_nothing_and_does_not_raise():
    mounts = _mounts()
    mounts.apply([_frontend("bad", "/x/{p:nosuch}")])
    assert _by_id(mounts)["bad"]["status"] == "broken"


def test_a_reload_that_meets_a_bad_path_keeps_the_rows_that_were_mounted_and_adds_the_good_ones():
    mounts = _mounts()
    mounts.apply([_frontend("a", "/x/a")])

    mounts.apply([_frontend("a", "/x/a"), _frontend("bad", "/x/{p:nosuch}"), _frontend("c", "/x/c")])

    client = TestClient(mounts.app)
    assert client.get("/x/a").status_code == 200 and client.get("/x/c").status_code == 200
    assert _by_id(mounts)["bad"]["status"] == "broken"


def test_the_overview_lists_the_frontend_with_the_bad_path_as_a_problem():
    mounts = _mounts()
    mounts.apply([_frontend("bad", "/x/{p:nosuch}")])
    [item] = [i for i in derive_admin_attention({"frontends": mounts.status()}) if i.get("kind") == "frontend"]
    assert item["severity"] == "problem"


# --- a stored record with a builtin's id --------------------------------------------------------

def _builtin_id() -> str:
    seed = service_registry.load_seed_catalogs({}).get("protocol_frontends", {})
    return next(fid for fid, rec in seed.items() if rec.get("source") == "builtin"
                and any(isinstance(r, dict) and r.get("translator") for r in rec.get("registered_routes") or []))


def _shadow(frontend_id: str) -> dict:
    return {"id": frontend_id, "name": "mine", "source": "ui", "enabled": True, "registered_routes": []}


def _seed_rows(frontend_id: str) -> int:
    seed = service_registry.load_seed_catalogs({}).get("protocol_frontends", {})
    return len(seed[frontend_id]["registered_routes"])


def test_a_stored_record_with_a_builtins_id_does_not_replace_the_builtin_at_mount():
    frontend_id = _builtin_id()
    registry = {"protocol_frontends": {frontend_id: _shadow(frontend_id)}}

    records = route_registry.mount_frontend_records(registry=registry, proxy_config={})

    mounting = [r for r in records if r["id"] == frontend_id and not r.get("mount_problem")]
    assert [r["source"] for r in mounting] == ["builtin"]
    assert len(mounting[0]["registered_routes"]) == _seed_rows(frontend_id)
    ignored = [r for r in records if r.get("mount_problem")]
    assert [r["id"] for r in ignored] == [frontend_id]


def test_the_ignored_record_is_reported_as_broken_and_the_builtin_stays_mounted():
    frontend_id = _builtin_id()
    registry = {"protocol_frontends": {frontend_id: _shadow(frontend_id)}}
    mounts = frontend_mount.FrontendMounts(FastAPI(), _translators(), authenticate=None)

    mounts.prepare(route_registry.mount_frontend_records(registry=registry, proxy_config={}))
    status = mounts.prepare(route_registry.mount_frontend_records(registry=registry, proxy_config={})).status

    by_source = {item["source"]: item for item in status if item["id"] == frontend_id}
    assert by_source["ui"]["status"] == "broken"
    assert "builtin" in by_source["ui"]["errors"][0]["message"]
    assert by_source["builtin"]["status"] == "mounted"
    assert [i for i in derive_admin_attention({"frontends": status}) if i.get("kind") == "frontend"]


def test_the_route_projections_read_the_builtin_not_the_stored_record_with_its_id():
    frontend_id = _builtin_id()
    registry = {"protocol_frontends": {frontend_id: _shadow(frontend_id)}}
    projected = [r for r in route_registry.protocol_frontend_records(registry=registry, proxy_config={})
                 if r["id"] == frontend_id]
    assert [r["source"] for r in projected] == ["builtin"]
    assert len(projected[0]["registered_routes"]) == _seed_rows(frontend_id)


def _store(tmp_path) -> AdminSQLiteStore:
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({}), expected_generation=0, updated_by="seed", audit_action="seed",
        audit_actor_kind="user", audit_actor_id="seed", audit_actor_label="seed",
        audit_target_kind="operator_setting", audit_target_id="service_registry", path=path,
    )
    return AdminSQLiteStore(path)


ADMIN = AdminContext(subject="admin-user", roles=frozenset({"admin"}), actor_kind="user")
OWNER = AdminContext(subject="owner-user", roles=frozenset({"owner"}), actor_kind="user")


@pytest.mark.parametrize("actor", [ADMIN, OWNER])
def test_the_admin_write_refuses_a_frontend_record_with_a_builtins_id(tmp_path, actor):
    store = _store(tmp_path)
    frontend_id = _builtin_id()
    registry = store.get_service_registry()
    registry["protocol_frontends"] = {frontend_id: _shadow(frontend_id)}

    with pytest.raises(ValueError, match="builtin"):
        store.update_service_registry(data=registry, actor=actor, expected_generation=_generation(store))


def test_the_admin_write_refuses_a_frontend_row_with_a_path_the_router_rejects(tmp_path):
    store = _store(tmp_path)
    registry = store.get_service_registry()
    registry["protocol_frontends"] = {"acme": _frontend("acme", "/x/{p:nosuch}", source="ui")}

    with pytest.raises(ValueError, match="nosuch"):
        store.update_service_registry(data=registry, actor=OWNER, expected_generation=_generation(store))


def test_the_admin_write_still_keeps_a_stored_copy_of_a_builtin_that_says_builtin(tmp_path):
    store = _store(tmp_path)
    frontend_id = _builtin_id()
    seed = service_registry.load_seed_catalogs({})["protocol_frontends"][frontend_id]
    registry = store.get_service_registry()
    registry["protocol_frontends"] = {frontend_id: {**seed, "name": "Renamed"}}

    store.update_service_registry(data=registry, actor=OWNER, expected_generation=_generation(store))

    assert store.get_service_registry()["protocol_frontends"][frontend_id]["name"] == "Renamed"


def test_a_package_import_refuses_a_frontend_with_a_builtins_id():
    frontend_id = _builtin_id()
    with pytest.raises(service_registry.ServiceRegistryConflict, match="builtin"):
        service_registry.merge_imported_records(
            service_registry.normalize_service_registry({}),
            protocol_frontends={frontend_id: _shadow(frontend_id)},
        )


def _generation(store: AdminSQLiteStore) -> int:
    return admin_store.get_service_registry_with_generation(path=store.path)["operator_settings_generation"]


# --- the admin save reports a reload that failed ------------------------------------------------

def test_an_admin_save_whose_reload_fails_says_so_instead_of_answering_success(tmp_path):
    store = _store(tmp_path)

    def failing_reload():
        raise RuntimeError("route table could not be built")

    store.on_service_registry_changed = failing_reload
    registry = store.get_service_registry()

    with pytest.raises(RegistryNotApplied, match="route table could not be built") as raised:
        store.update_service_registry(data=registry, actor=OWNER, expected_generation=_generation(store))

    assert raised.value.record["value"]["services"] is not None  # the registry is saved; the running gateway is not updated


def test_the_admin_api_answers_a_failed_reload_with_a_clear_error():
    from fastapi.responses import JSONResponse
    from gateway.admin_api import registry_not_applied_response

    response = registry_not_applied_response(RegistryNotApplied("boom", record={"value": {}}))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    assert b"registry_saved_not_applied" in response.body and b"boom" in response.body
