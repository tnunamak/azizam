"""The owner HTTP surface of the transition gate, the generic declared-operation adapter, the reviewed profile
and the lease counts on the gate route (gateway/transition_admin.py, transition_adapter.py, transition_profiles.py)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from gateway import transition_profiles
from gateway.swap_drain import SwapDrain
from gateway.transition_admin import register_transition_routes
from gateway.transition_adapter import DeclaredOperationAdapter
from gateway.transition_gate import GateStore, ServicePaths, domain_violations
from gateway.transition_ops import StepIntent
from gateway.transition_runtime import TransitionRuntime

PROFILE = "declared-ops-v1"
FAST = {"admission_wait_s": 2, "drain_deadline_s": 1, "idle_poll_s": 0.01, "probe_deadline_s": 1, "release_deadline_s": 2,
        "load_deadline_s": 2, "query_deadline_s": 0.5, "lock_wait_s": 1, "fresh_within_s": 30, "retry_after_s": 7}


def services() -> dict[str, Any]:
    def record() -> dict[str, Any]:
        return {"residency": {"domain": "dom", "transition": {"enabled": True, "profile": PROFILE, **FAST}}}
    return {"alpha": record(), "beta": record(), "other": {}}


@dataclass
class Reply:
    status: int
    selected: Mapping[str, Any]


class FakeEngine:
    """Answers the profile's declared operations as an http-v1 reply would carry them."""

    def __init__(self) -> None:
        self.resident: Optional[str] = "alpha"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hold_release: Optional[asyncio.Event] = None
        self.release_outcome = "success"
        self.query_outcome = "unknown"
        self.status = 200

    async def __call__(self, service_id: str, op: str, body: Mapping[str, Any]) -> Reply:
        self.calls.append((op, dict(body)))
        if op == "transition.resident":
            return Reply(self.status, {"resident": self.resident is not None, "offering": self.resident,
                                       "ready": True, "incarnation": "inc-1"})
        if op == "transition.idle":
            return Reply(self.status, {"idle": True, "incarnation": "inc-1"})
        if op == "transition.release":
            if self.hold_release is not None:
                await self.hold_release.wait()
            if self.release_outcome == "success":
                self.resident = None
            return Reply(self.status, {"outcome": self.release_outcome, "incarnation": "inc-1", "attested": False})
        if op == "transition.load":
            self.resident = str(body["target"])
            return Reply(self.status, {"outcome": "success", "evidence_ref": "loaded"})
        if op == "transition.query":
            return Reply(self.status, {"outcome": self.query_outcome})
        raise AssertionError(op)


class Ctx:
    def __init__(self, tmp_path: Path, *, roles: tuple[str, ...] = ("owner",)) -> None:
        self.engine = FakeEngine()
        self.wall = [1_700_000_000.0]
        self.gates = GateStore(tmp_path / "transition.db")
        self.gates.load()
        self.drain = SwapDrain(wall_clock=lambda: self.wall[0])
        self.locks: dict[str, asyncio.Lock] = {}
        self.runtime = TransitionRuntime(
            gates=lambda: self.gates, services=services, lock_for=lambda d: self.locks.setdefault(d, asyncio.Lock()),
            drain=lambda: self.drain, protocol_invoke=self.engine)
        self.roles = roles
        router = APIRouter(prefix="/admin/api")

        def guard(request: Request) -> Any:
            if request.headers.get("x-test") == "anonymous":
                return JSONResponse({"error": {"code": "unauthenticated"}}, status_code=401)
            return SimpleNamespace(roles=self.roles)

        async def read_json(request: Request) -> Any:
            return await request.json()

        register_transition_routes(router, runtime=lambda: self.runtime, guard=guard, read_json=read_json)
        self.app = FastAPI()
        self.app.include_router(router)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")


BASE = "/admin/api/engines/domains"


async def settled(ctx: Ctx, client: httpx.AsyncClient, op_id: str, domain: str = "dom") -> dict[str, Any]:
    for _ in range(300):
        body = (await client.get(f"{BASE}/{domain}/transitions/{op_id}")).json()["operation"]
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("the operation did not settle")


