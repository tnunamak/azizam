"""Mounted proxy integration tests for paid remote Decisions providers.

These tests use the real FastAPI routes on ``proxy.app`` and the real provider
execution/reservation code.  Only the vendor HTTP boundary is mocked with
``httpx.MockTransport`` so the route wiring must prove policy admission,
provider cost reservation/settlement, concurrency, receipt ownership, and safe
projection together.
"""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from threading import Event
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from tests.support.access import plan_for, access  # noqa: E402
from gateway import admin_store, decision_provider_execution, media_jobs, providers, receipt_store
from gateway.runtime_auth import RuntimeIdentity

RAW_KEY = "opk_decisions_provider_integration"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_KEY}"}
BODY = {
    "model": "typesafe/jev-latest",
    "state": "Customer says payouts failed for three days.",
    "questions": {"is_urgent": {"type": "noul", "instructions": "Is this urgent?"}},
}
LOCAL_BODY = {**BODY, "model": "jev-latest"}
TYPESAFE_BODY = {
    "model": "jev-latest",
    "answers": {"is_urgent": {"type": "noul", "noul": 0.91}},
    "usage": {"input_tokens": 1000, "output_tokens": 0, "cost": "0.002"},
}


def _provider_registry(*, provider_id="typesafe", protocols=("typesafe-systemone",), concurrency_limit=2, models=None, price=None) -> dict:
    env_name = "OPENAI_TEST_KEY" if provider_id == "openai" else "TYPESAFE_TEST_KEY"
    base_url = "https://openai.test" if provider_id == "openai" else "https://api.typesafe.test"
    return {
        "services": {
            provider_id: {
                "id": provider_id,
                "kind": "provider",
                "enabled": True,
                "capabilities": list(protocols),
                "config": {
                    "provider_id": provider_id,
                    "base_url": base_url,
                    "api_key_env": env_name,
                    "credential_origin": "https://openai.test" if provider_id == "openai" else "https://api.typesafe.test",
                    "cost_class": "paid-remote",
                    "concurrency_limit": concurrency_limit,
                    "models": models if models is not None else [{"id": f"{provider_id}/jev-latest" if provider_id == "typesafe" else "gpt-5"}],
                    "price": price if price is not None else {"input_usd_per_million_tokens": "1.00"},
                },
            }
        }
    }


def _profile(*, providers=frozenset({"typesafe", "openai"}), max_usd_per_request=None, budget_usd_per_day=Decimal("10"), budget_usd_per_month=Decimal("100"), tags=None, models=None) -> "AccessPlan":
    # Capability tags are not access-plan parts; providers, listed models and
    # spending caps are.
    return plan_for(
        "paid-plan", models=list(models) if models is not None else None, paid=sorted(providers),
        usd_per_request=None if max_usd_per_request is None else str(max_usd_per_request),
        usd_per_day=None if budget_usd_per_day is None else str(budget_usd_per_day),
        usd_per_month=None if budget_usd_per_month is None else str(budget_usd_per_month),
    )


@contextmanager
def _authenticated(profile: "AccessPlan"):
    key_hash = proxy.hash_api_key(RAW_KEY)
    identity = RuntimeIdentity("provider-test", key_hash, app_id="app-123", accountable_person_id="usr-123", access=access(profile))

    def lookup(candidate_hash: str, **_kwargs):
        return identity if candidate_hash == key_hash else None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proxy, "runtime_identity_for_app_credential_hash", lookup)
        yield identity, mp


def _install_registry(mp: pytest.MonkeyPatch, tmp_path, registry: dict | None = None) -> None:
    admin_path = tmp_path / "admin.db"
    facts_path = tmp_path / "media_jobs.db"
    admin_store.init_db(admin_path)
    media_jobs.init_db(facts_path)
    mp.setenv("TYPESAFE_TEST_KEY", "ts_secret_test_key")
    mp.setenv("OPENAI_TEST_KEY", "oa_secret_test_key")
    mp.setattr(proxy, "ADMIN_DB_PATH", admin_path)
    mp.setattr(proxy.media_jobs, "DB_PATH", facts_path)
    if hasattr(proxy, "ADMIN_STORE"):
        mp.setattr(proxy.ADMIN_STORE, "path", admin_path, raising=False)
        mp.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry or _provider_registry())


def _ledger_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT provider, app_id, status, estimated_usd, actual_usd FROM provider_spend ORDER BY id").fetchall()


