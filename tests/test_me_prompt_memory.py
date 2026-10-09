from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, prompt_memory
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
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_me_prompt_memory_router(
            session_provider=_session(user_id),
            mutation_validator=_validator,
            household_provider=lambda: household or {},
            admin_db_path=admin_db,
            prompt_memory_path=prompt_db,
        )
    )
    return TestClient(app)


def test_member_prompt_memory_defaults_and_put_are_admin_store_backed(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", user_id="usr_alice", path=admin_db)
    client = _client(admin_db, prompt_db, household={"retention_cap": "1_year"})

    response = client.get("/me/api/prompt-memory")
    assert response.status_code == 200
    assert response.json()["prompt_memory"] == {
        "remember": True,
        "retention": "3_months",
        "effective_remember": True,
        "effective_retention": "3_months",
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
    assert admin_store.get_prompt_memory_preference("usr_alice", path=admin_db)["remember"] is False
    assert updated.json()["prompt_memory"]["effective_remember"] is False


def test_member_prompt_memory_honors_household_kill_switch_and_delete_all(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", user_id="usr_alice", path=admin_db)
    prompt_memory.put("img_one", "usr_alice", "sentinel text", kept_until=None, path=prompt_db)
    client = _client(admin_db, prompt_db, household={"never_keep": True})

    response = client.get("/me/api/prompt-memory")
    assert response.json()["prompt_memory"]["effective_remember"] is False
    assert client.delete("/me/api/prompt-memory/prompts").status_code == 403

    deleted = client.delete(
        "/me/api/prompt-memory/prompts",
        headers={"x-azizam-user-action": "1"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_prompts": 1}
    assert prompt_memory.get("img_one", "usr_alice", path=prompt_db) is None


def test_member_prompt_memory_rejects_bad_settings(tmp_path: Path) -> None:
    admin_db = tmp_path / "admin.db"
    prompt_db = tmp_path / "prompt.db"
    admin_store.init_db(admin_db)
    admin_store.upsert_oidc_user("issuer", "alice", email="alice@example.test", user_id="usr_alice", path=admin_db)
    client = _client(admin_db, prompt_db)

    response = client.put(
        "/me/api/prompt-memory",
        json={"remember": "yes", "retention": "soon"},
        headers={"x-azizam-user-action": "1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_prompt_memory_settings"
    assert admin_store.get_prompt_memory_preference("usr_alice", path=admin_db) is None
