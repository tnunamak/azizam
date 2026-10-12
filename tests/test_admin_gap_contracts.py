"""Proposed admin gap tests. Run only through the hermetic pytest runner."""
import asyncio
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from gateway import admin_runtime, admin_store
from gateway.admin_api import AdminAuthError, AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore, EXTENSION_PACKAGE_INDEX_SETTING_KEY


OWNER = AdminContext(subject="fixture-owner", roles=frozenset({"owner"}))


def empty_registry():
    return {
        "schema_version": 1,
        "services": {},
        "protocol_frontends": {},
        "workflows": {},
        "capabilities": {},
    }


@pytest.fixture
def sqlite_store(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return AdminSQLiteStore(path)


# G1: Intentionally red at the reviewed SHA. This is a proposed atomicity
# acceptance test, not a claim that the current implementation already passes.
@pytest.mark.parametrize("failure_stage", ["index", "audit"])
def test_extension_enable_failure_leaves_no_partial_durable_state(
    sqlite_store, monkeypatch, failure_stage
):
    store = sqlite_store
    review = {
        "ok": True,
        "package": {"id": "fixture-package", "version": "1.0.0"},
        "package_hash": "sha256:" + "a" * 64,
    }
    imported = {
        "services": {}, "protocol_frontends": {},
        "workflows": {}, "capabilities": {},
    }
    ep = admin_runtime.extension_packages
    monkeypatch.setattr(ep, "validate_extension_package_source",
                        lambda *a, **kw: copy.deepcopy(review))
    monkeypatch.setattr(ep, "imported_registry_records",
                        lambda *a, **kw: copy.deepcopy(imported))
    monkeypatch.setattr(ep, "merge_package_index", lambda *a, **kw: {
        "schema_version": 1, "sources": [],
        "packages": {"fixture-package": {"id": "fixture-package"}},
        "updated_at": 1,
    })
    monkeypatch.setattr(store, "write_litellm_config",
                        lambda registry: store.path.parent / "unused.yaml")

    original_put = admin_store.put_operator_setting
    original_audit = admin_store.record_audit
    injected = []

    def put(key, value, **kwargs):
        if failure_stage == "index" and key == EXTENSION_PACKAGE_INDEX_SETTING_KEY:
            injected.append("index")
            raise OSError("injected index failure")
        return original_put(key, value, **kwargs)

    def audit(action, **kwargs):
        if failure_stage == "audit" and action == "operator.extension_package.enable":
            injected.append("audit")
            raise OSError("injected audit failure")
        return original_audit(action, **kwargs)

    monkeypatch.setattr(admin_store, "put_operator_setting", put)
    monkeypatch.setattr(admin_store, "record_audit", audit)

    def snapshot():
        return (
            admin_store.get_service_registry_with_generation(path=store.path),
            admin_store.get_operator_setting(
                EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=store.path
            ),
            admin_store.list_audit(path=store.path),
        )

    before = snapshot()
    with pytest.raises(OSError, match="injected"):
        store.enable_extension_package(
            source={"type": "local_directory", "path": "/not-accessed"},
            actor=OWNER, expected_generation=0,
        )
    assert injected == [failure_stage]
    assert snapshot() == before


# G2: Every registered route must reject before store/provider access.
class ForbiddenDependency:
    def __getattr__(self, name):
        raise AssertionError("denied request accessed dependency: " + name)


@pytest.mark.parametrize("case,status_code,error_code", [
    ("missing", 401, "admin_unauthenticated"),
    ("member", 403, "admin_forbidden"),
    ("invalid_context", 500, "admin_auth_invalid"),
    ("session_missing", 403, "admin_csrf_required"),
    ("session_wrong", 403, "admin_csrf_required"),
    ("legacy_missing", 403, "admin_action_header_required"),
])
def test_all_admin_routes_guard_before_any_dependency(case, status_code, error_code):
    def auth(request):
        if case == "missing":
            raise AdminAuthError()
        if case == "member":
            return AdminContext(subject="member", roles=frozenset())
        if case == "invalid_context":
            return None
        return AdminContext(
            subject="owner", roles=frozenset({"owner"}), auth_method="session",
            csrf_token=None if case == "legacy_missing" else "expected-csrf",
        )

    async def forbidden_async(*args, **kwargs):
        raise AssertionError("denied request called provider")

    def forbidden_sync(*args, **kwargs):
        raise AssertionError("denied request called provider")

    router = create_admin_router(
        ForbiddenDependency(), auth=auth,
        service_probe=forbidden_async, status_provider=forbidden_async,
        litellm_key_client=ForbiddenDependency(),
        known_model_names_getter=forbidden_sync,
        effective_oidc_config_provider=forbidden_sync,
        profile_resolver=forbidden_sync,
    )
    app = FastAPI()
    app.include_router(router)
    checked = []
    with TestClient(app) as client:
        for route in router.routes:
            for method in sorted(route.methods):
                if case.startswith("session_") or case == "legacy_missing":
                    if method in {"GET", "HEAD", "OPTIONS"}:
                        continue
                headers = {}
                if case == "session_wrong":
                    headers = {
                        "x-azizam-csrf": "wrong-csrf",
                        "x-azizam-admin-action": "1",
                    }
                path = re.sub(r"\{[^}]+\}", "fixture-target", route.path)
                response = client.request(method, path, json={}, headers=headers)
                label = (method, path)
                assert response.status_code == status_code, (label, response.text)
                assert response.json()["error"]["code"] == error_code, label
                checked.append(label)
    assert checked


# G3: Real competing registry writers, including effects only for the winner.
def test_registry_race_has_one_commit_one_audit_and_one_reload(sqlite_store, monkeypatch):
    path = sqlite_store.path
    barrier = Barrier(2)
    lock = Lock()
    effects = []

    def note(kind, subject):
        with lock:
            effects.append((kind, subject))

    def writer(subject):
        store = AdminSQLiteStore(path)
        actor = AdminContext(subject=subject, roles=frozenset({"owner"}))
        store.on_service_registry_changed = lambda: note("reload", subject)
        store.write_litellm_config = lambda registry: note("artifact", subject)
        barrier.wait(timeout=5)
        try:
            result = store.update_service_registry(
                data=empty_registry(), actor=actor, expected_generation=0
            )
            return ("saved", subject, result["operator_settings_generation"])
        except admin_store.OperatorSettingsGenerationConflict:
            return ("conflict", subject, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, ["writer-a", "writer-b"]))
    saved = [r for r in results if r[0] == "saved"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(saved) == len(conflicts) == 1
    _, winner, generation = saved[0]
    assert generation == 1
    durable = admin_store.get_service_registry_with_generation(path=path)
    assert durable["operator_settings_generation"] == 1
    assert durable["updated_by"] == winner
    events = admin_store.list_audit(path=path)
    assert len(events) == 1
    assert events[0]["action"] == "operator.service_registry.update"
    assert events[0]["actor_id"] == winner
    assert effects == [("artifact", winner), ("reload", winner)]


# G4: Pin the documented post-commit boundary; no false rollback guarantee.
@pytest.mark.parametrize("failure_stage", ["artifact", "reload"])
def test_registry_post_commit_failure_contract(
    sqlite_store, monkeypatch, capsys, failure_stage
):
    store = sqlite_store
    effects = []

    def artifact(registry):
        effects.append("artifact")
        if failure_stage == "artifact":
            raise OSError("injected artifact failure")
        return store.path.parent / "unused.yaml"

    def reload():
        effects.append("reload")
        raise RuntimeError("injected reload failure")

    monkeypatch.setattr(store, "write_litellm_config", artifact)
    store.on_service_registry_changed = reload
    if failure_stage == "artifact":
        with pytest.raises(OSError, match="injected artifact failure"):
            store.update_service_registry(
                data=empty_registry(), actor=OWNER, expected_generation=0
            )
        assert effects == ["artifact"]
    else:
        result = store.update_service_registry(
            data=empty_registry(), actor=OWNER, expected_generation=0
        )
        assert result["operator_settings_generation"] == 1
        assert effects == ["artifact", "reload"]
        assert "on_service_registry_changed" in capsys.readouterr().out

    durable = admin_store.get_service_registry_with_generation(path=store.path)
    assert durable["operator_settings_generation"] == 1
    assert durable["value"]["services"] == {}
    assert len(admin_store.list_audit(path=store.path)) == 1
    before_effects = list(effects)
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        store.update_service_registry(
            data=empty_registry(), actor=OWNER, expected_generation=0
        )
    assert effects == before_effects
    assert admin_store.get_service_registry_with_generation(path=store.path) == durable
    assert len(admin_store.list_audit(path=store.path)) == 1


# G5: No live provider; synchronize on entry instead of sleeping.
@pytest.mark.parametrize("suffix", ["test", "detect-models"])
def test_probe_route_propagates_cancellation_and_runs_provider_cleanup(suffix):
    async def scenario():
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def probe(host, port, path):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        router = create_admin_router(
            ForbiddenDependency(), auth=lambda request: OWNER, service_probe=probe
        )
        path = "/admin/api/operator/services/" + suffix
        endpoint = next(route.endpoint for route in router.routes if route.path == path)
        raw = json.dumps({"base_url": "http://example.invalid:8000/v1"}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request = Request({
            "type": "http", "method": "POST", "path": path,
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        }, receive=receive)
        task = asyncio.create_task(endpoint(request))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_detect_models_exception_is_structured_and_does_not_persist():
    calls = []

    async def probe(host, port, path):
        calls.append((host, port, path))
        raise TimeoutError("fixture timeout")

    app = FastAPI()
    app.include_router(create_admin_router(
        ForbiddenDependency(), auth=lambda request: OWNER, service_probe=probe
    ))
    with TestClient(app) as client:
        response = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://example.invalid:8000/v1"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "detected": {"models": [], "ok": False, "error": "fixture timeout"}
    }
    assert calls == [("example.invalid", 8000, "/v1/models")]
