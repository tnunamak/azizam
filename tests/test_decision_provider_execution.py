from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.requests import Request

from gateway import admin_store, decision_provider_execution as execution
from gateway import receipt_store
from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy


def _profile(*, providers=frozenset({"typesafe", "openai"}), max_request=Decimal("1.00"), budget=Decimal("5.00"), monthly=Decimal("20.00")):
    return DEFAULT_PROFILE.with_overrides(
        id="paid-decisions",
        remote=RemoteProviderPolicy(
            providers=frozenset(providers),
            max_usd_per_request=max_request,
            budget_usd_per_day=budget,
            budget_usd_per_month=monthly,
        ),
    )


def _auth(app_id="app_1", *, delegated=False):
    return SimpleNamespace(identity=SimpleNamespace(app_id=app_id, name="Decision App", delegated=delegated))


def _registry(*, provider_id="typesafe", protocols=("typesafe-systemone",), models=None, price=None, api_key_env=None, enabled=True):
    config = {
        "provider_id": provider_id,
        "base_url": f"https://{provider_id}.test",
        "api_key_env": api_key_env or f"{provider_id.upper()}_TEST_KEY",
        "cost_class": "paid-remote",
        "price": price if price is not None else {"input_usd_per_million_tokens": "1.00"},
    }
    if models is not None:
        config["models"] = models
    elif "typesafe-systemone" in protocols:
        config["default_models"] = [f"{provider_id}/jev-latest"]
    return {
        "services": {
            provider_id: {
                "id": provider_id,
                "kind": "provider",
                "enabled": enabled,
                "capabilities": list(protocols),
                "config": config,
            }
        }
    }


def _request(payload: dict, *, path="/v1/decisions") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": path, "headers": [], "scheme": "http", "server": ("test", 80)}, receive)
    request.state.request_id = "req-openai"
    return request


async def _response_body(response):
    if hasattr(response, "body"):
        return response.body
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


class DecisionProviderExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.db"
        self.receipts = []
        self.env = patch.dict(os.environ, {"TYPESAFE_TEST_KEY": "ts-key", "OPENAI_TEST_KEY": "oa-key"})
        self.begin = patch.object(receipt_store, "begin", lambda _receipt: None)
        self.finalize = patch.object(receipt_store, "finalize", side_effect=lambda receipt: self.receipts.append(receipt) or True)
        self.env.start(); self.begin.start(); self.finalize.start()

    def tearDown(self) -> None:
        self.finalize.stop(); self.begin.stop(); self.env.stop(); self.tmp.cleanup()

    async def test_typesafe_systemone_uses_paid_admission_and_settles_actual_cost(self) -> None:
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content.decode())
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "model": "jev-latest",
                    "answers": {"q": {"type": "noul", "noul": 1.0}},
                    "usage": {"input_tokens": 1000, "output_tokens": 0, "cost": "0.000123"},
                },
            )

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        registry = _registry(models=[{"id": "typesafe/jev-latest"}])
        with patch.object(execution.providers.httpx, "AsyncClient", client_factory):
            result = await execution.systemone(
                _auth(), _profile(), {"model": "typesafe/jev-latest", "state": "x", "questions": {"q": {"type": "noul", "instructions": "ok?"}}},
                "req-ts", registry, self.ledger,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.cost_usd, "0.000123000")
        self.assertEqual(result.cost_source, "provider-reported")
        self.assertEqual(seen["body"]["model"], "jev-latest")
        self.assertEqual(seen["authorization"], "Bearer ts-key")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("settled", "0.000123000")])
        self.assertEqual(self.receipts, [])

    async def test_typesafe_provider_error_returns_dispatch_result_not_exception(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            result = await execution.systemone(
                _auth(), _profile(), {"model": "typesafe/jev-latest", "state": "x", "questions": {}},
                "req-rate", _registry(), self.ledger,
            )

        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.body["error"]["code"], "provider_rate_limited")
        self.assertEqual(result.headers["retry-after"], "2")
        self.assertEqual(result.backend, "typesafe")


    async def test_custom_systemone_provider_id_dispatches_by_protocol_and_model_prefix(self) -> None:
        seen = {}

        async def fake_call(provider, _profile, body, **_kwargs):
            seen["provider"] = provider.id
            seen["model"] = body["model"]
            reservation = execution.providers.Reservation(
                123,
                provider.id,
                "paid-decisions",
                Decimal("0"),
                "test-estimate",
            )
            return {"model": "acme/decider", "answers": {}, "usage": {}}, execution.providers.Cost(None, "unknown", provider.id), execution.providers.Usage(), reservation

        registry = _registry(provider_id="acme-decisions", models=[{"id": "acme-decisions/decider"}])
        with patch.object(execution.providers, "call_typesafe_systemone", fake_call):
            result = await execution.systemone(
                _auth(), _profile(providers=frozenset({"acme-decisions"}), max_request=None, budget=None, monthly=None),
                {"model": "acme-decisions/decider", "state": "x", "questions": {}},
                "req-custom", registry, self.ledger,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.backend, "acme-decisions")
        self.assertEqual(result.reservation_id, 123)
        self.assertEqual(result.reconciliation_status, "needs_reconciliation")
        self.assertEqual(seen, {"provider": "acme-decisions", "model": "acme-decisions/decider"})

    def test_catalog_models_filters_disabled_rows_and_prefixes_by_provider_data(self) -> None:
        provider = execution.providers.provider_lookup(
            _registry(
                provider_id="acme-decisions",
                models=[
                    {"id": "decider"},
                    {"id": "acme-decisions/hidden", "enabled": False},
                    {"id": "org/model"},
                    {"model_group": "acme-decisions/explicit"},
                ],
            ),
            "acme-decisions",
        )
        assert provider is not None

        self.assertEqual(
            execution.catalog_models(provider),
            ["acme-decisions/decider", "acme-decisions/org/model", "acme-decisions/explicit"],
        )
        self.assertFalse(execution._model_declared(provider, "acme-decisions/hidden"))
        self.assertTrue(execution._model_declared(provider, "acme-decisions/org/model"))

    def test_model_declaration_does_not_allow_disabled_suffix_collision(self) -> None:
        provider = execution.providers.provider_lookup(
            _registry(
                provider_id="acme",
                models=[
                    {"id": "acme/foo", "enabled": False},
                    {"id": "other/foo"},
                ],
            ),
            "acme",
        )
        assert provider is not None

        self.assertEqual(execution.catalog_models(provider), ["acme/other/foo"])
        self.assertFalse(execution._model_declared(provider, "acme/foo"))
        self.assertTrue(execution._model_declared(provider, "acme/other/foo"))

    async def test_unprefixed_jev_model_stays_local_by_returning_not_configured_without_provider_call(self) -> None:
        called = False

        async def fake_call(*_args, **_kwargs):
            nonlocal called
            called = True

        with patch.object(execution.providers, "call_typesafe_systemone", fake_call):
            result = await execution.systemone(_auth(), _profile(), {"model": "jev-latest"}, "req-local", _registry(), self.ledger)

        self.assertEqual(result.status_code, 501)
        self.assertEqual(result.body["error"]["code"], "provider_not_configured")
        self.assertFalse(called)
        self.assertFalse(self.ledger.exists())

    async def test_real_app_identity_required_before_paid_provider_call(self) -> None:
        result = await execution.systemone(SimpleNamespace(identity=SimpleNamespace(name="legacy")), _profile(), {"model": "typesafe/jev-latest"}, "req-auth", _registry(), self.ledger)

        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.body["error"]["code"], "remote_provider_not_allowed")
        self.assertFalse(self.ledger.exists())


    def test_catalog_models_handles_mapping_and_upstream_model_conventions(self) -> None:
        provider = execution.providers.provider_lookup(
            _registry(
                models={
                    "alias": {"upstream_model": "jev-pro"},
                    "typesafe/jev-latest": {},
                }
            ),
            "typesafe",
        )
        assert provider is not None

        self.assertEqual(execution.catalog_models(provider), ["typesafe/jev-pro", "typesafe/jev-latest"])

    def test_capability_for_marks_remote_paid_decision(self) -> None:
        provider = execution.providers.provider_lookup(_registry(), "typesafe")
        assert provider is not None
        cap = execution.capability_for(provider, "typesafe/jev-latest")

        self.assertEqual(cap.intent, "decision.systemone")
        self.assertEqual(cap.backend, "typesafe")
        self.assertEqual(cap.model, "typesafe/jev-latest")
        self.assertIn("paid-remote", cap.tags)
        self.assertEqual(cap.cost_class, "paid-remote")

    async def test_openai_decisions_returns_501_without_configured_decisions_provider(self) -> None:
        response = await execution.openai(_request({"model": "gpt-5"}), _auth(), _profile(), {"services": {}}, self.ledger)
        body = json.loads(await _response_body(response))

        self.assertEqual(response.status_code, 501)
        self.assertEqual(body["error"]["code"], "decisions_backend_unavailable")
        self.assertFalse(self.ledger.exists())

    async def test_openai_decisions_configured_provider_forwards_and_settles(self) -> None:
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": "dec_1", "usage": {"input_tokens": 1000, "output_tokens": 1, "cost": "0.000222"}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(_request({"model": "gpt-5", "input": "hi"}), _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger)

        body = json.loads(await _response_body(response))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["id"], "dec_1")
        self.assertEqual(seen["url"], "https://openai.test/v1/decisions")
        self.assertEqual(seen["body"]["input"], "hi")
        self.assertEqual(response.headers["x-gateway-cost-usd"], "0.000222000")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("settled", "0.000222000")])



    async def test_empty_typesafe_provider_model_is_rejected_before_admission(self) -> None:
        result = await execution.systemone(_auth(), _profile(), {"model": "typesafe/"}, "req-empty", _registry(), self.ledger)

        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.body["error"]["code"], "invalid_model")
        self.assertFalse(self.ledger.exists())

    async def test_typesafe_success_without_actual_cost_marks_reconciliation_and_cost_unknown(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "jev-remote", "answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            result = await execution.systemone(
                _auth(), _profile(max_request=None, budget=None, monthly=None),
                {"model": "typesafe/jev-latest", "state": "x", "questions": {}},
                "req-unknown", _registry(), self.ledger,
            )

        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.cost_usd)
        self.assertEqual(result.cost_source, "unknown")
        self.assertEqual(result.resolved_model, "jev-remote")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])

    async def test_typesafe_cancellation_before_headers_cancels_vendor_and_marks_reconciliation(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            task = asyncio.create_task(execution.systemone(
                _auth(), _profile(),
                {"model": "typesafe/jev-latest", "state": "x", "questions": {}},
                "req-cancel", _registry(), self.ledger,
            ))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertTrue(cancelled.is_set())
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])

    async def test_openai_cancellation_before_headers_marks_receipt_cancelled_and_marks_reconciliation(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            task = asyncio.create_task(execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            ))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.receipts), 1)
        self.assertEqual(self.receipts[0].status, "cancelled")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])


    async def test_openai_success_with_unknown_actual_cost_emits_unknown_header_and_marks_reconciliation(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "dec_unknown", "usage": {"input_tokens": 1, "output_tokens": 0}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-cost-usd"], "unknown")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])


    async def test_openai_opaque_hook_forwards_403_body_and_status_without_rewriting(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "not_enabled", "message": "disabled"}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            )

        body = json.loads(await _response_body(response))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(body, {"error": {"code": "not_enabled", "message": "disabled"}})
        self.assertEqual(len(self.receipts), 1)
        self.assertEqual(self.receipts[0].status, "failed")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])

    async def test_openai_opaque_hook_forwards_429_retry_after_and_releases_unknown_reservation(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"code": "rate_limit"}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            )

        body = json.loads(await _response_body(response))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(body, {"error": {"code": "rate_limit"}})
        self.assertEqual(response.headers["retry-after"], "7")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("released", None)])


    async def test_openai_opaque_usage_tokens_do_not_invoke_litellm_pricing_for_known_model(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "dec_unknown", "usage": {"input_tokens": 1, "output_tokens": 0}})

        def fail_pricing(*_args, **_kwargs):
            raise AssertionError("opaque OpenAI Decisions must not infer chat pricing from usage tokens")

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))), \
             patch.object(execution.providers, "_litellm_full_usage_cost", fail_pricing):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-cost-usd"], "unknown")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("needs_reconciliation", None)])

    async def test_openai_opaque_trusted_cost_header_settles_without_usage_pricing(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"x-litellm-response-cost": "0.000333"}, json={"id": "dec_header", "usage": {"input_tokens": 1}})

        def fail_pricing(*_args, **_kwargs):
            raise AssertionError("trusted header cost should not need usage pricing")

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))), \
             patch.object(execution.providers, "_litellm_full_usage_cost", fail_pricing):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-cost-usd"], "0.000333000")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("settled", "0.000333000")])

    async def test_openai_provider_hook_redacts_key_in_array_json_and_plain_text_but_preserves_safe_bytes(self) -> None:
        bodies = [
            (b'["oa-key"]', b'["<redacted>"]', {}),
            (b'["oa-' + b'\\u006b' + b'ey"]', b'["<redacted>"]', {}),
            (b'"oa-' + b'\\u006b' + b'ey"', b'"<redacted>"', {}),
            (b'{"oa-' + b'\\u006b' + b'ey":"value"}', b'{"<redacted>":"value"}', {"<redacted>": "value"}),
            (b'not json oa-key', b'not json <redacted>', {}),
            (b'{  "safe" : "kept" }', b'{  "safe" : "kept" }', {"safe": "kept"}),
            (b'plain safe bytes', b'plain safe bytes', {}),
        ]

        for raw_body, expected_content, expected_payload in bodies:
            async def handler(_request: httpx.Request, raw_body=raw_body) -> httpx.Response:
                return httpx.Response(200, content=raw_body, headers={"content-type": "application/json"})

            real_client = httpx.AsyncClient
            transport = httpx.MockTransport(handler)
            provider = execution.providers.ProviderRef(
                id="openai",
                base_url="https://openai.test",
                protocols=("openai-compatible",),
                api_key_env="OPENAI_TEST_KEY",
                cost_class="paid-remote",
                price={},
                config={},
            )
            async with real_client(transport=transport, timeout=1) as client:
                payload, _cost, _usage, _headers, content, status = await execution.providers.call_openai_json(
                    provider, "/v1/decisions", {}, client=client
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload, expected_payload)
            self.assertEqual(content, expected_content)
            self.assertNotIn(b"oa-key", content)

    async def test_openai_provider_hook_normalizes_v1_base_preserves_raw_body_and_redacts_echoed_key(self) -> None:
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["content"] = request.content
            return httpx.Response(
                200,
                json={"debug": "oa-key", "usage": {"input_tokens": 1, "output_tokens": 0, "cost": "0.000001"}},
            )

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        provider = execution.providers.ProviderRef(
            id="openai",
            base_url="https://openai.test/v1",
            protocols=("openai-compatible",),
            api_key_env="OPENAI_TEST_KEY",
            cost_class="paid-remote",
            price={},
            config={},
        )
        raw = b'{"model":"gpt-5", "input":"spacing kept"}'

        async with real_client(transport=transport, timeout=1) as client:
            payload, cost, usage, _headers, content, status = await execution.providers.call_openai_json(
                provider,
                "/v1/decisions",
                {"model": "gpt-5", "input": "spacing kept"},
                content=raw,
                client=client,
            )

        self.assertEqual(seen["url"], "https://openai.test/v1/decisions")
        self.assertEqual(seen["content"], raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["debug"], "<redacted>")
        self.assertNotIn(b"oa-key", content)
        self.assertEqual(json.loads(content)["debug"], "<redacted>")
        self.assertEqual(str(cost.usd), "0.000001000")
        self.assertEqual(usage.input_tokens, 1)


    async def test_openai_decisions_uses_encrypted_provider_key(self) -> None:
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "dec_1", "usage": {"input_tokens": 1, "output_tokens": 0, "cost": "0.000001"}})

        admin_db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(path=admin_db)
        with patch.object(admin_store, "DB_PATH", admin_db):
            execution.providers.secret_store.put_provider_secret(
                "provider:openai:api_key",
                "oa-stored-key",
                db_path=admin_db,
                approved_origin="https://openai.test/v1",
            )
        registry = _registry(
            provider_id="openai",
            protocols=("openai-compatible", "decisions"),
            models=[{"id": "gpt-5"}],
            api_key_env=None,
        )
        service = registry["services"]["openai"]
        service["config"].pop("api_key_env", None)
        service["config"]["api_key_secret_id"] = "provider:openai:api_key"
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(admin_store, "DB_PATH", admin_db), patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(), _profile(max_request=None, budget=None, monthly=None), registry, self.ledger, admin_db_path=admin_db,
            )

        self.assertEqual(response.status_code, 200, await _response_body(response))
        self.assertEqual(seen["authorization"], "Bearer oa-stored-key")

    async def test_openai_decisions_ambiguous_provider_selection_fails_closed(self) -> None:
        registry = _registry(provider_id="openai-a", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        other = _registry(provider_id="openai-b", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        registry["services"].update(other["services"])

        response = await execution.openai(_request({"model": "gpt-5"}), _auth(), _profile(providers=frozenset({"openai-a", "openai-b"})), registry, self.ledger)
        body = json.loads(await _response_body(response))

        self.assertEqual(response.status_code, 501)
        self.assertEqual(body["error"]["code"], "decisions_backend_unavailable")

    async def test_openai_unknown_cost_under_caps_fails_closed_before_forward(self) -> None:
        called = False

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(provider_id="openai", protocols=("openai-compatible", "decisions"), price={}, models=[{"id": "gpt-5"}], api_key_env="OPENAI_TEST_KEY")
        with patch.object(execution.providers.httpx, "AsyncClient", lambda *a, **kw: real_client(transport=transport, timeout=kw.get("timeout"))):
            response = await execution.openai(_request({"model": "gpt-5", "input": "hi"}), _auth(), _profile(max_request=Decimal("0.01")), registry, self.ledger)

        body = json.loads(await _response_body(response))
        self.assertEqual(response.status_code, 402)
        self.assertEqual(body["error"]["code"], "budget_exceeded")
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