def _capture_receipts(mp: pytest.MonkeyPatch) -> list:
    records = []

    def record(receipt, *args, **kwargs):
        records.append(receipt)
        return True

    def begin(receipt, *args, **kwargs):
        records.append(receipt)
        return getattr(receipt, "request_id", "request")

    def finalize(receipt, *args, **kwargs):
        if receipt not in records:
            records.append(receipt)
        return True

    mp.setattr(proxy.receipt_store, "record", record)
    mp.setattr(proxy.receipt_store, "begin", begin)
    mp.setattr(proxy.receipt_store, "finalize", finalize)
    return records


@contextmanager
def _mock_typesafe_http(handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        # call_typesafe_systemone creates clients through gateway.providers.httpx.
        mp.setattr(providers.httpx, "AsyncClient", client_factory)
        yield


@contextmanager
def _mock_openai_http(handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        # OpenAI Decisions passthrough sends HTTP through gateway.providers.
        mp.setattr(providers.httpx, "AsyncClient", client_factory)
        yield


@patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
def test_remote_decision_models_are_listed_without_paid_http(tmp_path):
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        with _mock_typesafe_http(handler):
            client = TestClient(proxy.app)
            typesafe_response = client.get("/typesafe/v1/models", headers=AUTH_HEADERS)
            root_response = client.get("/v1/models", headers=AUTH_HEADERS)

    assert typesafe_response.status_code == 200, typesafe_response.text
    typesafe_models = typesafe_response.json()["models"]
    assert any(
        item["name"] == "typesafe/jev-latest"
        and item["capabilities"]["remote"] is True
        and item["capabilities"]["cost_class"] == "paid-remote"
        for item in typesafe_models
    )
    assert root_response.status_code == 200, root_response.text
    root_models = root_response.json()["data"]
    assert any(
        item["id"] == "typesafe/jev-latest"
        and item["capabilities"]["remote"] is True
        and item["capabilities"]["cost_class"] == "paid-remote"
        for item in root_models
    )
    assert called is False
    assert not (tmp_path / "provider_spend.db").exists()


def test_typesafe_prefixed_systemone_routes_to_paid_provider_and_records_owner_receipt(tmp_path):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content.decode())))
        return httpx.Response(200, json=TYPESAFE_BODY)

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        receipts = _capture_receipts(mp)
        with _mock_typesafe_http(handler):
            client = TestClient(proxy.app)
            responses = [client.post(path, json=BODY, headers=AUTH_HEADERS) for path in ("/v1/systemone", "/typesafe/v1/systemone")]

    assert [r.status_code for r in responses] == [200, 200]
    for response in responses:
        assert response.json()["model"] == "jev-latest"
        assert response.headers["x-azizam-remote"] == "1"
        assert response.headers["x-azizam-provider"] == "typesafe"
        assert Decimal(response.headers["x-azizam-cost-usd"]) == Decimal("0.002")
    assert [item[0] for item in seen] == ["https://api.typesafe.test/v1/systemone", "https://api.typesafe.test/v1/systemone"]
    assert all(auth == "Bearer ts_secret_test_key" for _url, auth, _body in seen)
    assert [body["model"] for _url, _auth, body in seen] == ["jev-latest", "jev-latest"]
    assert _ledger_rows(tmp_path / "provider_spend.db") == [
        ("typesafe", "app-123", "settled", "0.000643000", "0.002000000"),
        ("typesafe", "app-123", "settled", "0.000643000", "0.002000000"),
    ]
    assert len(receipts) == 2
    for receipt in receipts:
        internal = receipt.to_dict(audience="internal")
        assert internal["app_id"] == "app-123"
        assert internal["api_key_name"] == "provider-test"
        projected = receipt.to_dict(audience="client")
        assert projected["remote"] is True
        assert projected["cost"] == {"usd": "0.002000000", "source": "provider-reported", "cost_class": "paid-remote", "reconciliation_status": "settled"}
        assert "ts_secret_test_key" not in str(projected)
        assert "backend_secret" not in str(projected)


