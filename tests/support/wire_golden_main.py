"""Run against a checkout of ``main`` to record what the gateway sends an engine for records that hold inline secrets.

    cd <checkout of main> && PYTHONPATH=. python <this file> tests/fixtures/wire_legacy_records.json > golden.json

It records the request headers and URL of a native call, the executable router config, the argument lists and
environments the lifecycle drivers start commands with, and the URL a readiness probe requests. The branch that moves
secrets into the encrypted store (decision D26) must produce exactly these for the same records after migrating them
(``tests/test_wire_compat_with_main.py`` compares against ``tests/fixtures/wire_golden_main.json``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.getcwd())

import proxy  # noqa: E402
from gateway import service_registry  # noqa: E402
from gateway.engine_observations import ObservationReader  # noqa: E402
from gateway.engine_process import ProcessDriver  # noqa: E402
from gateway.lifecycle_drivers import CommandDriver  # noqa: E402
from gateway.litellm_substrate import compile_litellm_substrate_config  # noqa: E402


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


def main(path: str) -> dict[str, Any]:
    records = json.load(open(path))
    out: dict[str, Any] = {}

    proxy.config = {"backends": {rid: service_registry.runtime_backend_from_service(rec) for rid, rec in records.items()}}
    targets = proxy._build_provider_native_targets()
    out["native"] = {rid: {"base": target["host"], "headers": dict(target.get("headers") or {})} for rid, target in targets.items()}

    out["litellm"] = compile_litellm_substrate_config({"engine-a": records["engine-a"]})

    cmd = records["engine-cmd"]["lifecycle"]["driver_config"]
    out["command"] = {key: CommandDriver._command(cmd, key) for key in ("start", "stop", "status")}
    env = CommandDriver._env(cmd)
    out["command_env"] = {key: env[key] for key in cmd["env"]}

    proc = records["engine-proc"]
    svc = {**proc, "connection": {"address": "http://127.0.0.1:8192"}}
    driver = ProcessDriver()
    config, argv, port = driver._validated(svc)
    out["process"] = {"argv": argv, "port": port, "env": {key: ProcessDriver._environment(config)[key] for key in proc["process"]["env"]}}

    FakeClient.seen.clear()
    asyncio.run(ObservationReader(client_factory=FakeClient).read(svc, "process_ready"))
    out["probe"] = FakeClient.seen
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1]), indent=1, sort_keys=True, default=str))
