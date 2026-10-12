from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gateway import admin_store, receipt_store
from tests.support.access import plan_for  # noqa: E402
from gateway.provider_admin import create_provider_admin_router
from test_provider_admin import FakeProviderAdminStore, _auth


def _client(
    store: FakeProviderAdminStore,
    ledger_path: Path,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_provider_admin_router(store, auth=_auth, ledger_path=ledger_path,
                                     receipts_path=ledger_path.with_name("receipts.db"))
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _seed_ledger(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE provider_spend (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                estimated_usd TEXT NOT NULL,
                actual_usd TEXT,
                cost_source TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                settled_at INTEGER,
                request_id TEXT,
                generation_id TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO provider_spend
            (provider, profile_id, status, estimated_usd, actual_usd, cost_source,
             created_at, settled_at, request_id, generation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "openrouter",
                "builders",
                "needs_reconciliation",
                "0.020000000",
                None,
                "unknown",
                1_700_000_000,
                1_700_000_010,
                "req_123",
                "gen_456",
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_spend
            (provider, profile_id, status, estimated_usd, actual_usd, cost_source,
             created_at, settled_at, request_id, generation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "typesafe",
                "builders",
                "needs_reconciliation",
                "0.010000000",
                None,
                "unknown",
                1_700_000_020,
                1_700_000_030,
                "req_other",
                None,
            ),
        )
        conn.commit()


class ProviderReconciliationAdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger_path = Path(self.tmpdir.name) / "provider_spend.db"
        self.receipts_path = Path(self.tmpdir.name) / "receipts.db"
        _seed_ledger(self.ledger_path)
        receipt_store.init_db(self.receipts_path)
        self.store = FakeProviderAdminStore()
        admin_store.init_db(self.store.path)
        self.client = _client(self.store, self.ledger_path)

    def test_member_cannot_reconcile_owner_charges(self) -> None:
        response = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer member"}, json={"actual_usd": "0"},
        )
        self.assertIn(response.status_code, (401, 403))
        with sqlite3.connect(self.ledger_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM provider_spend WHERE id=1").fetchone()[0],
                             "needs_reconciliation")

    def test_provider_surface_reports_unknown_spend_even_after_window_rollover(self) -> None:
        response = self.client.get("/admin/api/providers", headers={"authorization": "Bearer admin"})
        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.json()["providers"]}
        self.assertEqual(rows["openrouter"]["spend"]["status"], "needs_reconciliation")
        self.assertEqual(rows["openrouter"]["spend"]["pending_reconciliation_count"], 1)
        self.assertEqual(response.json()["global"]["spend"]["pending_reconciliation_count"], 2)

    def test_owner_reconciliation_is_audited_in_runtime_store_database(self) -> None:
        response = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"}, json={"actual_usd": "0.03"},
        )
        self.assertEqual(response.status_code, 200)
        events = admin_store.list_audit(path=self.store.path)
        self.assertEqual(events[0]["action"], "operator.provider_reconciliation.update")
        self.assertEqual(events[0]["metadata"]["actual_usd"], "0.030000000")

    def test_lists_pending_reconciliations_for_provider(self) -> None:
        response = self.client.get(
            "/admin/api/providers/openrouter/reconciliation",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "reconciliations": [
                    {
                        "id": 1,
                        "provider": "openrouter",
                        "profile_id": "builders",
                        "status": "needs_reconciliation",
                        "estimated_usd": "0.020000000",
                        "actual_usd": None,
                        "cost_source": "unknown",
                        "created_at": 1_700_000_000,
                        "settled_at": 1_700_000_010,
                        "request_id": "req_123",
                        "generation_id": "gen_456",
                    }
                ]
            },
        )

    def test_owner_reconciles_pending_provider_spend(self) -> None:
        response = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.030000000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reconciliation"]["status"], "settled")
        self.assertEqual(response.json()["reconciliation"]["actual_usd"], "0.030000000")
        self.assertEqual(response.json()["reconciliation"]["cost_source"], "owner-reported")
        followup = self.client.get(
            "/admin/api/providers/openrouter/reconciliation",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(followup.status_code, 200)
        self.assertEqual(followup.json(), {"reconciliations": []})

    def test_owner_reconciliation_updates_persisted_receipt_cost(self) -> None:
        receipt_store.record(
            {
                "request_id": "req_123",
                "protocol": "openai.chat",
                "intent": "chat.completions",
                "path": "/v1/chat/completions",
                "status": "succeeded",
                "accepted_at_ms": 1_700_000_000_000,
                "updated_at_ms": 1_700_000_001_000,
                "remote": True,
                "cost": {
                    "usd": None,
                    "source": "unknown",
                    "provider": "openrouter",
                    "cost_class": "paid-remote",
                    "reservation_id": 1,
                    "reconciliation_status": "needs_reconciliation",
                },
                "metadata": {
                    "decisions": {
                        "question_count": 2,
                        "engine": None,
                        "remote": True,
                        "cost_usd": None,
                        "usage": {"input_tokens": 302, "output_tokens": 37},
                    }
                },
            },
            path=self.receipts_path,
        )

        with patch.object(receipt_store, "DB_PATH", self.receipts_path):
            response = self.client.post(
                "/admin/api/providers/openrouter/reconciliation/1",
                headers={"authorization": "Bearer admin"},
                json={"actual_usd": "0.030000000"},
            )

        self.assertEqual(response.status_code, 200)
        stored = receipt_store.get("req_123", path=self.receipts_path)
        self.assertEqual(
            stored["cost"],
            {
                "usd": "0.030000000",
                "source": "owner-reported",
                "provider": "openrouter",
                "cost_class": "paid-remote",
                "reservation_id": 1,
                "reconciliation_status": "settled",
            },
        )
        self.assertEqual(stored["metadata"]["decisions"]["cost_usd"], "0.030000000")

    def test_owner_reconciliation_retry_repairs_receipt_after_update_failure(self) -> None:
        receipt_store.record(
            {
                "request_id": "req_123",
                "protocol": "openai.chat",
                "intent": "chat.completions",
                "path": "/v1/chat/completions",
                "status": "succeeded",
                "accepted_at_ms": 1_700_000_000_000,
                "updated_at_ms": 1_700_000_001_000,
                "remote": True,
                "cost": {
                    "usd": None,
                    "source": "unknown",
                    "provider": "openrouter",
                    "cost_class": "paid-remote",
                    "reservation_id": 1,
                    "reconciliation_status": "needs_reconciliation",
                },
                "metadata": {
                    "decisions": {
                        "question_count": 2,
                        "engine": None,
                        "remote": True,
                        "cost_usd": None,
                        "usage": {"input_tokens": 302, "output_tokens": 37},
                    }
                },
            },
            path=self.receipts_path,
        )

        calls = 0
        real_update = receipt_store.update_cost

        def flaky_update(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("receipt store unavailable")
            return real_update(*args, **kwargs)

        retry_client = _client(self.store, self.ledger_path, raise_server_exceptions=False)
        with (
            patch.object(receipt_store, "DB_PATH", self.receipts_path),
            patch.object(receipt_store, "update_cost", side_effect=flaky_update),
        ):
            failed = retry_client.post(
                "/admin/api/providers/openrouter/reconciliation/1",
                headers={"authorization": "Bearer admin"},
                json={"actual_usd": "0.030000000"},
            )
            retry = retry_client.post(
                "/admin/api/providers/openrouter/reconciliation/1",
                headers={"authorization": "Bearer admin"},
                json={"actual_usd": "0.030000000"},
            )

        self.assertEqual(failed.status_code, 500)
        with sqlite3.connect(self.ledger_path) as conn:
            self.assertEqual(
                conn.execute("SELECT status, actual_usd FROM provider_spend WHERE id = 1").fetchone(),
                ("settled", "0.030000000"),
            )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["reconciliation"]["status"], "settled")
        stored = receipt_store.get("req_123", path=self.receipts_path)
        self.assertEqual(stored["cost"]["usd"], "0.030000000")
        self.assertEqual(stored["cost"]["reconciliation_status"], "settled")
        self.assertEqual(stored["metadata"]["decisions"]["cost_usd"], "0.030000000")

    def test_owner_reconciliation_retry_rejects_different_settled_amount(self) -> None:
        first = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.030000000"},
        )
        second = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.040000000"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"]["code"], "invalid_provider_reconciliation")




    def test_remove_with_absent_explicit_ledger_does_not_create_ledger(self) -> None:
        absent_ledger = Path(self.tmpdir.name) / "absent-provider-spend.db"
        app = FastAPI()
        app.include_router(
            create_provider_admin_router(self.store, auth=_auth, ledger_path=absent_ledger)
        )
        client = TestClient(app)

        removed = client.delete(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(removed.status_code, 200)
        self.assertFalse(absent_ledger.exists())
        self.assertNotIn("openrouter", self.store.registry["services"])

    def test_owner_can_reconcile_historical_row_after_provider_was_removed(self) -> None:
        self.store.registry["services"].pop("openrouter")

        listed = self.client.get(
            "/admin/api/providers/openrouter/reconciliation",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["reconciliations"][0]["id"], 1)

        settled = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.03"},
        )
        self.assertEqual(settled.status_code, 200)
        self.assertEqual(settled.json()["reconciliation"]["status"], "settled")

    def test_remove_rejects_provider_with_reconciliation_then_allows_after_owner_settlement(self) -> None:
        blocked = self.client.delete(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "provider_spend_pending")
        self.assertIn("openrouter", self.store.registry["services"])

        settled = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.03"},
        )
        self.assertEqual(settled.status_code, 200)

        removed = self.client.delete(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn("openrouter", self.store.registry["services"])

    def test_remove_rejects_provider_with_reserved_inflight_charge(self) -> None:
        from gateway.providers import Cost, provider_lookup, reserve_provider_spend

        provider = provider_lookup(self.store.registry, "openrouter-canonical")
        self.assertIsNotNone(provider)
        profile = plan_for("builders", paid=sorted({"openrouter-canonical"}))
        with patch.dict(os.environ, {"OPENROUTER_CANONICAL_API_KEY": "test-key"}):
            reserve_provider_spend(
                profile,
                provider,
                Cost(Decimal("0.050000000"), "pre-dispatch", "openrouter-canonical"),
                path=self.ledger_path,
            )

        blocked = self.client.delete(
            "/admin/api/providers/openrouter-canonical",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "provider_spend_pending")
        self.assertIn("reserved", blocked.json()["error"]["message"])
        self.assertIn("openrouter-service", self.store.registry["services"])

    def test_reconcile_rejects_unknown_provider_mismatch_and_invalid_amount(self) -> None:
        missing_provider = self.client.get(
            "/admin/api/providers/not-there/reconciliation",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(missing_provider.status_code, 404)

        mismatch = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/2",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": "0.01"},
        )
        self.assertEqual(mismatch.status_code, 404)
        self.assertEqual(mismatch.json()["error"]["code"], "provider_reconciliation_not_found")

        invalid = self.client.post(
            "/admin/api/providers/openrouter/reconciliation/1",
            headers={"authorization": "Bearer admin"},
            json={"actual_usd": -1},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_provider_reconciliation")


if __name__ == "__main__":
    unittest.main()