def test_typesafe_resolved_model_alias_price_persists_receipt_and_ledger(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content.decode())["model"] == "jev-latest"
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"is_urgent": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 302, "output_tokens": 37},
            },
        )

    registry = _provider_registry(
        models=[{"id": "typesafe/jev-latest", "upstream_model": "jev-latest"}],
        price={
            "models": {
                "typesafe/jev-latest": {
                    "input_usd_per_million_tokens": "0.042",
                    "output_usd_per_million_tokens": "0",
                }
            }
        },
    )
    registry["services"]["typesafe"]["config"]["preset_id"] = "typesafe"
    receipts_db = tmp_path / "receipts.db"
    receipt_store.init_db(receipts_db)
    real_record = receipt_store.record
    real_begin = receipt_store.begin
    real_finalize = receipt_store.finalize

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path, registry)
        mp.setattr(proxy.receipt_store, "record", lambda receipt, *args, **kwargs: real_record(receipt, *args, **{**kwargs, "path": receipts_db}))
        mp.setattr(proxy.receipt_store, "begin", lambda receipt, *args, **kwargs: real_begin(receipt, *args, **{**kwargs, "path": receipts_db}))
        mp.setattr(proxy.receipt_store, "finalize", lambda receipt, *args, **kwargs: real_finalize(receipt, *args, **{**kwargs, "path": receipts_db}))
        with _mock_typesafe_http(handler):
            client = TestClient(proxy.app)
            response = client.post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "jev-1.13.0"
    assert Decimal(response.headers["x-azizam-cost-usd"]) == Decimal("0.000012684")
    rows = _ledger_rows(tmp_path / "provider_spend.db")
    assert len(rows) == 1
    assert rows[0][0] == "typesafe"
    assert rows[0][2] == "settled"
    assert rows[0][4] == "0.000012684"
    stored = receipt_store.get(response.headers["x-azizam-request-id"], path=receipts_db)
    assert stored["resolved_model"] == "jev-1.13.0"
    assert stored["cost"]["usd"] == "0.000012684"
    assert stored["cost"]["source"] == "price-table"
    assert stored["cost"]["reconciliation_status"] == "settled"
    assert stored["metadata"]["decisions"]["cost_usd"] == "0.000012684"
    assert stored["metadata"]["decisions"]["usage"] == {"input_tokens": 302, "output_tokens": 37}


def test_bare_jev_alias_stays_local_and_never_uses_paid_provider(tmp_path):
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def local_dispatch(_auth, _profile, body, _request_id):
        assert body["model"] == "jev-latest"
        return proxy.decisions_api.DecisionDispatchResult(
            body={"model": "decider-local", "answers": {"is_urgent": {"type": "noul", "noul": 0.7}}, "usage": {"input_tokens": 1, "output_tokens": 0}},
            backend="decider-cpu",
            engine="cpu",
        )

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        mp.setattr(proxy.DECISION_RUNTIME, "dispatch", local_dispatch)
        with _mock_typesafe_http(handler):
            response = TestClient(proxy.app).post("/v1/systemone", json=LOCAL_BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    assert response.headers["x-azizam-remote"] == "0"
    assert called is False
    assert not (tmp_path / "provider_spend.db").exists()


def test_local_unavailable_does_not_implicitly_fallback_to_paid_provider(tmp_path):
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=TYPESAFE_BODY)

    async def local_unavailable(_auth, _profile, _body, _request_id):
        return proxy.decisions_api.DecisionDispatchResult(
            body={"error": {"message": "local unavailable", "type": "server_error", "code": "overloaded"}},
            status_code=529,
            backend="decider-cpu",
            headers={"retry-after": "5"},
        )

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        mp.setattr(proxy.DECISION_RUNTIME, "dispatch", local_unavailable)
        with _mock_typesafe_http(handler):
            response = TestClient(proxy.app).post("/v1/systemone", json=LOCAL_BODY, headers=AUTH_HEADERS)

    assert response.status_code == 529
    assert response.headers["retry-after"] == "5"
    assert called is False
    assert not (tmp_path / "provider_spend.db").exists()


@pytest.mark.parametrize(
    "profile, expected_status, expected_code",
    [
        (_profile(providers=frozenset()), 403, "remote_provider_not_allowed"),
        (_profile(max_usd_per_request=Decimal("0.000001")), 402, "budget_exceeded"),
        (_profile(models={"typesafe/other-model"}), 403, "remote_provider_not_allowed"),
    ],
)
def test_typesafe_paid_provider_admission_failures_are_safe_and_fail_closed(tmp_path, profile, expected_status, expected_code):
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=TYPESAFE_BODY)

    with _authenticated(profile) as (_identity, mp):
        _install_registry(mp, tmp_path)
        with _mock_typesafe_http(handler):
            response = TestClient(proxy.app).post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "ts_secret" not in response.text
    assert called is False


