"""Paid decision provider over the typesafe-systemone wire: the money path, normal and faulted.

``decision_provider_execution.systemone`` reserves spend, calls ``POST /v1/systemone``
and settles or flags the reservation. Each case serves one FK scenario
(tests/support/fake_upstream/scenarios/typesafe-systemone/) and asserts the
result the caller gets, the forwarded request and the provider_spend ledger row.
The provider is not streamed: the "split event" fault is the JSON answer
arriving in pieces, cut inside a multibyte character.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import decision_provider_execution as execution
from tests.support.access import plan_for  # noqa: E402
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, load_scenario, module_deadline  # noqa: F401

MODEL = "typesafe/jev-latest"
API_KEY = "ts-fk-key"
BODY = {"model": MODEL, "state": "x", "questions": {"q": {"type": "noul", "instructions": "ok?"}}}
HANG_BOUND_SECONDS = 10
PROFILE = plan_for("paid-decisions", paid=sorted({"typesafe"}))
AUTH = SimpleNamespace(identity=SimpleNamespace(app_id="app_1", name="Decision App", delegated=False))


def _registry(base_url: str) -> dict:
    return {"services": {"typesafe": {
        "id": "typesafe", "kind": "provider", "enabled": True, "capabilities": ["typesafe-systemone"],
        "config": {
            "provider_id": "typesafe", "base_url": base_url, "api_key_env": "TYPESAFE_FK_KEY",
            "credential_origin": base_url, "cost_class": "paid-remote",
            "price": {"input_usd_per_million_tokens": "1.00"},
            "models": [{"id": MODEL}], "default_models": [MODEL],
        },
    }}}


def _serve(fakes: FakeUpstreams, monkeypatch, name: str, *, wire: bool = False) -> tuple[FakeUpstream, dict]:
    monkeypatch.setenv("TYPESAFE_FK_KEY", API_KEY)
    scenario = load_scenario(f"typesafe-systemone/{name}", model="jev-latest", api_key=API_KEY)
    if wire:
        fake = fakes.loopback(scenario)
    else:
        fake = fakes.asgi(scenario, host="typesafe.fk.test")
        fake.base_url = "https://typesafe.fk.test"  # the registry names an https origin; the router matches the host
    return fake, _registry(fake.base_url)


def _decide(registry: dict, ledger: Path, fakes: FakeUpstreams | None = None, *, release_waits: int = 0):
    async def pump() -> None:
        for _ in range(release_waits):
            await fakes.clock.until_waiting()
            fakes.clock.advance(1)

    async def run():
        async with asyncio.timeout(HANG_BOUND_SECONDS):
            releaser = asyncio.create_task(pump())
            result = await execution.systemone(AUTH, PROFILE, dict(BODY), "req-fk", registry, ledger)
            await releaser
            return result

    return asyncio.run(run())


def _spend(ledger: Path) -> list[tuple]:
    with sqlite3.connect(ledger) as conn:
        return conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()


def _forwarded(fake: FakeUpstream) -> None:
    [call] = fake.calls("decide")
    assert call.headers["authorization"] == f"Bearer {API_KEY}"
    assert call.json["model"] == "jev-latest" and call.json["questions"] == BODY["questions"]


@pytest.mark.parametrize("name, wire, waits", [("answer", False, 0), ("answer-split", True, 1)], ids=["asgi", "split-loopback"])
def test_answer_settles_the_reserved_spend_at_the_reported_cost(fake_upstreams, monkeypatch, tmp_path, name, wire, waits) -> None:
    fake, registry = _serve(fake_upstreams, monkeypatch, name, wire=wire)
    ledger = tmp_path / "ledger.db"

    result = _decide(registry, ledger, fake_upstreams, release_waits=waits)

    assert result.status_code == 200
    assert result.body["answers"] == {"q": {"type": "noul", "noul": 1.0}}
    assert result.body.get("note", "café") == "café"
    assert (result.cost_usd, result.cost_source) == ("0.000123000", "provider-reported")
    assert _spend(ledger) == [("settled", "0.000123000")]
    _forwarded(fake)


def test_rate_limit_keeps_retry_after_and_releases_the_reservation(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake, registry = _serve(fake_upstreams, monkeypatch, "rate-limited")
    ledger = tmp_path / "ledger.db"

    result = _decide(registry, ledger)

    assert result.status_code == 429
    assert result.body["error"]["code"] == "provider_rate_limited"
    assert result.headers["retry-after"] == "2"
    assert [row[0] for row in _spend(ledger)] == ["released"]
    _forwarded(fake)


_DEADLINE_BODY = {"error": {"message": "Remote decision provider timed out.", "type": "invalid_request_error", "code": "deadline_exceeded"}}


def test_first_byte_past_the_deadline_gives_a_504_and_leaves_the_charge_for_reconciliation(fake_upstreams, monkeypatch, tmp_path) -> None:
    """The decision deadline passes once the fake has logged the request and holds the response."""
    fake, registry = _serve(fake_upstreams, monkeypatch, "slow-first-byte", wire=True)
    deadline = module_deadline(monkeypatch, execution, fake_upstreams.clock)
    ledger = tmp_path / "ledger.db"

    result = _decide(registry, ledger)

    assert deadline.timeout_fired == 1
    assert result.status_code == 504
    assert result.body == _DEADLINE_BODY
    assert [row[0] for row in _spend(ledger)] == ["needs_reconciliation"]
    _forwarded(fake)


def test_decision_deadline_wiring_on_the_real_clock_gives_a_504(fake_upstreams, monkeypatch, tmp_path) -> None:
    """The one real-clock case: the module's own deadline, shortened.

    The deadline may pass before the fake logged the request, so only the
    envelope and the ledger state are asserted.
    """
    fake, registry = _serve(fake_upstreams, monkeypatch, "slow-first-byte", wire=True)
    fake.waive("decide")
    monkeypatch.setattr(execution, "DEFAULT_DECISION_TIMEOUT_S", 0.2)
    ledger = tmp_path / "ledger.db"

    result = _decide(registry, ledger)

    assert result.status_code == 504
    assert result.body == _DEADLINE_BODY
    assert [row[0] for row in _spend(ledger)] == ["needs_reconciliation"]


def test_answer_cut_by_the_upstream_is_a_502_and_leaves_the_charge_for_reconciliation(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake, registry = _serve(fake_upstreams, monkeypatch, "answer-disconnect", wire=True)
    ledger = tmp_path / "ledger.db"

    result = _decide(registry, ledger)

    assert result.status_code == 502
    assert result.body == {"error": {"message": "remote provider request failed", "type": "invalid_request_error", "code": "provider_error"}}
    assert [row[0] for row in _spend(ledger)] == ["needs_reconciliation"]
    _forwarded(fake)
