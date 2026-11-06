"""scripts/transition-proof against the real owner transition routes (gateway/transition_admin.py).

The harness's own tests use a fake gateway. These mount the real routes on a test app, over a fake engine, and serve it
on a loopback socket, so a path, a body, a status code or a header that differs from the routes shows here. The chat
route is a stand-in: it answers 503 + Retry-After with the gateway's real refusal builder while the gate is not open.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

import proxy
from gateway.swap_drain import SwapDrain
from gateway.transition_admin import register_transition_routes
from gateway.transition_gate import GateStore, TransitionUnavailable
from gateway.transition_runtime import TransitionRuntime
from scripts.transition_proof.runner import Proof
from tests.support.fake_upstream.loopback import LoopbackServer
from tests.test_transition_admin import FakeEngine, services
from tests.test_transition_proof import STANDARD, assertions, steps, write_cases
from tests.support.fake_upstream.transition_gateway import KEY
import subprocess
import sys
import time

DOMAIN = "dom"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transition-proof"


class CachingEngine(FakeEngine):
    """The fake engine, plus a prompt cache a release can drop."""

    def __init__(self) -> None:
        super().__init__()
        self.cache: list[list[Any]] = []
        self.release_drops_cache = False

    async def __call__(self, service_id: str, op: str, body: Any) -> Any:
        reply = await super().__call__(service_id, op, body)
        if op == "transition.release" and self.release_drops_cache:
            self.cache.clear()
        return reply


class RealRoutesGateway:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = CachingEngine()
        self.log: list[dict[str, Any]] = []
        self.gates = GateStore(tmp_path / "transition.db")
        self.gates.load()
        drain, locks = SwapDrain(), {}
        self.runtime = TransitionRuntime(
            gates=lambda: self.gates, services=services, lock_for=lambda d: locks.setdefault(d, asyncio.Lock()),
            drain=lambda: drain, protocol_invoke=self.engine)
        router = APIRouter(prefix="/admin/api")

        def guard(request: Request) -> Any:
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return JSONResponse({"error": {"code": "unauthenticated"}}, status_code=401)
            return SimpleNamespace(roles=("owner",))

        async def read_json(request: Request) -> Any:
            return await request.json()

        register_transition_routes(router, runtime=lambda: self.runtime, guard=guard, read_json=read_json)
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def record(request: Request, call_next: Any) -> Any:
            response = await call_next(request)
            self.log.append({"method": request.method, "path": request.url.path, "status": response.status_code,
                             "retry_after": response.headers.get("retry-after")})
            return response

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> Any:
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return JSONResponse({"error": "bad key"}, status_code=401)
            body = await request.json()
            if self.gates.parked(DOMAIN) is not None and self.gates.state(DOMAIN) == "open":
                executor = self.runtime.executor()  # wake on request, as the gateway does
                began = await executor.submit(domain=DOMAIN, kind="wake", idempotency_key="wake-auto")
                await executor.wait(began.operation.op_id)
            state = self.gates.state(DOMAIN)
            if state != "open":
                return proxy._transition_unavailable_response(
                    TransitionUnavailable(DOMAIN, reason=f"gate_{state}", retry_after_s=7))
            messages = body["messages"]
            tokens = lambda seen: sum(len(str(m["content"])) for m in seen) // 4  # noqa: E731
            cached = max((tokens(seen) for seen in self.engine.cache if messages[:len(seen)] == seen), default=0)
            self.engine.cache.append(messages)
            await asyncio.sleep(0.01 if cached else 0.2)
            return {"choices": [{"message": {"role": "assistant", "content": "answer"}}],
                    "usage": {"prompt_tokens": tokens(messages), "completion_tokens": 3,
                              "prompt_tokens_details": {"cached_tokens": cached}}}

        self.server = LoopbackServer(app)

    def run_on_server_loop(self, call: Any) -> None:
        self.server._loop.call_soon_threadsafe(call)  # type: ignore[union-attr]


@pytest.fixture
def real(tmp_path: Path) -> Iterator[RealRoutesGateway]:
    gateway = RealRoutesGateway(tmp_path)
    gateway.server.start()
    try:
        yield gateway
    finally:
        gateway.server.stop()


def proof(real: RealRoutesGateway, tmp_path: Path, *extra: str, manifest: Path | None = None):
    key_file = tmp_path / "owner.key"
    key_file.write_text(KEY + "\n")
    out = tmp_path / "result.json"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", real.server.base_url, "--domain", DOMAIN, "--model", "alias",
         "--cases", str(manifest or write_cases(tmp_path, {**STANDARD, "max_timing_ratio": 0.5})),
         "--owner-key-file", str(key_file), "--out", str(out), "--poll-interval-s", "0.02", "--poll-timeout-s", "5",
         "--request-timeout-s", "10", "--run-id", "t", *extra],
        capture_output=True, text=True, check=False, timeout=60)
    return process, json.loads(out.read_text()) if out.exists() else {}


def transitions(real: RealRoutesGateway) -> list[tuple[str, str, int]]:
    return [(e["method"], e["path"], e["status"]) for e in real.log if "/transitions" in e["path"] or e["path"].endswith("/gate")]


def test_the_harness_passes_against_the_real_routes_in_post_mode(real: RealRoutesGateway, tmp_path: Path) -> None:
    process, result = proof(real, tmp_path)
    assert process.returncode == 0, process.stdout + process.stderr
    assert result["status"] == "passed" and result["assertions_failed"] == 0
    assert set(assertions(result).values()) == {True} and "gate_open_after_wake" in assertions(result)
    base = f"/admin/api/engines/domains/{DOMAIN}"
    log = transitions(real)
    assert ("POST", f"{base}/transitions", 202) in log and ("GET", f"{base}/gate", 200) in log
    posted = [e for e in real.log if e["method"] == "POST" and e["path"] == f"{base}/transitions"]
    assert len(posted) == 2 and all(e["status"] == 202 for e in posted)
    assert any(m == "GET" and p.startswith(f"{base}/transitions/") and s == 200 for m, p, s in log)
    assert steps(result)["park"]["states"][-1] == "succeeded" and steps(result)["wake"]["final_state"] == "succeeded"
    assert [t["gate"] for t in result["cases"][0]["gate_trace"]] == ["open"] * 4
    assert [c[0] for c in real.engine.calls if c[0] in ("transition.release", "transition.load")] == \
        ["transition.release", "transition.load"]


def test_request_mode_wakes_through_the_real_executor(real: RealRoutesGateway, tmp_path: Path) -> None:
    process, result = proof(real, tmp_path, "--wake-mode", "request")
    assert process.returncode == 0, process.stdout + process.stderr
    posted = [e for e in real.log if e["method"] == "POST" and e["path"].endswith("/transitions")]
    assert len(posted) == 1, "only the park is posted; the follow-up woke the domain"
    store = real.runtime.executor().store
    assert store.by_idempotency_key(DOMAIN, "proof-t-park").kind == "park"
    assert store.by_idempotency_key(DOMAIN, "wake-auto").kind == "wake"


def test_a_cache_the_release_dropped_fails_the_cache_assertion_only(real: RealRoutesGateway, tmp_path: Path) -> None:
    real.engine.release_drops_cache = True
    process, result = proof(real, tmp_path)
    assert process.returncode == 1
    assert {n for n, ok in assertions(result).items() if not ok} == {"cached_prefix_kept", "followup_faster_than_cold"}


def test_a_held_domain_is_a_503_with_retry_after_and_the_harness_reports_it(real: RealRoutesGateway, tmp_path: Path) -> None:
    real.gates.force(DOMAIN, "reconciling")
    process, result = proof(real, tmp_path)
    assert process.returncode == 1
    seed = steps(result)["seed"]
    assert seed["http_status"] == 503 and assertions(result) == {"seed_replied": False}
    refused = [e for e in real.log if e["path"] == "/v1/chat/completions"]
    assert refused[0]["status"] == 503 and refused[0]["retry_after"] == "7"


def test_a_409_from_the_real_routes_names_the_running_operation(real: RealRoutesGateway) -> None:
    hold = asyncio.Event()
    real.engine.hold_release = hold
    headers = {"Authorization": f"Bearer {KEY}"}
    with httpx.Client(headers=headers, timeout=10, base_url=real.server.base_url) as client:
        base = f"/admin/api/engines/domains/{DOMAIN}/transitions"
        first = client.post(base, json={"kind": "park", "idempotency_key": "other"})
        assert first.status_code == 202 and set(first.json()) == {"op_id", "state", "gate"}
        step = Proof(client, domain=DOMAIN, model="alias", wake_mode="post", run_id="t", poll_interval_s=0.02,
                     poll_timeout_s=1)._transition("park")
        assert step["http_status"] == 409 and first.json()["op_id"] in step["error"] and step["ok"] is False
        real.run_on_server_loop(hold.set)
        for _ in range(500):  # let the held park finish before the server stops
            if client.get(f"/admin/api/engines/domains/{DOMAIN}/gate").json()["gate"] == "open":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("the held park did not finish")
