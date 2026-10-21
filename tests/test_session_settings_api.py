"""Owner settings for sign-in sessions: bounds, revisions, who may change them, and what they do."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, session_policy
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.session_policy import DAY, MINUTE
from gateway.settings_api import create_settings_router

OWNER = {"test-role": "owner", "x-azizam-csrf": "csrf-fixture"}
ADMIN = {"test-role": "admin", "x-azizam-csrf": "csrf-fixture"}
NEW = {"idle_timeout_days": 14, "absolute_lifetime_days": 60, "step_up_minutes": 5}


@pytest.fixture
def sessions(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)

    def auth(request):
        role = request.headers.get("test-role")
        if not role:
            raise AdminAuthError()
        return AdminContext(subject="tester", roles=frozenset({role}), auth_method="session", csrf_token="csrf-fixture")

    app = FastAPI()
    app.include_router(create_settings_router(auth=auth, admin_path=path, config_provider=lambda: {}))
    return TestClient(app), path


def read(client):
    response = client.get("/admin/api/settings/sessions", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


def save(client, body, revision):
    return client.put("/admin/api/settings/sessions", json=body, headers={**OWNER, "If-Match": revision})


def test_the_owner_reads_the_defaults_with_their_bounds(sessions):
    client, _ = sessions

    body = read(client)

    assert body["sessions"] == {"idle_timeout_days": 30, "absolute_lifetime_days": 90, "step_up_minutes": 15}
    assert body["defaults"] == body["sessions"]
    assert body["bounds"] == {"idle_timeout_days": [1, 90], "absolute_lifetime_days": [1, 365], "step_up_minutes": [1, 1440]}
    assert (body["revision"], body["apply_mode"]) == ("0", "live")


def test_saving_changes_what_new_sessions_get_and_advances_the_revision(sessions):
    client, path = sessions

    saved = save(client, NEW, read(client)["revision"])

    assert saved.status_code == 200, saved.text
    assert saved.json()["sessions"] == NEW and saved.json()["revision"] == "1"
    assert session_policy.load_policy(path) == session_policy.SessionPolicy(14 * DAY, 60 * DAY, 5 * MINUTE)


def test_saving_is_audited_with_the_new_values(sessions):
    client, path = sessions

    save(client, NEW, "0")

    with admin_store._conn(path) as conn:
        rows = conn.execute("SELECT metadata_json FROM audit_events WHERE action='operator.sessions.update'").fetchall()
    assert len(rows) == 1 and "step_up_minutes" in rows[0][0]


@pytest.mark.parametrize("body, words", [
    ({**NEW, "idle_timeout_days": 0}, "idle_timeout_days"),
    ({**NEW, "idle_timeout_days": 91}, "idle_timeout_days"),
    ({**NEW, "absolute_lifetime_days": 366}, "absolute_lifetime_days"),
    ({**NEW, "step_up_minutes": 0}, "step_up_minutes"),
    ({**NEW, "step_up_minutes": 1441}, "step_up_minutes"),
    ({**NEW, "idle_timeout_days": 61}, "idle timeout cannot be longer"),
    ({**NEW, "idle_timeout_days": "14"}, "idle_timeout_days"),
    ({"idle_timeout_days": 14}, "Supply"),
])
def test_values_outside_their_bounds_are_refused_with_a_readable_reason_and_nothing_is_saved(sessions, body, words):
    client, path = sessions

    response = save(client, body, "0")

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_sessions"
    assert words in response.json()["error"]["message"]
    assert session_policy.load_policy(path) == session_policy.SessionPolicy()
    assert read(client)["revision"] == "0"


def test_a_save_needs_the_revision_it_was_read_at(sessions):
    client, _ = sessions

    missing = client.put("/admin/api/settings/sessions", json=NEW, headers=OWNER)
    first = save(client, NEW, "0")
    stale = save(client, {**NEW, "step_up_minutes": 10}, "0")

    assert missing.status_code == 428
    assert first.status_code == 200
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"


def test_only_the_owner_reads_or_changes_them(sessions):
    client, _ = sessions

    assert client.get("/admin/api/settings/sessions", headers=ADMIN).status_code == 403
    assert client.put("/admin/api/settings/sessions", json=NEW, headers={**ADMIN, "If-Match": "0"}).status_code == 403
    assert client.get("/admin/api/settings/sessions").status_code == 401
