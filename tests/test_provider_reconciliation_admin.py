from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gateway.provider_admin import create_provider_admin_router
from test_provider_admin import FakeProviderAdminStore, _auth


def _client(store: FakeProviderAdminStore, ledger_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_provider_admin_router(store, auth=_auth, ledger_path=ledger_path)
    )
    return TestClient(app)


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
        _seed_ledger(self.ledger_path)
        self.store = FakeProviderAdminStore()
        self.client = _client(self.store, self.ledger_path)

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
