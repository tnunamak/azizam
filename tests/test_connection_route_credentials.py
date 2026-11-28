"""Connection credentials survive storage and remain independent of paid grants."""
import copy
import json

from gateway import admin_store, registry_snapshot, service_registry
from tests.support import access as acc


def test_service_credential_fields_survive_save_export_import(tmp_path):
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    for path in (source, destination):
        admin_store.init_db(path)
    config = {"remote_provider": True, "provider_id": "roundtrip-plan", "base_url": "https://example.test/v1",
              "credential_source": {"kind": "connection", "connection_id": "roundtrip-owner"},
              "request_protocol": "openai-responses-plan", "billing_mode": "plan"}
    document = service_registry.normalize_service_registry({"schema_version": 1, "services": {
        "roundtrip-plan": {"id": "roundtrip-plan", "kind": "provider", "config": config}}})
    admin_store.write_service_registry(document, expected_generation=0, path=source)
    exported = json.loads(admin_store.get_service_registry_json_with_generation(path=source)["value_json"])
    imported = service_registry.merge_imported_records({}, services=exported["services"])
    admin_store.write_service_registry(imported, expected_generation=0, path=destination)
    restored = service_registry.load_service_registry(path=destination)
    for field in ("credential_source", "request_protocol", "billing_mode"):
        assert restored["services"]["roundtrip-plan"]["config"][field] == config[field]
    snapshot = registry_snapshot.build(restored, registry_snapshot.BundledRelease({}), generation=1)
    assert snapshot.services["roundtrip-plan"]["config"]["credential_source"]["connection_id"] == "roundtrip-owner"
    changed = copy.deepcopy(restored)
    changed["services"]["roundtrip-plan"]["config"]["credential_source"]["connection_id"] = "another-owner"
    assert registry_snapshot.build(changed, registry_snapshot.BundledRelease({}), generation=1).digest != snapshot.digest


def test_connection_grants_survive_plan_save_export_import(tmp_path):
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    for path in (source, destination):
        admin_store.init_db(path)
    document = acc.plan_document()
    document["connections"] = ["roundtrip-owner"]
    saved = admin_store.create_access_plan(document, path=source)
    exported = json.loads(json.dumps(admin_store.get_access_plan(saved["id"], path=source)["plan"]))
    imported = admin_store.create_access_plan(exported, path=destination)
    assert imported["plan"]["connections"] == ["roundtrip-owner"]
    assert imported["plan"]["paid_providers"] == []


import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import sqlite3

import pytest

from gateway import access_plan_migration, providers
from gateway.access_plans import AccessPlan, plan_from_document, plan_to_document, union_plans
from gateway.policy import EffectiveAccess, PolicyRequest, evaluate
from gateway.provider_connections import SubscriptionError
from tests.support.subscription_fakes import KIND, Upstream, connect, make_service, json_route, token_body, urlparts


@pytest.mark.parametrize("person,app,allowed", [(True, True, True), (False, True, False), (True, False, False), (False, False, False)])
def test_exact_connection_grants_intersect(person, app, allowed):
    base = acc.plan_for()
    connection_id = "plan-owner"
    access = EffectiveAccess(replace(base, connections=frozenset([connection_id]) if person else frozenset()),
                             replace(base, id="app-plan", connections=frozenset([connection_id]) if app else frozenset()))
    assert (connection_id in access.connections) is allowed
    assert evaluate(access, PolicyRequest(intent="chat.completions", model="test", connection_id=connection_id)).allowed is allowed


def test_all_capabilities_and_paid_provider_grants_do_not_grant_connections():
    plan = AccessPlan("all", "All", all_work_types=True, paid_providers=frozenset([KIND.id]))
    assert evaluate(EffectiveAccess(plan, plan), PolicyRequest(intent="chat.completions", connection_id="chatgpt-owner")).denied
    assert EffectiveAccess(None, plan).connections == frozenset()
    assert plan_from_document(plan_to_document(plan)).connections == frozenset()
    assert union_plans("union", "Union", [replace(plan, connections=frozenset(["a"])), replace(plan, connections=frozenset(["b"]))]).connections == {"a", "b"}


