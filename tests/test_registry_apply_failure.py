"""A stored registry that cannot be applied never replaces the running config with the bootstrap file's.

The re-review of round 3 found that ``_overlay_service_registry`` caught every error and returned the
bootstrap YAML, so a hot reload swapped in config the stored registry never described and the admin
save answered 200, and boot did the same. Now a failed hot reload keeps the old config and routes and
the admin save says the change is stored but not live (500 ``registry_saved_not_applied``). A failed boot
serves the stored services that do validate, or none, reports it on the Overview, and logs it loudly.
It never serves the YAML backends in place of the registry.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, service_registry
from gateway.admin_api import AdminContext, create_admin_router, derive_admin_attention
from gateway.admin_runtime import AdminAuthenticator
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_frontend_mount_reload import _frontend, _get_status, _publish_registry, live  # noqa: F401 - fixture

OWNER = AdminContext(subject="owner-user", roles=frozenset({"owner"}), actor_kind="user", auth_method="key")

ACME = _frontend("acme", {"methods": ["GET"], "canonical": "/acme/api/version", "intent": "version",
                          "translator": "ollama"})


def _service(service_id: str, **config) -> dict:
    return {"id": service_id, "name": service_id, "kind": "engine", "enabled": True, "capabilities": ["chat"],
            "config": {"host": "10.0.0.5", "port": 8080, **config}}


async def _no_status(_config):
    return {}


async def _no_grid(_config):
    return []


def _owner_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(AdminAuthenticator, "__call__", lambda self, request: OWNER)
    monkeypatch.setattr(proxy.status_endpoint, "get_cached_status", _no_status)
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", _no_grid)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", None, raising=False)
    return TestClient(proxy.app, raise_server_exceptions=False)


def _put_registry(client: TestClient, db) -> object:
    if admin_store.get_service_registry_with_generation(path=db) is None:
        write_service_registry_for_test(service_registry.normalize_service_registry({}), path=db)
    generation = admin_store.get_service_registry_with_generation(path=db)["operator_settings_generation"]
    registry = proxy.ADMIN_STORE.get_service_registry()
    return client.put("/admin/api/operator/service-registry", json=registry, headers={"If-Match": f'"os-{generation}"'})


# --- a hot reload -------------------------------------------------------------------------------

def test_a_hot_reload_whose_overlay_fails_keeps_the_old_config_and_routes(live, monkeypatch):
    _publish_registry(live, ACME)
    proxy.reload_runtime_config()
    config_before, routes_before = proxy.config, list(proxy.app.router.routes)
    assert _get_status("/acme/api/version") == 200

    def broken(*args, **kwargs):
        raise RuntimeError("registry store unreadable")

    monkeypatch.setattr(service_registry, "ensure_service_registry", broken)
    with pytest.raises(RuntimeError, match="registry store unreadable"):
        proxy.reload_runtime_config()

    assert proxy.config is config_before
    assert proxy.app.router.routes == routes_before
    assert _get_status("/acme/api/version") == 200


def test_a_hot_reload_that_fails_does_not_swap_in_the_bootstrap_backends(live, monkeypatch):
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"] = {"only-stored": service_registry.normalize_service_record("only-stored", _service("only-stored"))}
    write_service_registry_for_test(registry, path=live)
    proxy.reload_runtime_config()
    backends_before = set(proxy.config["backends"])
    assert "only-stored" in backends_before

    monkeypatch.setattr(service_registry, "ensure_service_registry", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        proxy.reload_runtime_config()

    assert set(proxy.config["backends"]) == backends_before


def test_an_admin_save_whose_overlay_fails_answers_500_registry_saved_not_applied_through_the_app(live, monkeypatch):
    client = _owner_client(monkeypatch)
    config_before = proxy.config

    def broken(*args, **kwargs):
        raise RuntimeError("registry store unreadable")

    monkeypatch.setattr(service_registry, "ensure_service_registry", broken)
    response = _put_registry(client, live)

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "registry_saved_not_applied"
    assert "registry store unreadable" in response.json()["error"]["message"]
    assert proxy.config is config_before


def test_an_admin_save_whose_route_mount_fails_answers_500_registry_saved_not_applied_through_the_app(live, monkeypatch):
    client = _owner_client(monkeypatch)

    def failing(*args, **kwargs):
        raise RuntimeError("mount failed")

    with monkeypatch.context() as patched:  # undone before the fixture remounts the table
        patched.setattr(proxy.FRONTEND_MOUNTS, "prepare", failing)
        response = _put_registry(client, live)

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "registry_saved_not_applied"


def test_an_admin_save_that_applies_answers_200(live, monkeypatch):
    client = _owner_client(monkeypatch)
    assert _put_registry(client, live).status_code == 200


def test_a_failed_reload_is_reported_on_the_overview_until_a_reload_succeeds(live, monkeypatch):
    client = _owner_client(monkeypatch)

    def attention_kinds() -> list[str]:
        response = client.get("/admin/api/overview")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        return [item["kind"] for item in response.json()["attention"]]

    with monkeypatch.context() as broken:
        broken.setattr(service_registry, "ensure_service_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        with pytest.raises(RuntimeError):
            proxy.reload_runtime_config()
        assert "registry" in attention_kinds()

    proxy.reload_runtime_config()
    assert proxy.REGISTRY_APPLY_PROBLEM is None  # what the Overview lists, as the line above shows


# --- boot ---------------------------------------------------------------------------------------

def _store_raw(db, services: dict) -> None:
    """Write a registry document the way an older version or a hand edit could have left it: unvalidated."""
    write_service_registry_for_test(
        {"schema_version": 1, "services": services, "protocol_frontends": {}, "workflows": {}, "capabilities": {}},
        path=db,
    )


GOOD = _service("good-engine")
BAD = _service("bad-engine", models=["this is not a model record"])


def _boot(live):
    return proxy._boot_effective_runtime_config(proxy._bootstrap_config)


def test_a_healthy_boot_reports_no_problem_and_serves_the_stored_registry(live):
    config, problem = _boot(live)
    assert problem is None
    assert set(config["backends"]) == set(proxy.config["backends"])


def test_a_boot_with_one_bad_service_serves_the_others_and_names_the_one_it_left_out(live, capsys):
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    config, problem = _boot(live)

    assert set(config["backends"]) == {"good-engine"}
    assert problem["mode"] == "partial"
    assert set(problem["left_out"]) == {"bad-engine"}
    assert problem["left_out"]["bad-engine"]
    assert "bad-engine" in capsys.readouterr().out  # logged loudly, not swallowed


def test_a_boot_never_serves_the_bootstrap_yaml_backends_in_place_of_the_registry(live):
    yaml_backends = set(proxy._bootstrap_config.get("backends") or {})
    assert yaml_backends, "the fixture needs bootstrap backends for this to prove anything"
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    config, _problem = _boot(live)

    assert not yaml_backends & set(config["backends"])


def test_a_boot_that_cannot_read_the_stored_registry_serves_no_registry_backends(live, monkeypatch, capsys):
    def unreadable(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "ensure_service_registry", unreadable)
    monkeypatch.setattr(admin_store, "get_service_registry_with_generation", unreadable)

    config, problem = _boot(live)

    assert config["backends"] == {}
    assert problem["mode"] == "empty"
    assert "database is locked" in problem["error"]
    assert "database is locked" in capsys.readouterr().out


def test_a_boot_with_a_bad_service_leaves_the_litellm_artifact_alone(live, monkeypatch):
    written: list[object] = []
    monkeypatch.setattr(proxy, "_write_litellm_config_artifact", lambda registry: written.append(registry))
    _store_raw(live, {"good-engine": GOOD, "bad-engine": BAD})

    _boot(live)

    assert written == []  # a partial registry must not overwrite the file the running LiteLLM reloads


def test_the_overview_item_says_what_is_served_and_what_is_not():
    partial = {"mode": "partial", "error": "bad", "left_out": {"bad-engine": "models[0] must be an object"}, "served": 3}
    empty = {"mode": "empty", "error": "database is locked", "left_out": {}, "served": 0}
    [item] = [i for i in derive_admin_attention({"registry_problem": partial}) if i["kind"] == "registry"]
    assert item["severity"] == "problem" and "bad-engine" in item["detail"]
    [item] = [i for i in derive_admin_attention({"registry_problem": empty}) if i["kind"] == "registry"]
    assert item["severity"] == "problem" and "no stored services" in item["detail"].lower()
    assert [i for i in derive_admin_attention({"registry_problem": None}) if i["kind"] == "registry"] == []


def test_the_overview_route_lists_the_registry_problem_it_is_given():
    from tests.test_admin_api import FakeAdminStore, _admin_auth, _auth

    problem = {"mode": "empty", "error": "database is locked", "left_out": {}, "served": 0}
    app = FastAPI()
    app.include_router(create_admin_router(FakeAdminStore(), auth=_admin_auth, registry_problem=lambda: problem))

    body = TestClient(app).get("/admin/api/overview", headers=_auth()).json()

    assert [i for i in body["attention"] if i["kind"] == "registry"]


# --- overlapping reloads ------------------------------------------------------------------------

def test_overlapping_reloads_apply_in_order_and_the_last_applied_config_is_the_stored_one(live, monkeypatch):
    stored = {"marker": "v1"}
    applied: list[str] = []
    first_has_read, release_first = threading.Event(), threading.Event()
    original = proxy._derive_backend_constants

    def loader(raw):
        snapshot = stored["marker"]  # what the database says when this reload reads it
        if snapshot == "v1":
            first_has_read.set()
            assert release_first.wait(10)
        return {**copy.deepcopy(dict(proxy.config)), "marker": snapshot}

    def derive(cfg):
        applied.append(cfg["marker"])
        return original(cfg)

    monkeypatch.setattr(proxy, "_load_effective_runtime_config", loader)
    monkeypatch.setattr(proxy, "_derive_backend_constants", derive)
    monkeypatch.setattr(proxy, "_prepare_frontend_mounts", lambda cfg: None)
    errors: list[BaseException] = []

    def reload() -> None:
        try:
            proxy.reload_runtime_config()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    first = threading.Thread(target=reload)
    first.start()
    assert first_has_read.wait(10)
    stored["marker"] = "v2"  # a second save lands while the first reload is still working
    second = threading.Thread(target=reload)
    second.start()
    time.sleep(0.3)  # without a lock the second reload finishes now, and the first then overwrites it
    release_first.set()
    first.join(10)
    second.join(10)

    assert errors == []
    assert applied == ["v1", "v2"]
    assert proxy.config["marker"] == "v2"


# --- the frontend table and packages ------------------------------------------------------------

def test_a_stored_shadow_of_a_builtin_with_its_own_path_mounts_nothing():
    from gateway import frontend_mount, route_registry

    seed = service_registry.load_seed_catalogs({}).get("protocol_frontends", {})
    builtin_id = next(fid for fid, rec in seed.items() if rec.get("source") == "builtin")
    shadow = _frontend(builtin_id, {"methods": ["GET"], "canonical": "/unique-shadow/path", "intent": "models.list",
                                    "translator": "identity"}, source="ui")
    records = route_registry.mount_frontend_records(registry={"protocol_frontends": {builtin_id: shadow}}, proxy_config={})
    mounts = frontend_mount.FrontendMounts(FastAPI(), proxy.FRONTEND_TRANSLATORS,
                                           authenticate=proxy._authenticate_mounted_route)

    prepared = mounts.prepare(records)
    prepared.commit()

    assert all(getattr(route, "path", "") != "/unique-shadow/path" for route in mounts.app.routes)
    assert TestClient(mounts.app).get("/unique-shadow/path").status_code == 404
    [ignored] = [item for item in mounts.status() if item["status"] == "broken" and builtin_id in item["id"]]
    assert "builtin" in ignored["errors"][0]["message"]


# --- what a boot can still read ------------------------------------------------------------------

def _model(model_id: str, base: str | None = None) -> dict:
    return {"id": model_id, **({"base_model_id": base} if base else {})}


def test_the_servable_part_keeps_every_record_that_validates_and_names_each_one_it_drops():
    stored = {
        "services": {"good-engine": GOOD, "bad-engine": BAD},
        "protocol_frontends": {"fine": _frontend("fine"), "broken": "not a mapping", "odd": {"id": "odd", "routes": 5}},
        "workflows": {}, "capabilities": {},
    }

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"good-engine"}
    assert "fine" in part["protocol_frontends"]
    assert set(left_out) >= {"bad-engine"}


def test_two_services_that_claim_one_public_alias_are_both_left_out():
    def claiming(service_id):
        return _service(service_id, models=[_model("base"), _model("shared-alias", base="base")])

    stored = {"services": {"one": claiming("one"), "two": claiming("two"), "good-engine": GOOD}}

    part, left_out = service_registry.servable_part_of_registry(stored)

    assert set(part["services"]) == {"good-engine"}
    assert set(left_out) == {"one", "two"}


def test_a_gateway_started_on_a_registry_with_one_bad_service_serves_the_rest(tmp_path):
    """The whole boot, in a fresh process: the import that used to fail, or serve the YAML backends."""
    import json
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    repo = Path(proxy.__file__).resolve().parent
    script = textwrap.dedent(f"""
        import json, os, sys
        from pathlib import Path
        from gateway import admin_store
        db = Path(os.environ["AZIZAM_ADMIN_DB"])
        admin_store.init_db(db)
        good = {GOOD!r}
        bad = {BAD!r}
        admin_store.write_service_registry({{"schema_version": 1, "services": {{"good-engine": good, "bad-engine": bad}}}},
                                           updated_by="test", expected_generation=0, path=db)
        import proxy
        print("RESULT" + json.dumps({{"problem": proxy.REGISTRY_APPLY_PROBLEM, "backends": sorted(proxy.config["backends"])}}))
    """)
    env = dict(os.environ)
    for name, filename in [("AZIZAM_ADMIN_DB", "admin.db"), ("AZIZAM_METRICS_DB", "metrics.db"),
                           ("AZIZAM_RECEIPTS_DB", "receipts.db"), ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db"),
                           ("AZIZAM_RESPONSES_DB", "responses.db")]:
        env[name] = str(tmp_path / filename)
    env.update(PYTHONPATH=str(repo), HOME=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)

    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(next(line for line in done.stdout.splitlines() if line.startswith("RESULT"))[len("RESULT"):])
    assert result["backends"] == ["good-engine"]
    assert result["problem"]["mode"] == "partial" and set(result["problem"]["left_out"]) == {"bad-engine"}
    assert "bad-engine" in done.stdout  # the loud log line


def test_a_failed_reload_after_a_partial_boot_keeps_the_partial_report(live, monkeypatch):
    partial = {"mode": "partial", "error": "bad", "left_out": {"bad-engine": "x"}, "served": 1}
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", partial, raising=False)
    with monkeypatch.context() as broken:
        broken.setattr(service_registry, "ensure_service_registry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        with pytest.raises(RuntimeError):
            proxy.reload_runtime_config()
    assert proxy.REGISTRY_APPLY_PROBLEM == partial
    proxy.reload_runtime_config()
    assert proxy.REGISTRY_APPLY_PROBLEM is None