def run(scenario: Callable[[Ctx, httpx.AsyncClient], Awaitable[None]], tmp_path: Path, **kwargs: Any) -> Ctx:
    ctx = Ctx(tmp_path, **kwargs)

    async def main() -> None:
        async with ctx.client() as client:
            await scenario(ctx, client)
            await asyncio.sleep(0)

    asyncio.run(main())
    return ctx


# -- the routes -----------------------------------------------------------------------------------------


def test_a_transition_is_accepted_runs_to_the_end_and_is_readable(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        started = await client.post(f"{BASE}/dom/transitions", json={"kind": "select", "target": "beta", "idempotency_key": "k1"})
        assert started.status_code == 202 and started.json()["gate"] in {"closed", "open"}
        op_id = started.json()["op_id"]
        done = await settled(ctx, client, op_id)
        assert done["state"] == "succeeded" and done["target"] == "beta"
        assert ctx.engine.resident == "beta"
        gate = await client.get(f"{BASE}/dom/gate")
        assert gate.status_code == 200 and gate.json()["gate"] == "open" and gate.json()["op_id"] is None
        same = await client.post(f"{BASE}/dom/transitions", json={"kind": "select", "target": "beta", "idempotency_key": "k1"})
        assert same.status_code == 202 and same.json()["op_id"] == op_id, "a retry under the same key is the same operation"
        clash = await client.post(f"{BASE}/dom/transitions", json={"kind": "select", "target": "alpha", "idempotency_key": "k1"})
        assert clash.status_code == 409 and clash.json()["op_id"] == op_id, "G4: another request under one key"

    run(scenario, tmp_path)


def test_a_second_transition_while_one_runs_is_409_with_the_running_id(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        ctx.engine.hold_release = asyncio.Event()
        first = await client.post(f"{BASE}/dom/transitions", json={"kind": "park", "idempotency_key": "a"})
        assert first.status_code == 202
        rival = await client.post(f"{BASE}/dom/transitions", json={"kind": "select", "target": "beta", "idempotency_key": "b"})
        assert rival.status_code == 409 and rival.json()["op_id"] == first.json()["op_id"] and rival.json()["state"] == "running"
        assert (await client.get(f"{BASE}/dom/gate")).json()["gate"] == "closed"
        ctx.engine.hold_release.set()
        assert (await settled(ctx, client, first.json()["op_id"]))["state"] == "succeeded"
        gate = (await client.get(f"{BASE}/dom/gate")).json()
        assert gate["gate"] == "open" and gate["parked"] == first.json()["op_id"]

    run(scenario, tmp_path)


def test_only_the_owner_signed_in_and_a_known_domain_may_use_the_routes(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        body = {"kind": "park", "idempotency_key": "a"}
        assert (await client.post(f"{BASE}/dom/transitions", json=body, headers={"x-test": "anonymous"})).status_code == 401
        assert (await client.post(f"{BASE}/dom/transitions", json=body)).status_code == 403
        assert (await client.get(f"{BASE}/dom/gate")).status_code == 403
        assert (await client.get(f"{BASE}/dom/transitions/x")).status_code == 403
        assert (await client.post(f"{BASE}/dom/transitions/x/resolve", json={})).status_code == 403
        assert ctx.engine.calls == [], "a refused request reaches no engine"

    run(scenario, tmp_path, roles=("administrator",))

    async def owner(ctx: Ctx, client: httpx.AsyncClient) -> None:
        for path in ("nothing", "other"):
            assert (await client.post(f"{BASE}/{path}/transitions", json={"kind": "park", "idempotency_key": "a"})).status_code == 404
            assert (await client.get(f"{BASE}/{path}/gate")).status_code == 404
        assert (await client.get(f"{BASE}/dom/transitions/missing")).status_code == 404

    (tmp_path / "second").mkdir()
    run(owner, tmp_path / "second")


def test_a_request_that_cannot_start_is_422(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        for body in ({"kind": "explode", "idempotency_key": "a"}, {"kind": "park"}, {"kind": "park", "idempotency_key": ""},
                     {"kind": "select", "idempotency_key": "b"}, {"kind": "wake", "idempotency_key": "c"},
                     {"kind": "park", "idempotency_key": "d", "target": 3}):
            response = await client.post(f"{BASE}/dom/transitions", json=body)
            assert response.status_code == 422, body
        assert (await client.get(f"{BASE}/dom/gate")).json()["gate"] == "open", "a refused request closes nothing"

    run(scenario, tmp_path)


def test_resolve_settles_a_held_step_for_the_owner_only(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        ctx.engine.release_outcome = "unknown"
        started = await client.post(f"{BASE}/dom/transitions", json={"kind": "park", "idempotency_key": "a"})
        op_id = started.json()["op_id"]
        held = await settled(ctx, client, op_id)
        assert held["state"] == "reconciling" and held["step"] == "release"
        assert (await client.get(f"{BASE}/dom/gate")).json()["gate"] == "reconciling"
        assert (await client.post(f"{BASE}/dom/transitions", json={"kind": "wake", "idempotency_key": "w"})).status_code in {409, 422}
        assert (await client.post(f"{BASE}/dom/transitions/{op_id}/resolve", json={"step": "load", "outcome": "success"})).status_code == 409
        assert (await client.post(f"{BASE}/dom/transitions/{op_id}/resolve", json={"step": "release", "outcome": "bogus"})).status_code == 422
        ctx.engine.resident = None
        done = await client.post(f"{BASE}/dom/transitions/{op_id}/resolve",
                                 json={"step": "release", "outcome": "failed_closed_by_owner"})
        assert done.status_code == 422
        resolved = await client.post(f"{BASE}/dom/transitions/{op_id}/resolve",
                                     json={"step": "release", "outcome": "partial", "evidence_ref": "operator looked"})
        assert resolved.status_code == 200 and resolved.json()["state"] == "failed_closed" and resolved.json()["gate"] == "failed_closed"
        again = await client.post(f"{BASE}/dom/transitions/{op_id}/resolve", json={"step": "release", "outcome": "success"})
        assert again.status_code == 409, "a finished operation has nothing to resolve"

    run(scenario, tmp_path)


# -- lease counts on the gate route ------------------------------------------------------------------------


def test_the_gate_route_reports_active_leases_and_the_last_mint(tmp_path: Path) -> None:
    async def scenario(ctx: Ctx, client: httpx.AsyncClient) -> None:
        before = (await client.get(f"{BASE}/dom/gate")).json()
        assert before["active_leases"] == 0 and before["last_lease_at"] is None
        first = ctx.drain.lease("dom", admit=False)
        ctx.wall[0] += 5
        second = ctx.drain.lease("dom", admit=False)
        other = ctx.drain.lease("elsewhere", admit=False)
        held = (await client.get(f"{BASE}/dom/gate")).json()
        assert held["active_leases"] == 2, "only this domain's leases count"
        assert held["last_lease_at"] == pytest.approx(1_700_000_005.0)
        first.release()
        assert (await client.get(f"{BASE}/dom/gate")).json()["active_leases"] == 1
        second.release()
        other.release()
        after = (await client.get(f"{BASE}/dom/gate")).json()
        assert after["active_leases"] == 0 and after["last_lease_at"] == pytest.approx(1_700_000_005.0), "a release keeps the last mint"

    run(scenario, tmp_path)


def test_swap_drain_remembers_the_last_mint_per_key() -> None:
    now = [100.0]
    drain = SwapDrain(wall_clock=lambda: now[0], key_for=lambda name: name.split("/")[0])
    assert drain.last_lease_at("a/x") is None and drain.in_flight("a/x") == 0
    lease = drain.lease("a/x")
    now[0] = 130.0
    other = drain.lease("a/y")
    assert (drain.in_flight("a/z"), drain.last_lease_at("a/z"), drain.last_lease_at("b/x")) == (2, 130.0, None)
    lease.release()
    other.release()
    assert drain.in_flight("a/x") == 0 and drain.last_lease_at("a/x") == 130.0


# -- the declared-operation adapter -------------------------------------------------------------------------


def adapter_over(engine: Callable[..., Awaitable[Any]], *, attested: bool = True) -> DeclaredOperationAdapter:
    profile = transition_profiles.get(PROFILE)
    assert profile is not None
    return DeclaredOperationAdapter(type(profile)(profile.id, profile.title, profile.operations, attested),
                                    lambda domain, op, body: engine(domain, op, body))


INTENT = StepIntent(step="release", effect_key="op:release", expected_incarnation="inc-1")


def test_attestation_comes_from_the_profile_never_from_the_reply() -> None:
    engine = FakeEngine()

    async def scenario() -> None:
        # The reply says attested; the adapter ignores it. The profile decides.
        assert (await adapter_over(engine, attested=True).release("dom", INTENT)).attested is True
        engine.resident = "alpha"
        assert (await adapter_over(engine, attested=False).release("dom", INTENT)).attested is False
        engine.release_outcome = "failed"
        failed = await adapter_over(engine, attested=True).release("dom", INTENT)
        assert (failed.outcome, failed.attested) == ("partial", False), "a declared failure is not a success"
        assert (await adapter_over(engine).load("dom", "beta", StepIntent("load", "op:load", None))).attested is False

    asyncio.run(scenario())
    assert engine.calls[0][1] == {"effect_key": "op:release", "expected_incarnation": "inc-1"}


def test_an_unreadable_reply_is_unknown_and_never_a_success() -> None:
    async def broken(domain: str, op: str, body: Mapping[str, Any]) -> Any:
        raise OSError("connection reset")

    engine = FakeEngine()

    async def scenario() -> None:
        assert (await adapter_over(broken).release("dom", INTENT)).outcome == "unknown"
        assert (await adapter_over(broken).resident("dom")).value is None
        engine.status = 500
        assert (await adapter_over(engine).release("dom", INTENT)).outcome == "unknown", "a non-2xx reply decides nothing"
        assert (await adapter_over(engine).idle("dom")).value is None
        engine.status = 200
        engine.release_outcome = "who-knows"
        assert (await adapter_over(engine).release("dom", INTENT)).outcome == "unknown", "an unlisted outcome is unknown"

    asyncio.run(scenario())


def test_evidence_carries_the_offering_readiness_and_incarnation() -> None:
    engine = FakeEngine()

    async def scenario() -> None:
        seen = await adapter_over(engine).resident("dom")
        assert (seen.value, seen.incarnation, dict(seen.detail)) == (True, "inc-1", {"offering": "alpha", "ready": True})
        engine.resident = None
        assert (await adapter_over(engine).resident("dom")).value is False

    asyncio.run(scenario())


# -- the reviewed profile and the owner's config ------------------------------------------------------------


def test_the_shipped_profile_loads_and_is_known() -> None:
    profile = transition_profiles.get(PROFILE)
    assert profile is not None and profile.attested is True
    assert set(profile.operations) == {"idle", "resident", "release", "load", "query"}
    assert transition_profiles.known(PROFILE) and not transition_profiles.known("not-reviewed")


def test_a_malformed_profile_is_skipped_not_used(tmp_path: Path) -> None:
    (tmp_path / "bad.yml").write_text("profile_version: 1\nid: bad\noperations: {resident: a}\n")
    (tmp_path / "worse.yml").write_text("- not a mapping\n")
    (tmp_path / "good.yml").write_text(
        "profile_version: 1\nid: good\noperations: {resident: a, release: b, load: c, query: d}\n")
    assert set(transition_profiles.load_profiles(tmp_path)) == {"good"}
    assert transition_profiles.load_profiles(tmp_path)["good"].attested is False, "attested defaults to false"


@pytest.mark.parametrize("key", ["attested", "failure_semantics", "operations", "verify", "residual", "idle_evidence"])
def test_an_owner_config_cannot_set_what_the_profile_holds(key: str) -> None:
    record = services()["alpha"]
    record["residency"]["transition"][key] = True
    errors = domain_violations({"alpha": record}, paths=lambda sid, rec: ServicePaths(leased=("hop",)),
                               profile_known=transition_profiles.known)
    assert "alpha" in errors and key in errors["alpha"]


def test_an_unknown_profile_is_refused_at_load() -> None:
    record = services()["alpha"]
    record["residency"]["transition"]["profile"] = "not-reviewed"
    errors = domain_violations({"alpha": record}, paths=lambda sid, rec: ServicePaths(leased=("hop",)),
                               profile_known=transition_profiles.known)
    assert "not a reviewed profile" in errors["alpha"]