def test_connection_migration_is_versioned_and_idempotent(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    doc = acc.plan_document(paid=[KIND.id, "openai"])
    saved = admin_store.create_access_plan(doc, path=path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM schema_migration_steps WHERE name=?", (access_plan_migration.CONNECTION_GRANTS_STEP,))
        access_plan_migration.migrate_connection_grants(db, {KIND.id: "chatgpt-owner"})
        first = db.execute("SELECT plan_json FROM access_plans WHERE id=?", (saved["id"],)).fetchone()[0]
        access_plan_migration.migrate_connection_grants(db, {KIND.id: "chatgpt-owner"})
        assert db.execute("SELECT plan_json FROM access_plans WHERE id=?", (saved["id"],)).fetchone()[0] == first
    migrated = json.loads(first)
    assert migrated["connections"] == ["chatgpt-owner"]
    assert migrated["paid_providers"] == ["openai"]


def test_owner_connect_grants_only_owner_and_seeds_service(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    owner = admin_store.upsert_oidc_user("https://issuer.test", "owner", path=path)["id"]
    other = admin_store.upsert_oidc_user("https://issuer.test", "other", path=path)["id"]
    shared = admin_store.create_access_plan(acc.plan_document("Shared"), path=path)
    with sqlite3.connect(path) as db:
        for user in (owner, other):
            db.execute("INSERT OR REPLACE INTO account_access(user_id,plan_id) VALUES(?,?)", (user, shared["id"]))
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    store._secrets_path = lambda: path
    asyncio.run(connect(service, jwks, upstream, owner=owner))
    assigned = admin_store.person_plan_id(owner, path=path)
    assert assigned != shared["id"]
    assert admin_store.get_access_plan(assigned, path=path)["plan"]["connections"] == ["chatgpt-owner"]
    assert admin_store.person_plan_id(other, path=path) == shared["id"]
    assert admin_store.get_access_plan(shared["id"], path=path)["plan"]["connections"] == []
    record = service_registry.load_service_registry(path=path)["services"][KIND.model_prefix]
    assert record["config"]["credential_source"] == {"kind": "connection", "connection_id": "chatgpt-owner"}
    assert record["config"]["request_protocol"] == KIND.protocol
    assert record["config"]["billing_mode"] == "plan"


def test_disconnect_invalidates_leases_and_reconnect_advances_generation(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    lease = asyncio.run(service.acquire("chatgpt-owner"))
    assert lease.connection_id == "chatgpt-owner"
    asyncio.run(service.disconnect(KIND.id, session=None))
    with pytest.raises(SubscriptionError):
        asyncio.run(service.validate_lease(lease))
    asyncio.run(connect(service, jwks, upstream))
    assert asyncio.run(service.acquire("chatgpt-owner")).generation > lease.generation


@pytest.mark.parametrize("order", ["lease-first", "disconnect-first"])
def test_disconnect_during_refresh_cannot_restore_credentials(tmp_path, monkeypatch, order):
    upstream = Upstream()
    now = [1_000_000.0]
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    asyncio.run(connect(service, jwks, upstream))
    now[0] += 3600
    mechanism = service.mechanisms[KIND.mechanism]
    original = mechanism._post_form

    async def race():
        import threading
        entered, release, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lease_returned = threading.Event()
        acquire, invalidate = mechanism.acquire, store.invalidate

        async def blocked(ctx, url, fields):
            if fields.get("grant_type") == "refresh_token":
                entered.set()
                await release.wait()
                return 200, token_body("late-access", "late-refresh")
            return await original(ctx, url, fields)

        async def acquired(*args, **kwargs):
            credential = await acquire(*args, **kwargs)
            if order == "disconnect-first":
                await disconnected.wait()  # final require_generation must observe the disconnected row
            return credential

        def invalidating(*args, **kwargs):
            if order == "lease-first":
                assert lease_returned.wait(5)  # final generation check observes the still-connected row
            return invalidate(*args, **kwargs)

        monkeypatch.setattr(mechanism, "_post_form", blocked)
        monkeypatch.setattr(mechanism, "acquire", acquired)
        monkeypatch.setattr(store, "invalidate", invalidating)
        task = asyncio.create_task(service.acquire("chatgpt-owner"))
        await entered.wait()
        disconnecting = asyncio.create_task(service.disconnect(KIND.id, session=None))
        await asyncio.sleep(0)
        assert not disconnecting.done()
        release.set()
        if order == "disconnect-first":
            await disconnecting
            disconnected.set()
            with pytest.raises(SubscriptionError) as refused:
                await task
        else:
            try:
                lease = await task
                assert lease.connection_id == "chatgpt-owner" and lease.generation == 1
                await service.validate_lease(lease)
            finally:
                lease_returned.set()
            await disconnecting
            with pytest.raises(SubscriptionError) as refused:
                await service.validate_lease(lease)
        assert (refused.value.status, refused.value.code) == (503, "plan_connection_invalid")
    asyncio.run(race())
    assert store.connection(KIND.id)["status"] == "disconnected"
    assert store.load_tokens(KIND.id, origin=KIND.upstream_origin) is None


def test_static_key_configuration_and_paid_reservation_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTION_TEST_STATIC_KEY", "static-fixture-key")
    provider = providers.provider_lookup({"services": {"paid": {"id": "paid", "kind": "provider", "config": {
        "remote_provider": True, "provider_id": "paid", "base_url": "https://example.test/v1",
        "api_key_env": "CONNECTION_TEST_STATIC_KEY", "credential_origin": "https://example.test", "credential_source": {"kind": "static-key"}}}}}, "paid")
    assert provider.configured
    plan = acc.plan_for(paid=["paid"])
    reservation = providers.reserve_provider_spend(acc.identity(plan), provider,
        providers.Cost(Decimal("0.01"), "fixture", "paid"), path=tmp_path / "spend.db")
    assert reservation.id
    assert reservation.estimated_usd == Decimal("0.01")
    providers.release_provider_spend(reservation, path=tmp_path / "spend.db")


def test_service_lookup_does_not_collapse_connections_with_the_same_brand():
    registry = {"services": {name: {"id": name, "kind": "provider", "config": {
        "remote_provider": True, "provider_id": "same-brand", "credential_source": {"kind": "connection", "connection_id": connection}}}
        for name, connection in [("first", "first-owner"), ("second", "second-owner")]}}
    refs = providers.provider_refs(registry)
    assert set(refs) == {"first", "second"}
    assert refs["first"].config["credential_source"]["connection_id"] == "first-owner"
    assert refs["second"].service_id == "second"


def test_connection_aliases_share_one_refresh_lock(tmp_path):
    upstream = Upstream()
    now = [1_000_000.0]
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    asyncio.run(connect(service, jwks, upstream))
    generation = store.connection(KIND.id)["generation"]
    now[0] += 3600
    path = urlparts(KIND.oauth["token_url"])
    upstream.routes[("POST", path)] = json_route(token_body("renewed-access", "renewed-refresh"))
    before = upstream.count(path)
    async def acquire_all():
        return await asyncio.gather(*(service.acquire(name) for name in [KIND.id, "chatgpt-owner"] * 4))
    leases = asyncio.run(acquire_all())
    assert {lease.value for lease in leases} == {"renewed-access"}
    assert {lease.generation for lease in leases} == {generation}
    assert upstream.count(path) - before == 1


def test_schema_22_upgrade_moves_exact_grants_and_preserves_assignments(tmp_path):
    path = tmp_path / "old.db"
    admin_store.init_db(path)
    owner = admin_store.upsert_oidc_user("https://issuer.test", "owner", path=path)["id"]
    saved = admin_store.create_access_plan(acc.plan_document(paid=[KIND.id]), path=path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO account_access(user_id,plan_id) VALUES(?,?)", (owner, saved["id"]))
        db.execute("UPDATE schema_version SET version=22")
        db.execute("DELETE FROM schema_migration_steps WHERE name=?", (access_plan_migration.CONNECTION_GRANTS_STEP,))
    admin_store.init_db(path)
    admin_store.init_db(path)
    assert admin_store.schema_version(path) == admin_store.SCHEMA_VERSION  # later migrations run too
    assert admin_store.person_plan_id(owner, path=path) == saved["id"]
    doc = admin_store.get_access_plan(saved["id"], path=path)["plan"]
    assert doc["paid_providers"] == [] and doc["connections"] == ["chatgpt-owner"]


def test_provider_readiness_uses_mechanism_without_resolving_a_static_key(tmp_path, monkeypatch):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    from gateway import provider_connections
    monkeypatch.setattr(provider_connections, "connection_service", lambda **kwargs: service)
    monkeypatch.setattr(providers, "resolve_provider_api_key", lambda *args, **kwargs: pytest.fail("resolved a static key"))
    ref = providers.provider_lookup({"services": {"plan-service": {"id": "plan-service", "config": {
        "credential_source": {"kind": "connection", "connection_id": "chatgpt-owner"}}}}}, "plan-service")
    assert ref.configured
    asyncio.run(service.disconnect(KIND.id, session=None))
    assert not ref.configured


def test_service_deletion_changes_snapshot_identity(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    before = registry_snapshot.load_current(path)
    admin_store.write_service_registry({"services": {}}, expected_generation=0, path=path)
    after = registry_snapshot.load_current(path)
    assert set(before.services) != set(after.services)
    assert before.digest != after.digest


def test_credential_projection_exposes_labels_but_no_mechanism_material():
    from gateway import projection
    shown = projection.project("ServiceRecord", {"id": "plan", "kind": "provider", "config": {
        "request_protocol": "openai-responses-plan", "billing_mode": "plan",
        "credential_source": {"kind": "connection", "connection_id": "plan-owner", "token": "private-token",
                              "secret": "private-secret", "mechanism_state": {"ticket": "private-state"}}}}).tree
    source = shown["config"]["credential_source"]
    assert source["kind"] == "connection" and source["connection_id"] == "plan-owner"
    assert "private-" not in repr(shown)
    assert source.get("hidden_settings") == 3


def test_reconnect_cannot_publish_new_credentials_under_an_old_lease(tmp_path, monkeypatch):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    asyncio.run(connect(service, jwks, upstream))
    lease = asyncio.run(service.acquire(KIND.id))
    original = store.save_tokens
    observed = []
    def after_secret_write(*args, **kwargs):
        result = original(*args, **kwargs)
        # Pause at the old split between secret publication and connection metadata.
        with pytest.raises(SubscriptionError):
            store.require_generation(lease.connection_id, lease.generation)
        observed.append(True)
        return result
    monkeypatch.setattr(store, "save_tokens", after_secret_write)
    asyncio.run(connect(service, jwks, upstream))
    assert observed == [True]
    assert asyncio.run(service.acquire(KIND.id)).generation > lease.generation
    # A competing transition winning the generation check must abort before another publication.
    monkeypatch.setattr(store, "invalidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "save_tokens", lambda *args, **kwargs: pytest.fail("published after a lost generation check"))
    with pytest.raises(SubscriptionError):
        asyncio.run(connect(service, jwks, upstream))
