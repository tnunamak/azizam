from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from gateway import decision_provider_execution as execution
from tests.support.access import plan_for  # noqa: E402
from gateway import decisions_api, receipt_store
def _profile(
    *,
    providers=frozenset({"typesafe", "openai"}),
    max_request=Decimal("1.00"),
    budget=Decimal("5.00"),
    monthly=Decimal("20.00"),
):
    return plan_for("paid-decisions", paid=sorted(providers))


def _auth(app_id="app_1", *, delegated=False):
    return SimpleNamespace(
        identity=SimpleNamespace(app_id=app_id, name="Decision App", delegated=delegated)
    )


def _registry(
    *,
    provider_id="typesafe",
    protocols=("typesafe-systemone",),
    models=None,
    price=None,
    api_key_env=None,
):
    config = {
        "provider_id": provider_id,
        "base_url": f"https://{provider_id}.test",
        "api_key_env": api_key_env or f"{provider_id.upper()}_TEST_KEY",
        "credential_origin": f"https://{provider_id}.test",
        "cost_class": "paid-remote",
        "price": price if price is not None else {"input_usd_per_million_tokens": "1.00"},
    }
    if models is not None:
        config["models"] = models
    return {
        "services": {
            provider_id: {
                "id": provider_id,
                "kind": "provider",
                "enabled": True,
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

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
        },
        receive,
    )
    request.state.request_id = "req-openai"
    return request


class DecisionProviderReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.db"
        self.receipts_db = Path(self.tmp.name) / "receipts.db"
        receipt_store.init_db(self.receipts_db)
        self.addCleanup(self.tmp.cleanup)
        self.receipts_path = patch.object(receipt_store, "DB_PATH", self.receipts_db)
        self.receipts_path.start()
        self.addCleanup(self.receipts_path.stop)
        self.env = patch.dict(
            os.environ,
            {"TYPESAFE_TEST_KEY": "ts-key", "OPENAI_TEST_KEY": "oa-key"},
        )
        # begin/finalize/update_phases bind ``path`` at definition time, so the
        # spies route to this test's store explicitly instead of the default.
        real_begin, real_finalize = receipt_store.begin, receipt_store.finalize
        real_phases = receipt_store.update_phases
        self.begin = patch.object(
            receipt_store, "begin", side_effect=lambda r: real_begin(r, path=self.receipts_db)
        )
        self.finalize = patch.object(
            receipt_store, "finalize", side_effect=lambda r: real_finalize(r, path=self.receipts_db)
        )
        self.phases = patch.object(
            receipt_store, "update_phases", side_effect=lambda r: real_phases(r, path=self.receipts_db)
        )
        self.phases.start()
        self.addCleanup(self.phases.stop)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.begun = self.begin.start()
        self.addCleanup(self.begin.stop)
        self.finalized = self.finalize.start()
        self.addCleanup(self.finalize.stop)

    async def test_typesafe_unknown_cost_dispatch_result_links_reconciliation_reservation(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "jev-latest",
                    "answers": {"q": {"type": "noul", "noul": 1.0}},
                    "usage": {"input_tokens": 1, "output_tokens": 0, "unpriced_usage_units": 1},
                },
            )

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(models=[{"id": "typesafe/jev-latest"}])
        with patch.object(
            execution.providers.httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
        ):
            result = await execution.systemone(
                _auth(),
                _profile(max_request=None, budget=None, monthly=None),
                {
                    "model": "typesafe/jev-latest",
                    "state": "x",
                    "questions": {"q": {"type": "noul", "instructions": "ok?"}},
                },
                "req-ts-unknown",
                registry,
                self.ledger,
            )

        self.assertEqual(result.status_code, 200)
        self.assertIsNotNone(result.reservation_id)
        self.assertEqual(result.reconciliation_status, "needs_reconciliation")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, request_id FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("needs_reconciliation", "req-ts-unknown")])

    async def test_typesafe_resolved_response_model_prices_requested_alias(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"q": {"type": "noul", "noul": 1.0}},
                    "usage": {"input_tokens": 302, "output_tokens": 37},
                },
            )

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(
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
        registry["services"]["typesafe"]["config"]["model_listing"] = {
            "alias_resolution": {"price_model": "configured_model"}
        }
        with patch.object(
            execution.providers.httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
        ):
            result = await execution.systemone(
                _auth(),
                _profile(),
                {
                    "model": "typesafe/jev-latest",
                    "state": "x",
                    "questions": {"q": {"type": "noul", "instructions": "ok?"}},
                },
                "req-ts-priced",
                registry,
                self.ledger,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.cost_usd, "0.000012684")
        self.assertEqual(result.cost_source, "price-table")
        self.assertEqual(result.reconciliation_status, "settled")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, actual_usd, request_id FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("settled", "0.000012684", "req-ts-priced")])

    async def test_typesafe_timeout_result_marks_reconciliation_needed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("provider request timed out", request=request)

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(models=[{"id": "typesafe/jev-latest"}])
        with patch.object(
            execution.providers.httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
        ):
            result = await execution.systemone(
                _auth(),
                _profile(max_request=None, budget=None, monthly=None),
                {
                    "model": "typesafe/jev-latest",
                    "state": "x",
                    "questions": {"q": {"type": "noul", "instructions": "ok?"}},
                },
                "req-ts-timeout",
                registry,
                self.ledger,
            )

        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.reconciliation_status, "needs_reconciliation")
        self.assertIsNotNone(result.reservation_id)
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT id, status, request_id FROM provider_spend").fetchall()
        self.assertEqual(rows, [(result.reservation_id, "needs_reconciliation", "req-ts-timeout")])

    async def test_openai_unknown_cost_response_links_receipt_and_marks_reconciliation(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"id": "dec_unknown", "usage": {"input_tokens": 1, "output_tokens": 0}},
            )

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        registry = _registry(
            provider_id="openai",
            protocols=("openai-compatible", "decisions"),
            models=[{"id": "gpt-5"}],
            api_key_env="OPENAI_TEST_KEY",
        )
        with patch.object(
            execution.providers.httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
        ):
            response = await execution.openai(
                _request({"model": "gpt-5", "input": "hi"}),
                _auth(),
                _profile(max_request=None, budget=None, monthly=None),
                registry,
                self.ledger,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-azizam-cost-usd"], "unknown")
        self.finalized.assert_called_once()
        receipt = self.finalized.call_args.args[0]
        self.begun.assert_called_once_with(receipt)
        self.assertEqual(receipt_store.count(self.receipts_db), 1)
        persisted = receipt_store.get(receipt.request_id, audience="internal", path=self.receipts_db)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["request_id"], "req-openai")
        self.assertEqual(persisted["cost"]["reconciliation_status"], "needs_reconciliation")
        self.assertIsNotNone(persisted["cost"]["reservation_id"])
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT id, status, request_id FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [(persisted["cost"]["reservation_id"], "needs_reconciliation", "req-openai")])
        self.assertEqual(receipt.cost["reservation_id"], persisted["cost"]["reservation_id"])
        self.assertEqual(receipt.cost["reconciliation_status"], "needs_reconciliation")


