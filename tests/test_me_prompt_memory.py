from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, prompt_memory, settings_api
from gateway.admin_oidc import AdminSession
from gateway.me_prompt_memory import create_me_prompt_memory_router


def _session(user_id: str):
    def inner(_request: Request) -> AdminSession:
        return AdminSession(
            subject=user_id,
            email=f"{user_id}@example.test",
            role="none",
            issued_at=time.time(),
            expires_at=time.time() + 3600,
        )

    return inner


def _validator(request: Request, _session: AdminSession):
    if request.headers.get("x-azizam-user-action") != "1":
        return JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    return None


def _client(
    admin_db: Path,
    prompt_db: Path,
    *,
    user_id: str = "usr_alice",
    household: dict | None = None,
    on_person_prompt_purge=None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_me_prompt_memory_router(
            session_provider=_session(user_id),
            mutation_validator=_validator,
            household_provider=lambda: household or {},
            admin_db_path=admin_db,
            prompt_memory_path=prompt_db,
            on_person_prompt_purge=on_person_prompt_purge,
        )
    )
    return TestClient(app)


def test_member_prompt_memory_defaults_and_put_are_admin_store_backed(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", path=admin_db)
    client = _client(admin_db, prompt_db, user_id=user["id"], household={"retention_cap": "1_year"})

    response = client.get("/me/api/prompt-memory")
    assert response.status_code == 200
    assert response.json()["prompt_memory"] == {
        "remember": True,
        "retention": "never",
        "effective_remember": True,
        "effective_retention": "1_year",
        "household": {"default_remember": True, "never_keep": False, "retention_cap": "1_year"},
    }

    assert client.put(
        "/me/api/prompt-memory",
        json={"remember": False, "retention": "3_months"},
    ).status_code == 403
    updated = client.put(
        "/me/api/prompt-memory",
        json={"remember": False, "retention": "3_months"},
        headers={"x-azizam-user-action": "1"},
    )
    assert updated.status_code == 200
    assert admin_store.get_prompt_memory_preference(user["id"], path=admin_db)["remember"] is False
    assert updated.json()["prompt_memory"]["effective_remember"] is False


def test_member_prompt_memory_honors_household_kill_switch_and_delete_all(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", path=admin_db)
    prompt_memory.put("img_one", user["id"], "sentinel text", kept_until=None, path=prompt_db)
    client = _client(admin_db, prompt_db, user_id=user["id"], household={"never_keep": True})

    response = client.get("/me/api/prompt-memory")
    assert response.json()["prompt_memory"]["effective_remember"] is False
    assert client.delete("/me/api/prompt-memory/prompts").status_code == 403

    deleted = client.delete(
        "/me/api/prompt-memory/prompts",
        headers={"x-azizam-user-action": "1"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_prompts": 1}
    assert prompt_memory.get("img_one", user["id"], path=prompt_db) is None


def test_new_member_inherits_owner_default_off(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    admin_store.put_operator_setting(
        "prompt_memory", {"default_remember": False, "never_keep": False, "retention_cap": "never"},
        path=admin_db,
    )
    user = admin_store.upsert_oidc_user("issuer", "new", email="new@example.test", path=admin_db)
    client = _client(admin_db, tmp_path / "prompt.db", user_id=user["id"], household={"default_remember": False})
    settings = client.get("/me/api/prompt-memory").json()["prompt_memory"]
    assert settings["remember"] is False
    assert settings["retention"] == "never"


def test_member_prompt_memory_rejects_bad_settings(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", path=admin_db)
    client = _client(admin_db, prompt_db, user_id=user["id"])

    response = client.put(
        "/me/api/prompt-memory",
        json={"remember": "yes", "retention": "soon"},
        headers={"x-azizam-user-action": "1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_prompt_memory_settings"
    saved = admin_store.get_prompt_memory_preference(user["id"], path=admin_db)
    assert saved is not None and saved["remember"] is True and saved["retention"] == "never"


def test_member_shorter_retention_removes_expired_saved_prompt(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", path=admin_db)
    prompt_memory.put("img_old", user["id"], "old secret", kept_until=None,
                      created_at=int(time.time()) - 100 * 86400, path=prompt_db)
    response = _client(admin_db, prompt_db, user_id=user["id"]).put(
        "/me/api/prompt-memory", json={"remember": True, "retention": "3_months"},
        headers={"x-azizam-user-action": "1"},
    )
    assert response.status_code == 200
    assert prompt_memory.get("img_old", user["id"], path=prompt_db) is None


def test_member_prompt_purge_reports_pending_work_for_off_and_delete(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", path=admin_db)
    client = _client(
        admin_db, prompt_db, user_id=user["id"],
        on_person_prompt_purge=lambda _person_id: {"metadata_stripping": {"pending": 2}},
    )
    updated = client.put(
        "/me/api/prompt-memory", json={"remember": False, "retention": "never"},
        headers={"x-azizam-user-action": "1"},
    )
    assert updated.status_code == 202
    assert updated.json()["prompt_purge"]["metadata_stripping"]["pending"] == 2

    deleted = client.delete("/me/api/prompt-memory/prompts", headers={"x-azizam-user-action": "1"})
    assert deleted.status_code == 202
    assert deleted.json()["prompt_purge"]["retry_scheduled"] is True


def test_delete_reports_retry_required_when_erasure_intent_cannot_be_saved(tmp_path, monkeypatch):
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompts.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "maker", email="maker@test", path=admin_db)
    def purge(person_id):
        return prompt_memory.mark_person_erasure(person_id, admin_path=admin_db)
    def busy(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(prompt_memory, "mark_person_erasure", busy)
    client = _client(admin_db, prompt_db, user_id=user["id"], on_person_prompt_purge=purge)
    response = client.delete("/me/api/prompt-memory/prompts", headers={"x-azizam-user-action": "1"})
    assert response.status_code == 503
    assert response.json()["prompt_purge"]["retry_scheduled"] is False
    assert response.json()["prompt_purge"]["retry_required"] is True


def test_off_erasure_intent_survives_failure_and_reenable(tmp_path, monkeypatch):
    admin_db, prompt_db = tmp_path / "admin.db", tmp_path / "prompts.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "maker", path=admin_db)
    client = _client(admin_db, prompt_db, user_id=user["id"])
    original = prompt_memory.prune_person_before
    monkeypatch.setattr(prompt_memory, "prune_person_before", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    response = client.put("/me/api/prompt-memory", json={"remember": False, "retention": "never"},
                          headers={"x-azizam-user-action": "1"})
    assert response.status_code == 202
    assert response.json()["prompt_purge"]["database_pending"] is True
    cutoff = prompt_memory.recorded_erasure_cutoff(user["id"], admin_path=admin_db)
    client.put("/me/api/prompt-memory", json={"remember": True, "retention": "never"},
               headers={"x-azizam-user-action": "1"})
    assert prompt_memory.recorded_erasure_cutoff(user["id"], admin_path=admin_db) == cutoff
    assert client.get("/me/api/prompt-memory").json()["prompt_purge"]["database_pending"] is True
    monkeypatch.setattr(prompt_memory, "prune_person_before", original)


def test_off_intent_write_failure_leaves_preference_unchanged(tmp_path, monkeypatch):
    admin_db, prompt_db = tmp_path / "admin.db", tmp_path / "prompts.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "maker", path=admin_db)
    client = _client(admin_db, prompt_db, user_id=user["id"])
    monkeypatch.setattr(admin_store, "_record_prompt_erasure", lambda *a: (_ for _ in ()).throw(RuntimeError("locked")))
    response = client.put("/me/api/prompt-memory", json={"remember": False, "retention": "never"},
                          headers={"x-azizam-user-action": "1"})
    assert response.status_code == 503
    assert client.get("/me/api/prompt-memory").json()["prompt_memory"]["remember"] is True


def _retention_client(scope, admin_db, prompt_db, user_id, monkeypatch):
    if scope == "member":
        return (
            _client(admin_db, prompt_db, user_id=user_id),
            "/me/api/prompt-memory",
            {"x-azizam-user-action": "1"},
            {"remember": True, "retention": "3_months"},
            "retention",
            "put_prompt_memory_preference",
        )
    owner = SimpleNamespace(
        subject=user_id, roles=("owner",), actor_kind="user",
        audit_actor_id=user_id, actor_label="Owner",
    )
    monkeypatch.setattr(settings_api, "_require_admin", lambda _auth, _request: owner)
    app = FastAPI()
    app.include_router(settings_api.create_settings_router(
        auth=None, admin_path=admin_db, config_provider=lambda: {},
        prompt_memory_path=prompt_db,
    ))
    client = TestClient(app)
    path = "/admin/api/settings/prompt-memory"
    revision = client.get(path).json()["revision"]
    return (
        client, path, {"if-match": revision},
        {"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
        "retention_cap", "put_operator_setting_with_audit",
    )


@pytest.mark.parametrize("scope", ["member", "household"])
def test_saved_retention_cleanup_failure_returns_accepted(tmp_path, monkeypatch, scope):
    admin_db, prompt_db = tmp_path / "admin.db", tmp_path / "prompts.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "maker", path=admin_db)
    client, path, headers, body, retention_key, _ = _retention_client(
        scope, admin_db, prompt_db, user["id"], monkeypatch,
    )
    cleanup_calls = []

    def busy(*args, **kwargs):
        cleanup_calls.append((args, kwargs))
        raise RuntimeError("database is locked")

    monkeypatch.setattr(prompt_memory, "tighten_retention", busy)
    response = client.put(path, json=body, headers=headers)

    assert response.status_code == 202
    assert response.json() == {"error": {
        "code": "prompt_retention_pending",
        "message": "The setting was saved. Older prompt cleanup is pending and will be retried.",
    }}
    assert response.headers["cache-control"] == "no-store"
    assert len(cleanup_calls) == 1
    assert client.get(path).json()["prompt_memory"][retention_key] == "3_months"


@pytest.mark.parametrize("scope", ["member", "household"])
def test_unsaved_retention_write_failure_remains_unavailable(tmp_path, monkeypatch, scope):
    admin_db, prompt_db = tmp_path / "admin.db", tmp_path / "prompts.db"
    admin_store.init_db(admin_db)
    user = admin_store.upsert_oidc_user("issuer", "maker", path=admin_db)
    client, path, headers, body, _, writer = _retention_client(
        scope, admin_db, prompt_db, user["id"], monkeypatch,
    )
    before = client.get(path).json()["prompt_memory"]
    cleanup_calls = []

    def busy(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(admin_store, writer, busy)
    monkeypatch.setattr(prompt_memory, "tighten_retention",
                        lambda *args, **kwargs: cleanup_calls.append((args, kwargs)))
    response = client.put(path, json=body, headers=headers)

    assert response.status_code == 503
    assert response.json() == {"error": {
        "code": "prompt_erasure_unavailable",
        "message": "The setting wasn’t saved. Try again.",
    }}
    assert client.get(path).json()["prompt_memory"] == before
    assert cleanup_calls == []
