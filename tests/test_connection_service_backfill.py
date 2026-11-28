"""A connection made before the registry kept its service gets that service at start (no plan changes, once only)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, registry_snapshot
from tests.support import access as acc
from tests.support.subscription_fakes import KIND, Upstream, body_of, connect, make_service
from tests.test_subscription_plan_access import COMPLETED, RESPONSES_PATH, _receipts_table, sse  # noqa: F401  (fixture)

SLUG = "gpt-test"        # the one model the fake upstream lists
PIN = f"{KIND.model_prefix}/{SLUG}"
CONNECTION = "chatgpt-owner"
AUTH = {"authorization": "Bearer k", "x-api-key": "k"}
# An OpenAI-name pattern that would catch the bare name and send it to a local model when no plan route claims it.
OPENAI_NAME_PATTERNS = {"chat": {"stable": {"default": "local-default"}, "openai": [{"match": "gpt-*", "to": "default"}]}}


class Install:
    """A gateway install from before the registry kept connection services: a connected plan and no service for it."""

    def __init__(self, tmp_path):
        self.admin = tmp_path / "admin.db"
        admin_store.init_db(self.admin)
        self.owner = admin_store.upsert_oidc_user("https://issuer.test", "owner", path=self.admin)["id"]
        plan = admin_store.create_access_plan(acc.plan_document("Owner plan"), path=self.admin)
        with sqlite3.connect(self.admin) as db:
            db.execute("INSERT OR REPLACE INTO account_access(user_id,plan_id) VALUES(?,?)", (self.owner, plan["id"]))
        self.upstream = Upstream()
        self.service, self.store, jwks = make_service(tmp_path, self.upstream)
        self.store._secrets_path = lambda: self.admin
        asyncio.run(connect(self.service, jwks, self.upstream, owner=self.owner))
        # Connecting seeds the service today; an install from train 122 never had it.
        # The backfill must not grant: take the connect's own grant off the owner's plan, so any grant it made shows.
        with sqlite3.connect(self.admin) as db:
            db.execute("UPDATE access_plans SET plan_json=? WHERE id=?",
                       (json.dumps({**json.loads(db.execute("SELECT plan_json FROM access_plans WHERE id=?", (plan["id"],)).fetchone()[0]),
                                    "connections": []}, sort_keys=True), plan["id"]))
        self.seeded_by_connect = self.registry_services()
        assert set(self.seeded_by_connect) == {KIND.model_prefix}
        record = admin_store.get_service_registry_with_generation(path=self.admin)
        admin_store.write_service_registry({"services": {}}, expected_generation=record["operator_settings_generation"],
                                           stored_form=True, path=self.admin)
        assert self.registry_services() == {}
        self.provider = registry_snapshot.Provider(self.snapshot())
        self.service.snapshot = self.provider.current
        self.service.changed = lambda: self.provider.swap(self.snapshot())

    def snapshot(self, **kwargs):
        return registry_snapshot.load_current(self.admin, **kwargs)

    def registry_services(self):
        """The owner's stored registry document: what the backfill and a connect write, without the release services."""
        record = admin_store.get_service_registry_json_with_generation(path=self.admin)
        return (json.loads(record["value_json"]) if record else {}).get("services") or {}

    def registry_generation(self):
        return admin_store.get_service_registry_with_generation(path=self.admin)["operator_settings_generation"]

    def plans(self):
        with closing(sqlite3.connect(self.admin)) as db:
            return (db.execute("SELECT id, plan_json, updated_at FROM access_plans ORDER BY id").fetchall(),
                    db.execute("SELECT user_id, plan_id FROM account_access ORDER BY user_id").fetchall())


@pytest.fixture
def install(tmp_path):
    return Install(tmp_path)


def test_a_connection_without_a_service_derives_nothing_until_the_backfill(install):
    before = install.snapshot()
    assert before.model_routes.pins == {}
    assert install.service.backfill_services() == 1
    after = install.snapshot()
    assert set(after.model_routes.pins) == {PIN}
    rows = after.services[KIND.model_prefix]["config"]["models"]
    assert [row["id"] for row in rows] == [SLUG]
    assert after.services[KIND.model_prefix]["config"]["credential_source"] == {"kind": "connection", "connection_id": CONNECTION}


def test_the_backfill_swaps_the_serving_snapshot_without_a_restart(install):
    held = install.provider.current()
    assert held.model_routes.pins == {}
    install.service.backfill_services()
    assert set(install.provider.current().model_routes.pins) == {PIN}
    assert held.model_routes.pins == {}   # a request that took the old snapshot keeps it whole


def test_the_seeded_service_is_the_one_a_connect_seeds(install):
    install.service.backfill_services()
    assert install.registry_services() == install.seeded_by_connect


def test_running_the_backfill_twice_writes_the_registry_once(install):
    first = install.service.backfill_services()
    generation = install.registry_generation()
    document = install.registry_services()
    second = install.service.backfill_services()
    assert (first, second) == (1, 0)
    assert install.registry_generation() == generation
    assert install.registry_services() == document


def test_the_backfill_changes_no_access_plan(install):
    before = install.plans()
    assert json.loads(before[0][0][1])["connections"] == []
    assert install.service.backfill_services() == 1
    assert install.plans() == before


