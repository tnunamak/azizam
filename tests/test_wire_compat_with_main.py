"""Decision D26: moving secrets into the store changes nothing an engine can see.

``tests/fixtures/wire_golden_main.json`` was recorded from ``origin/main`` (5f9a825f) by
``tests/support/wire_golden_main.py`` for records that hold their secrets inline: the headers and URL of a native call,
the executable router config, the argument lists and environments the command and process drivers start commands with,
and the URL a readiness probe requests. This test migrates the same records into the secret store and computes the same
facts with the code of this branch, resolving every reference at the moment of use. They must be equal, byte for byte,
and the migrated records must hold none of the secrets.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import admin_store, litellm_runtime, record_carriers, runtime_secrets, secret_refs, service_registry
from gateway.engine_observations import ObservationReader
from gateway.engine_process import ProcessDriver
from gateway.lifecycle_drivers import CommandDriver

FIXTURES = Path(__file__).parent / "fixtures"
RECORDS = json.loads((FIXTURES / "wire_legacy_records.json").read_text())
GOLDEN = json.loads((FIXTURES / "wire_golden_main.json").read_text())
SECRETS = ["wirepass-1", "wirequery-2", "wire-region-eu-3", "wire-team-4", "wire-token-5", "wire-key-6", "wire-pw-7", "wire-url-8",
           "wire-pass-9", "wire-secret-10", "wire-env-11", "wire-key-12", "wire-auth-13", "wire-user-14", "wire-env-15", "wire-probe-16"]


class _Refused:
    async def __aenter__(self) -> None:
        import httpx

        raise httpx.ConnectError("no engine runs here")

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeClient:
    seen: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict, timeout: float):  # noqa: ANN201
        FakeClient.seen.append({"method": method, "url": url, "headers": dict(headers)})
        return _Refused()


@pytest.fixture
def migrated(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    put = secret_refs.putter(path)
    records = {rid: record_carriers.externalize_service(record, put)[0] for rid, record in RECORDS.items()}
    yield records
    runtime_secrets.configure(None)


def test_the_migrated_records_hold_none_of_the_secrets(migrated):
    text = json.dumps(migrated)
    assert not [secret for secret in SECRETS if secret in text]


def test_a_native_call_goes_to_the_same_url_with_the_same_headers(migrated, monkeypatch):
    monkeypatch.setattr(proxy, "config", {"backends": {rid: service_registry.runtime_backend_from_service(rec) for rid, rec in migrated.items()}})
    targets = proxy._build_provider_native_targets()
    seen = {rid: {"base": proxy._native_engine_base(target), "headers": proxy._native_engine_headers(target)} for rid, target in targets.items()}
    assert seen == GOLDEN["native"]
    # What the gateway holds between requests is references: the target names no value.
    assert "wire-region-eu-3" not in json.dumps({rid: {k: v for k, v in t.items() if k != "record"} for rid, t in targets.items()}, default=str)


def test_the_executable_router_config_is_the_same(migrated):
    compiled = litellm_runtime.compile_executable_config({"services": {"engine-a": migrated["engine-a"]}})
    assert json.loads(json.dumps(compiled)) == GOLDEN["litellm"]


def test_the_command_driver_starts_the_same_commands_with_the_same_environment(migrated):
    cmd = migrated["engine-cmd"]["lifecycle"]["driver_config"]
    assert {key: CommandDriver._command(cmd, key) for key in ("start", "stop", "status")} == GOLDEN["command"]
    env = CommandDriver._env(cmd)
    assert {key: env[key] for key in RECORDS["engine-cmd"]["lifecycle"]["driver_config"]["env"]} == GOLDEN["command_env"]


def test_the_process_driver_starts_the_same_argument_list_with_the_same_environment(migrated):
    svc = {**migrated["engine-proc"], "connection": {"address": "http://127.0.0.1:8192"}}
    config, argv, port = ProcessDriver()._validated(svc)
    environment = ProcessDriver._environment(config)
    assert {"argv": argv, "port": port, "env": {key: environment[key] for key in RECORDS["engine-proc"]["process"]["env"]}} == GOLDEN["process"]


def test_a_readiness_probe_requests_the_same_url(migrated):
    svc = {**migrated["engine-proc"], "connection": {"address": "http://127.0.0.1:8192"}}
    FakeClient.seen.clear()
    asyncio.run(ObservationReader(client_factory=FakeClient).read(svc, "process_ready"))
    assert FakeClient.seen == GOLDEN["probe"]


def test_a_record_whose_secret_cannot_be_read_never_sends_a_part_of_it(migrated, tmp_path):
    """Fail closed: a lost secret means the command is not run and the header is not sent, never a partial request."""
    runtime_secrets.configure(tmp_path / "another.db")  # a database that holds none of them
    admin_store.init_db(tmp_path / "another.db")
    cmd = migrated["engine-cmd"]["lifecycle"]["driver_config"]
    assert CommandDriver._command(cmd, "start") == []
    target = {"host": "http://engine-a.invalid:8080/v1", "header_rows": migrated["engine-a"]["config"]["headers"]}
    assert proxy._native_engine_headers(target) == {}