class DecisionsApiReconciliationReceiptTests(unittest.TestCase):
    def test_receipt_forwards_reconciliation_metadata(self) -> None:
        receipts = []
        app = FastAPI()

        def authenticate(_request, _path):
            return SimpleNamespace(app_profile="paid")

        def profile_for(_auth):
            return SimpleNamespace(id="paid")

        def models_for(_auth, _profile):
            return []

        def dispatch(_auth, _profile, body, _request_id):
            return decisions_api.DecisionDispatchResult(
                body={
                    "model": body["model"],
                    "answers": {"q": {"type": "noul", "noul": 1.0}},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
                backend="typesafe",
                resolved_model="jev-latest",
                remote=True,
                cost_usd=None,
                cost_source="unknown",
                reservation_id=42,
                reconciliation_status="needs_reconciliation",
            )

        def record_receipt(_auth, receipt):
            receipts.append(receipt)

        decisions_api.register_decision_routes(
            app,
            decisions_api.DecisionsApiDeps(
                authenticate=authenticate,
                profile_for=profile_for,
                models_for=models_for,
                dispatch=dispatch,
                record_receipt=record_receipt,
            ),
        )

        response = TestClient(app).post(
            "/typesafe/v1/systemone",
            json={
                "model": "typesafe/jev-latest",
                "state": "x",
                "questions": {"q": {"type": "noul", "instructions": "ok?"}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(receipts[0].reservation_id, 42)
        self.assertEqual(receipts[0].reconciliation_status, "needs_reconciliation")