def test_a_service_id_taken_by_another_service_is_a_warning_and_no_write(install, caplog):
    taken = {"id": KIND.model_prefix, "name": "mine", "kind": "provider", "enabled": True,
             "config": {"base_url": "http://other.test", "models": []}}
    record = admin_store.get_service_registry_with_generation(path=install.admin)
    admin_store.write_service_registry({"services": {KIND.model_prefix: taken}},
                                       expected_generation=record["operator_settings_generation"],
                                       stored_form=True, path=install.admin)
    before = (install.registry_generation(), install.registry_services(), install.plans())
    with caplog.at_level(logging.WARNING):
        assert install.service.backfill_services() == 0
    assert (install.registry_generation(), install.registry_services(), install.plans()) == before
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "not seeded" in r.getMessage()]
    assert lines == [f"connection service not seeded: connection {CONNECTION}, service {KIND.model_prefix}"]


def test_a_registry_that_already_serves_the_connection_under_another_id_is_left_alone(install):
    existing = {"id": "my-chatgpt", "name": "my-chatgpt", "kind": "provider", "enabled": True, "config": {
        "credential_source": {"kind": "connection", "connection_id": CONNECTION},
        "request_protocol": KIND.protocol, "billing_mode": "plan", "models": []}}
    record = admin_store.get_service_registry_with_generation(path=install.admin)
    admin_store.write_service_registry({"services": {"my-chatgpt": existing}},
                                       expected_generation=record["operator_settings_generation"],
                                       stored_form=True, path=install.admin)
    generation = install.registry_generation()
    assert install.service.backfill_services() == 0
    assert install.registry_generation() == generation
    assert set(install.registry_services()) == {"my-chatgpt"}


def test_a_start_survives_a_registry_it_cannot_write(install, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(admin_store, "backfill_connection_service", broken)
    with caplog.at_level(logging.WARNING):
        assert install.service.backfill_services() == 0
    assert [r.getMessage() for r in caplog.records if "not seeded" in r.getMessage()] == [
        f"connection service not seeded: connection {CONNECTION}, service {KIND.model_prefix}"]


def test_the_backfill_reads_no_secret(install, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the backfill must not read the token store")
    monkeypatch.setattr(install.store, "load_tokens", forbidden)
    from gateway import secret_store
    monkeypatch.setattr(secret_store, "get_provider_secret", forbidden)
    assert install.service.backfill_services() == 1


def test_a_browser_write_still_checks_the_sign_in(install):
    """The backfill carries no sign-in; a browser write that names one is still refused when it has ended."""
    from gateway import session_guard
    ended = session_guard.BrowserSession(user_id=install.owner, session_id="gone", family_id="gone",
                                         expires_at=1.0, absolute_expires_at=1.0)
    with pytest.raises(session_guard.SessionEnded):
        admin_store.bind_subscription_connection(install.owner, CONNECTION, KIND.model_prefix, {"models": []},
                                                 session=ended, path=install.admin)
    assert install.registry_services() == {}


# ------------------------------------------------------------------ through the real app

@pytest.fixture
def served(install, monkeypatch):
    """The app with the install's service and the snapshot the start-up backfill swaps in; plans come from the request."""
    install.upstream.routes[("POST", RESPONSES_PATH)] = lambda request: __import__("httpx").Response(
        200, content=sse({"type": "response.created", "response": {}}, COMPLETED),
        headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(proxy, "GATEWAY_REGISTRY_PROVIDER", install.provider)
    monkeypatch.setattr(proxy, "SUBSCRIPTIONS", install.service)
    monkeypatch.setitem(proxy.config, "model_aliases", OPENAI_NAME_PATTERNS)
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides", lambda: {})
    # The listing's other sources are not under test: the same stand-ins the catalog tests use.
    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(proxy, "_protocol_models_payload", AsyncMock(return_value=[]))
    monkeypatch.setattr(proxy, "_build_workflow_model_entries", lambda: [])
    monkeypatch.setattr(proxy, "_current_loaded_model_entries", lambda: [])
    monkeypatch.setattr(proxy, "_stable_alias_model_entries", lambda: [])
    monkeypatch.setattr(proxy, "_decision_models_for", lambda *a: [])
    monkeypatch.setattr(proxy, "_annotate_model_context_windows", AsyncMock())
    monkeypatch.setattr(proxy, "_annotate_model_availability", lambda data, **kw: data)
    return install


def request(method, path, *, body=None, granted=True):
    plan = acc.plan_for("plan_app", connections=[CONNECTION] if granted else [])
    with acc.authenticated(acc.identity(plan, person_id="usr_app")):
        client = TestClient(proxy.app)
        return client.get(path, headers=AUTH) if method == "get" else client.post(path, json=body, headers=AUTH)


def listed_ids():
    response = request("get", "/v1/models")
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["data"]}


def test_the_app_serves_the_plan_after_the_backfill(served):
    upstream = served.upstream
    assert SLUG not in listed_ids()
    # Before: the pin is unknown (the bare name would fall to the OpenAI-name pattern, a local model, never the plan).
    assert PIN not in served.provider.current().model_routes.pins

    assert served.service.backfill_services() == 1

    ids = listed_ids()
    assert SLUG in ids and PIN not in ids      # the bare name is listed; the pin is an unlisted exact name
    assert SLUG not in listed_ids_for_ungranted()
    chat = request("post", "/v1/chat/completions", body={"model": SLUG, "messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200, chat.text
    assert upstream.count(RESPONSES_PATH) == 1
    pinned = request("post", "/v1/responses", body={"model": PIN, "input": [{"role": "user", "content": "hi"}]})
    assert pinned.status_code == 200, pinned.text
    assert upstream.count(RESPONSES_PATH) == 2
    sent = body_of([c for c in upstream.calls if c.url.path == RESPONSES_PATH][-1])
    assert sent["model"] == SLUG and sent["store"] is False


def listed_ids_for_ungranted():
    response = request("get", "/v1/models", granted=False)
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["data"]}
