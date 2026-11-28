"""S8a over real loopback sockets and a persisted service registry.

No production engine is registered. The status calls and the HTTP lifecycle
driver are real; only host process launch, release data and metrics storage are
isolated. Run this file with the two existing address suites.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.support.fake_upstream.fixtures import fake_upstreams  # noqa: F401
from tests.support.fake_upstream.protocols import PROTOCOLS, Protocol
from tests.support.fake_upstream.scenario import scenario_from_data

SID = "hermetic-address"
PROTOCOL = "hermetic-lifecycle"
RAW_PATH = b"/health%3Fready"
RAW_QUERY = b"tag=first&tag=second&literal=%2F&empty="
DECODED_PATH = "/health?ready"

def _scenario(name, *, health=0, unload=0, raw=False):
    exchanges = []
    if health:
        match = {"method": "GET", "path": DECODED_PATH if raw else "/health"}
        if raw:
            match.update(raw_path=RAW_PATH.decode("ascii"), raw_query=RAW_QUERY.decode("ascii"))
        exchanges.append({
            "id": "health", "match": match, "times": health,
            "response": {"json": {"status": "ready", "ready": True}},
        })
    if unload:
        exchanges.append({
            "id": "unload", "match": {"method": "POST", "path": "/v1/model/unload"},
            "times": unload, "response": {"json": {"ok": True}},
        })
    return scenario_from_data({
        "protocol": PROTOCOL, "name": name,
        "provenance": {"source": "hand-written", "reference": "S8a hermetic ship checks"},
        "exchanges": exchanges,
    })


def _run(coro):
    # Bound all awaited test work without depending on pytest-timeout.
    started = time.monotonic()
    try:
        return asyncio.run(asyncio.wait_for(coro, timeout=1.5))
    finally:
        assert time.monotonic() - started < 2.0, "S8a case exceeded its two-second budget"


@pytest.fixture
def env(monkeypatch, tmp_path, fake_upstreams):
    allowed_ports = set()
    forbidden = []
    in_case = False
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def check_address(address):
        if not (isinstance(address, tuple) and address[0] == "127.0.0.1"
                and address[1] in allowed_ports):
            forbidden.append(("connect", repr(address)))
            raise AssertionError("S8a attempted a non-fixture socket")

    def guarded_connect(sock, address):
        check_address(address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_address(address)
        return connect_ex(sock, address)

    def no_process(*args, **kwargs):
        # status_endpoint's optional import-time git footer is blocked too.
        if in_case:
            forbidden.append(("process", repr(args)))
        raise AssertionError("S8a must not launch a process")

    async def no_async_process(*args, **kwargs):
        return no_process(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_async_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_async_process)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    import status_endpoint as status
    from gateway import admin_store, lifecycle_drivers, metrics_writer, service_registry as registry
    from tests.support.gateway import use_admin_db
    from tests.test_engine_address_from_record import _config
    from tests.test_engine_address_review_fixes import _record

    db = use_admin_db(monkeypatch, tmp_path)
    empty_release = {
        "schema_version": 2, "services": {}, "protocol_frontends": {},
        "workflows": {}, "capabilities": {},
    }
    # Only replace deployment data; the normalizer, CAS writer and loader stay real.
    monkeypatch.setattr(registry, "release_registry", lambda *a, **k: copy.deepcopy(empty_release))
    registry.ensure_service_registry(path=db, actor="hermetic-test")
    monkeypatch.setitem(PROTOCOLS, PROTOCOL, Protocol(
        PROTOCOL, ("/health", "/v1/model/unload", DECODED_PATH), "none"))

    async def plain_http_context():
        return False  # All allowed sockets are plain HTTP on fixture loopback ports.

    monkeypatch.setattr(status, "_shared_probe_ssl_context", plain_http_context)
    monkeypatch.setattr(status, "_HTTP_TIMEOUT", httpx.Timeout(0.25))
    monkeypatch.setattr(status, "_BACKEND_GRID_DEADLINE_S", 0.4)
    monkeypatch.setattr(status, "_SNAPSHOT", {})
    monkeypatch.setattr(status, "_SECTION_AT", {})
    monkeypatch.setattr(status, "_SNAPSHOT_BUILD", None)
    monkeypatch.setattr(status, "_SNAPSHOT_TASK", None)
    monkeypatch.setattr(status, "_SNAPSHOT_CLOSED", False)
    monkeypatch.setattr(status, "_SNAPSHOT_WANTED_UNTIL", 0.0)

    history_db = tmp_path / "hermetic-history.sqlite"
    with sqlite3.connect(history_db) as conn:
        conn.execute("CREATE TABLE samples (at INTEGER, metric TEXT, tags TEXT, value REAL)")

    class HistoryWriter:
        calls = 0

        def record_samples(self, samples):
            self.calls += 1
            with sqlite3.connect(history_db) as conn:
                conn.executemany("INSERT INTO samples VALUES (?, ?, ?, ?)", samples)
            return True

        def flush(self):
            pass

    writer = HistoryWriter()
    monkeypatch.setattr(metrics_writer, "get_writer", lambda: writer)

    async def write_samples(rows):
        # Keep real status-to-history conversion; only the final writer is private.
        status._record_backend_health_samples(rows)

    monkeypatch.setattr(status, "_write_backend_samples", write_samples)

    def history():
        with sqlite3.connect(history_db) as conn:
            return conn.execute("SELECT at, metric, tags, value FROM samples ORDER BY rowid").fetchall()

    def serve(name, **kwargs):
        fake = fake_upstreams.loopback(_scenario(name, **kwargs))
        allowed_ports.add(httpx.URL(fake.base_url).port)
        return fake

    def service(field, url, *, enabled=True, endpoint="/health", managed=False):
        config = _record(field, url, health_endpoint=endpoint)
        if field == "connection.address":
            # The schema-2 engine validator also requires its routing address.
            config["address"] = url
        driver_config = {"unload_url": "/v1/model/unload"}
        config["driver_config"] = driver_config
        return {
            "id": SID, "name": "Hermetic Engine", "kind": "engine", "source": "ui",
            "enabled": enabled, "role": "required", "config": config,
            "lifecycle": {
                "driver": "systemd" if managed else "http-load-unload",
                "driver_config": {"unit": "must-never-run.service"} if managed else driver_config,
            },
        }

    def save(record):
        document = registry.normalize_service_registry({
            **empty_release, "services": {SID: record},
        })
        old = admin_store.get_service_registry_json_with_generation(path=db)
        admin_store.write_service_registry_with_audit(
            document, expected_generation=old["operator_settings_generation"],
            updated_by="hermetic-test", audit_action="operator.service_registry.test",
            audit_actor_kind="admin", audit_actor_id="hermetic-test",
            audit_actor_label="Hermetic test", audit_target_kind="operator_setting",
            audit_target_id=registry.SERVICE_REGISTRY_SETTING_KEY, audit_metadata={},
            session=None, path=db,
        )
        # New loader call after each write: never pass our input dict to status.
        loaded = registry.load_service_registry(path=db)
        assert loaded is not None and SID in loaded["services"]
        return loaded

    def reload_config(bootstrap):
        loaded = registry.load_service_registry(path=db)
        assert loaded is not None
        return registry.apply_service_registry_to_config(bootstrap, loaded)

    async def grid(config):
        return await status._backend_grid(config, deadline_s=0.4)

    async def stop(config):
        # Real synchronous HTTP driver, no service manager or fake request client.
        driver = lifecycle_drivers.HttpLoadUnloadDriver()
        def stop_on_worker():
            # The driver's sync stop() runs its own loop; a worker thread has none,
            # and the driver swallows that error, so give the thread a private loop.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                driver.stop(config["backends"][SID])
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        await asyncio.to_thread(stop_on_worker)

    in_case = True
    yield SimpleNamespace(
        serve=serve, service=service, save=save, reload=reload_config,
        config=_config, grid=grid, stop=stop, status=status, history=history, writer=writer,
    )
    assert forbidden == [], forbidden


@pytest.mark.parametrize("field", ["base_url", "connection.address"])
def test_record_address_beats_bootstrap_and_reload_moves_health_and_lifecycle(env, field):
    async def case():
        a = env.serve("record-A", health=1, unload=1)
        b = env.serve("poison-bootstrap-B")
        c = env.serve("record-C", health=1, unload=1)
        bootstrap = env.config(**{SID: {"base_url": b.base_url, "health_endpoint": "/health"}})
        env.save(env.service(field, a.base_url))
        first = env.reload(bootstrap)
        rows = await env.grid(first)
        assert [row["key"] for row in rows] == [SID]
        assert rows[0]["url"] == a.base_url + "/health"
        await env.stop(first)
        assert len(a.calls("health")) == len(a.calls("unload")) == 1
        assert b.requests == [] and c.requests == []

        env.save(env.service(field, c.base_url))
        second = env.reload(bootstrap)
        rows = await env.grid(second)
        assert [row["url"] for row in rows] == [c.base_url + "/health"]
        await env.stop(second)
        assert len(c.calls("health")) == len(c.calls("unload")) == 1
        assert len(a.requests) == 2 and b.requests == []
        # A previously read runtime object is not mutated into the new endpoint.
        assert first["backends"][SID] != second["backends"][SID]

    _run(case())


@pytest.mark.parametrize("field", ["base_url", "connection.address"])
def test_declared_endpoint_keeps_raw_path_and_repeated_query_bytes(env, field):
    async def case():
        fake = env.serve("encoded-endpoint", health=1, raw=True)
        # A "/" endpoint is origin-relative for health (join_health), so no base prefix or query is used here.
        base = fake.base_url
        endpoint = "/health%3Fready?tag=first&tag=second&literal=%2F&empty="
        env.save(env.service(field, base, endpoint=endpoint))
        rows = await env.grid(env.reload(env.config()))
        assert [row["key"] for row in rows] == [SID]
        [request] = fake.calls("health")
        assert request.raw_path == RAW_PATH
        assert request.raw_query == RAW_QUERY
        assert request.path == DECODED_PATH
        assert request.raw_query.count(b"tag=") == 2

    _run(case())


@pytest.mark.parametrize("warm", [False, True], ids=["disabled-before-start", "disabled-after-warm-cache"])
@pytest.mark.parametrize("enabled", [False, "false", "0", "no", "off"], ids=repr)
@pytest.mark.parametrize("managed", [False, True], ids=["http-lifecycle", "forbidden-service-manager"])
def test_disabled_record_has_no_requests_status_row_or_new_history_across_reload(env, warm, enabled, managed):
    _run(_disable_and_reload(env, warm, enabled, managed, check_cached_row=not warm))


@pytest.mark.parametrize("enabled", [False, "false", "0", "no", "off"], ids=repr)
@pytest.mark.parametrize("managed", [False, True], ids=["http-lifecycle", "forbidden-service-manager"])
def test_cached_status_drops_the_row_of_an_engine_disabled_by_reload(env, enabled, managed):
    _run(_disable_and_reload(env, True, enabled, managed, check_cached_row=True))


async def _disable_and_reload(env, warm, enabled, managed, *, check_cached_row):
    fake = env.serve("disabled", health=2 if warm else 0)
    bootstrap = env.config(**{SID: {"base_url": fake.base_url, "health_endpoint": "/health"}})
    if warm:
        env.save(env.service("base_url", fake.base_url))
        current = env.reload(bootstrap)
        rows = await env.grid(current)
        assert [row["key"] for row in rows] == [SID]
        await env.status._sample_backend_health(current)
        assert len(fake.calls("health")) == 2
        assert env.writer.calls == 1 and env.history()
        assert all(json.loads(row[2])["backend"] == SID for row in env.history())
        env.status._SECTION_AT["backends"] = env.status._now()
        env.status._store_snapshot({"backends": rows})

    requests_before = len(fake.requests)
    history_before = env.history()
    writes_before = env.writer.calls
    env.save(env.service("base_url", fake.base_url, enabled=enabled, managed=managed))
    # Warm the registry's parsed-snapshot cache, then reload it again.
    first = env.reload(bootstrap)
    current = env.reload(bootstrap)
    assert first["backends"][SID]["enabled"] is False
    assert current["backends"][SID]["enabled"] is False
    assert env.status._resolve_backend_targets(current) == []
    assert env.status._lifecycle_backend_targets(current) == []
    assert await env.grid(current) == []
    await env.status._sample_backend_health(current)
    assert len(fake.requests) == requests_before
    assert env.history() == history_before
    assert env.writer.calls == writes_before
    page = await env.status.get_cached_status(current)
    if check_cached_row:
        # The status cache is deliberately not cleared before this check.
        assert not [row for row in page["backends"] if row.get("key") == SID]
    assert len(fake.requests) == requests_before
