from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from gateway import admin_store, providers
from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy


def _provider(**overrides: object) -> providers.ProviderRef:
    data = {
        "id": "openrouter",
        "base_url": "https://openrouter.test/api/v1",
        "protocols": ("openai",),
        "api_key_env": "OPENROUTER_TEST_KEY",
        "cost_class": "paid-remote",
        "price": {},
        "config": {"credential_origin": "https://openrouter.test"},
    }
    data.update(overrides)
    return providers.ProviderRef(**data)  # type: ignore[arg-type]


def _profile(**remote_overrides: object):
    return DEFAULT_PROFILE.with_overrides(
        id="paid-plan",
        remote=RemoteProviderPolicy(
            providers=frozenset({"openrouter"}),
            **remote_overrides,
        ),
    )


class ProviderCrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "provider_spend.db"
        self.admin_db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.admin_db)
        os.environ["OPENROUTER_TEST_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("OPENROUTER_TEST_KEY", None))

    def _reserve(
        self,
        *,
        generation: str = "100:old-process",
        now: int = 1_700_000_000,
        timeout: float = providers.DEFAULT_PROVIDER_REQUEST_TIMEOUT_S,
        profile=None,
        provider=None,
    ) -> providers.Reservation:
        with patch.object(providers, "current_process_generation", return_value=generation):
            return providers.reserve_provider_spend(
                profile or _profile(),
                provider or _provider(),
                providers.Cost(Decimal("0.000605"), "price-table", "openrouter"),
                path=self.ledger,
                admin_db_path=self.admin_db,
                now=now,
                request_timeout_s=timeout,
            )

    def _rows(self) -> list[sqlite3.Row]:
        with sqlite3.connect(self.ledger) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute("SELECT * FROM provider_spend ORDER BY id"))

    def test_restored_mid_request_snapshot_denies_second_call_until_reconciled(self) -> None:
        self._reserve(generation="101:before-crash", now=1_700_000_000)
        snapshot = Path(self.tmp.name) / "snapshot.db"
        shutil.copyfile(self.ledger, snapshot)
        self.ledger.unlink()
        shutil.copyfile(snapshot, self.ledger)

        with patch.object(providers, "current_process_generation", return_value="202:after-crash"):
            with self.assertRaises(providers.ProviderNeedsReconciliation):
                providers.reserve_provider_spend(
                    _profile(budget_usd_per_day=Decimal("0.02")),
                    _provider(),
                    providers.Cost(Decimal("0.000605"), "price-table", "openrouter"),
                    path=self.ledger,
                    admin_db_path=self.admin_db,
                    now=1_700_000_001,
                )

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "needs_reconciliation")
        self.assertEqual(rows[0]["cost_source"], "unknown")

    def test_current_schema_rows_missing_ownership_fail_closed(self) -> None:
        with sqlite3.connect(self.ledger) as conn:
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
                VALUES ('openrouter', 'paid-plan', 'reserved', '0.000605', NULL,
                        'price-table', 1700000000, NULL, 'req_1', 'gen_1')
                """
            )

        recovered = providers.recover_provider_spend(path=self.ledger, now=1_700_000_001)

        self.assertEqual(recovered, 0)
        row = self._rows()[0]
        self.assertEqual(row["status"], "needs_reconciliation")
        self.assertIsNone(row["process_generation"])
        self.assertIsNone(row["expires_at"])

    def test_expired_current_process_reservation_is_recovered(self) -> None:
        self._reserve(generation="303:current", now=1_700_000_000, timeout=120)

        with patch.object(providers, "current_process_generation", return_value="303:current"):
            recovered = providers.recover_provider_spend(
                path=self.ledger,
                now=1_700_000_120,
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(self._rows()[0]["status"], "needs_reconciliation")

    def test_active_current_process_reservation_is_preserved(self) -> None:
        self._reserve(generation="404:current", now=1_700_000_000, timeout=120)

        with patch.object(providers, "current_process_generation", return_value="404:current"):
            recovered = providers.recover_provider_spend(
                path=self.ledger,
                now=1_700_000_119,
            )

        row = self._rows()[0]
        self.assertEqual(recovered, 0)
        self.assertEqual(row["status"], "reserved")
        self.assertEqual(row["process_generation"], "404:current")
        self.assertEqual(row["expires_at"], 1_700_000_120)

    def test_recovered_unknown_blocks_provider_cap(self) -> None:
        capped_provider = _provider(config={
            "credential_origin": "https://openrouter.test",
            "spend_caps_usd": {"daily": "0.02"},
        })
        self._reserve(generation="505:old", provider=capped_provider)

        with patch.object(providers, "current_process_generation", return_value="606:new"):
            with self.assertRaises(providers.ProviderNeedsReconciliation):
                providers.reserve_provider_spend(
                    _profile(),
                    capped_provider,
                    providers.Cost(Decimal("0.000605"), "price-table", "openrouter"),
                    path=self.ledger,
                    admin_db_path=self.admin_db,
                    now=1_700_000_001,
                )

        self.assertEqual(self._rows()[0]["status"], "needs_reconciliation")

    def test_recovered_unknown_blocks_plan_cap(self) -> None:
        self._reserve(generation="707:old")

        with patch.object(providers, "current_process_generation", return_value="808:new"):
            with self.assertRaises(providers.ProviderNeedsReconciliation):
                providers.reserve_provider_spend(
                    _profile(budget_usd_per_day=Decimal("0.02")),
                    _provider(),
                    providers.Cost(Decimal("0.000605"), "price-table", "openrouter"),
                    path=self.ledger,
                    admin_db_path=self.admin_db,
                    now=1_700_000_001,
                )

    def test_recovered_unknown_blocks_global_admin_cap_durably_after_denial(self) -> None:
        admin_store.put_operator_setting(
            "provider_admin",
            {"providers": {}, "global_caps": {"daily_usd": "0.02"}},
            path=self.admin_db,
        )
        self._reserve(generation="909:old")

        with patch.object(providers, "current_process_generation", return_value="1001:new"):
            with self.assertRaises(providers.ProviderNeedsReconciliation):
                providers.reserve_provider_spend(
                    _profile(),
                    _provider(),
                    providers.Cost(Decimal("0.000605"), "price-table", "openrouter"),
                    path=self.ledger,
                    admin_db_path=self.admin_db,
                    now=1_700_000_001,
                )

        self.assertEqual(self._rows()[0]["status"], "needs_reconciliation")


if __name__ == "__main__":
    unittest.main()