def test_typesafe_provider_unknown_actual_cost_is_projected_as_unknown_not_zero_and_receipt_is_safe(tmp_path):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {"is_urgent": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 1000, "output_tokens": 0, "unpriced_usage_units": 1},
                "echo": "ts_secret_test_key",
            },
        )

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        receipts = _capture_receipts(mp)
        with _mock_typesafe_http(handler):
            response = TestClient(proxy.app).post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    assert response.headers["x-azizam-cost-usd"] == "unknown"
    assert "ts_secret_test_key" not in response.text
    assert _ledger_rows(tmp_path / "provider_spend.db") == [("typesafe", "app-123", "needs_reconciliation", "0.000643000", None)]
    projected = receipts[-1].to_dict(audience="client")
    assert projected["cost"] == {"usd": None, "source": "unknown", "cost_class": "paid-remote", "reconciliation_status": "needs_reconciliation"}
    assert "ts_secret_test_key" not in str(projected)


def test_malformed_remote_usage_does_not_leak_into_failure_receipt_projection(tmp_path):
    leaked_values = ("private echoed text", "-9")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {"is_urgent": {"type": "noul", "noul": 0.91}},
                "usage": {
                    "input_tokens": "private echoed text",
                    "output_tokens": True,
                    "completion_tokens": -9,
                },
            },
        )

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path)
        receipts = _capture_receipts(mp)
        with _mock_typesafe_http(handler):
            response = TestClient(proxy.app).post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "decision_backend_error"
    assert receipts
    for audience in ("client", "admin"):
        projected = receipts[-1].to_dict(audience=audience)
        assert "usage" not in projected or projected["usage"] in ({}, None)
        decisions = projected.get("decisions")
        if decisions is not None:
            assert decisions.get("usage") in ({}, None)
        assert "input_tokens" not in str(projected.get("usage"))
        assert "output_tokens" not in str(projected.get("usage"))
        serialized = str(projected)
        for value in leaked_values:
            assert value not in serialized


def test_typesafe_provider_concurrency_blocks_third_request_until_first_settles(tmp_path):
    entered = []
    release = Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.append(json.loads(request.content.decode())["model"])
        if len(entered) <= 2:
            release.wait(timeout=5)
        return httpx.Response(200, json={**TYPESAFE_BODY, "usage": {"input_tokens": 1000, "output_tokens": 0, "cost": "0.001"}})

    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path, _provider_registry(concurrency_limit=2))
        with _mock_typesafe_http(handler):
            client = TestClient(proxy.app)
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(client.post, "/v1/systemone", json=BODY, headers=AUTH_HEADERS)
                second = pool.submit(client.post, "/v1/systemone", json=BODY, headers=AUTH_HEADERS)
                deadline = time.monotonic() + 5
                while len(entered) < 2 and time.monotonic() < deadline:
                    time.sleep(0.001)
                assert len(entered) == 2
                third = client.post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)
                release.set()
                first_response = first.result(timeout=5)
                second_response = second.result(timeout=5)
            retry = client.post("/v1/systemone", json=BODY, headers=AUTH_HEADERS)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "provider_concurrency_limited"
    assert retry.status_code == 200
    assert entered == ["jev-latest", "jev-latest", "jev-latest"]
    assert _ledger_rows(tmp_path / "provider_spend.db") == [
        ("typesafe", "app-123", "settled", "0.000643000", "0.001000000"),
        ("typesafe", "app-123", "settled", "0.000643000", "0.001000000"),
        ("typesafe", "app-123", "settled", "0.000643000", "0.001000000"),
    ]


@pytest.mark.parametrize("protocols", [("typesafe-systemone",), ("openai-compatible",)])
def test_openai_decisions_is_501_without_explicit_decisions_capability(tmp_path, protocols):
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    registry = _provider_registry(provider_id="openai", protocols=protocols, models=[{"id": "gpt-5"}])
    with _authenticated(_profile()) as (_identity, mp):
        _install_registry(mp, tmp_path, registry)
        with _mock_openai_http(handler):
            response = TestClient(proxy.app).post("/v1/decisions", content=b'{"model":"gpt-5","input":"future"}', headers={**AUTH_HEADERS, "content-type": "application/json"})

    assert response.status_code == 501
    assert response.json()["error"]["code"] in {"decisions_backend_unavailable", "decisions_not_configured"}
    assert called is False
    assert not (tmp_path / "provider_spend.db").exists()


def test_openai_decisions_forwards_opaque_bytes_when_explicit_capability_configured_and_records_receipt(tmp_path):
    calls = []
    begin_ids = []
    finalize_ids = []
    finalize_results = []
    legacy_record_ids = []
    in_flight = []
    receipts_db = tmp_path / "openai-receipts.db"
    receipt_store.init_db(receipts_db)

    real_begin = receipt_store.begin
    real_finalize = receipt_store.finalize
    real_record = receipt_store.record
    real_checkpoint = receipt_store.update_phases

    def begin(receipt, *args, **kwargs):
        begin_ids.append(receipt.request_id)
        return real_begin(receipt, *args, **{**kwargs, "path": receipts_db})

    def finalize(receipt, *args, **kwargs):
        finalize_ids.append(receipt.request_id)
        result = real_finalize(receipt, *args, **{**kwargs, "path": receipts_db})
        finalize_results.append(result)
        return result

    def record(receipt, *args, **kwargs):
        legacy_record_ids.append(receipt.request_id)
        return real_record(receipt, *args, **{**kwargs, "path": receipts_db})

    def checkpoint(receipt, *args, **kwargs):
        return real_checkpoint(receipt, *args, **{**kwargs, "path": receipts_db})

    payload = {
        "id": "dec_123",
        "object": "decision",
        "status": "completed",
        "usage": {"input_tokens": 1000, "output_tokens": 1, "cost": "0.004"},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((
            str(request.url), request.content,
            request.headers.get("authorization"),
        ))
        assert len(begin_ids) == 1
        accepted = receipt_store.get(begin_ids[0], path=receipts_db)
        assert accepted is not None
        assert accepted["status"] == "in_progress"
        assert accepted["app_id"] == "app-123"
        in_flight.append(accepted)
        return httpx.Response(200, json=payload)

    registry = _provider_registry(
        provider_id="openai",
        protocols=("openai-compatible", "decisions"),
        models=[{"id": "gpt-5"}],
    )
    raw = b'{"model":"gpt-5","input":"future","opaque":true}'
    profile = _profile(
        max_usd_per_request=None,
        budget_usd_per_day=None,
        budget_usd_per_month=None,
    )
    with _authenticated(profile) as (_identity, mp):
        _install_registry(mp, tmp_path, registry)
        mp.setattr(proxy.receipt_store, "begin", begin)
        mp.setattr(proxy.receipt_store, "finalize", finalize)
        mp.setattr(proxy.receipt_store, "record", record)
        mp.setattr(proxy.receipt_store, "update_phases", checkpoint)
        with _mock_openai_http(handler):
            response = TestClient(proxy.app).post(
                "/v1/decisions",
                content=raw,
                headers={**AUTH_HEADERS, "content-type": "application/json"},
            )

    assert response.status_code == 200, response.text
    assert response.json() == payload
    assert response.headers["x-azizam-remote"] == "1"
    assert response.headers["x-azizam-provider"] == "openai"
    assert response.headers["x-azizam-cost-usd"] == "0.004000000"
    assert calls == [
        ("https://openai.test/v1/decisions", raw, "Bearer oa_secret_test_key")
    ]

    request_id = response.headers["x-azizam-request-id"]
    assert begin_ids == [request_id]
    assert finalize_ids == [request_id]
    assert finalize_results == [True]
    assert legacy_record_ids == []
    assert len(in_flight) == 1
    assert receipt_store.count(receipts_db, outer_only=False) == 1

    stored = receipt_store.get(request_id, path=receipts_db)
    assert stored["status"] == "succeeded"
    assert stored["app_id"] == "app-123"
    assert stored["api_key_name"] == "provider-test"
    assert stored["requested_model"] == "gpt-5"
    assert stored["accepted_at_ms"] == in_flight[0]["accepted_at_ms"]
    assert stored["cost"]["usd"] == "0.004000000"
    assert stored["cost"]["reconciliation_status"] == "settled"
    assert _ledger_rows(tmp_path / "provider_spend.db") == [
        ("openai", "app-123", "settled", "0E-9", "0.004000000")
    ]
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        linked = conn.execute(
            "SELECT id, request_id, status, actual_usd FROM provider_spend"
        ).fetchall()
    assert linked == [
        (stored["cost"]["reservation_id"], request_id, "settled", "0.004000000")
    ]
    projected = receipt_store.get(
        request_id, path=receipts_db, audience="client"
    )
    assert projected["remote"] is True
    assert projected["cost"] == {
        "usd": "0.004000000",
        "source": "provider-reported",
        "cost_class": "paid-remote",
        "reconciliation_status": "settled",
    }
    assert "oa_secret_test_key" not in json.dumps(stored)
    assert "oa_secret_test_key" not in json.dumps(projected)
