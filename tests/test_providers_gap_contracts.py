"""Provider core gap contracts (azgap gw-providers): atomic concurrency slot."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
from gateway.providers import (  # noqa: E402
    Cost,
    ProviderConcurrencyLimited,
    ensure_ledger,
    release_provider_spend,
    reserve_provider_spend,
)
from test_providers import profile, provider  # noqa: E402

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import sqlite3
import threading
from gateway import providers as provider_core

def test_concurrency_slot_is_reserved_atomically_without_money_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_TEST_KEY", "synthetic-local-only-key")
    ledger = tmp_path / "ledger.db"
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    ensure_ledger(ledger)
    ref = provider(config={"concurrency_limit": 1})
    original = provider_core._enforce_provider_concurrency

    def inside_transaction(conn, provider_id, provider_ref):
        assert conn.in_transaction, "concurrency check moved outside transaction"
        return original(conn, provider_id, provider_ref)

    monkeypatch.setattr(provider_core, "_enforce_provider_concurrency", inside_transaction)
    barrier = threading.Barrier(3, timeout=5)

    def attempt(index):
        barrier.wait()
        try:
            reservation = reserve_provider_spend(
                profile(app_id=f"app-{index}"),
                ref, Cost(Decimal("0.001"), "price-table", ref.id),
                path=ledger, admin_db_path=admin_db,
                now=1700000000, request_timeout_s=60,
            )
            return ("allowed", reservation)
        except ProviderConcurrencyLimited as exc:
            assert exc.status_code == 429
            assert exc.code == "provider_concurrency_limited"
            return ("denied", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, index) for index in range(2)]
        try:
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]
        finally:
            barrier.abort()

    assert sorted(result[0] for result in results) == ["allowed", "denied"]
    with sqlite3.connect(ledger) as conn:
        assert conn.execute(
            "SELECT provider, status, COUNT(*) FROM provider_spend GROUP BY provider, status"
        ).fetchall() == [("typesafe", "reserved", 1)]
    winner = next(reservation for state, reservation in results if state == "allowed")
    release_provider_spend(winner, path=ledger, now=1700000001)
    replacement = reserve_provider_spend(
        profile(app_id="app-replacement"), ref, Cost(Decimal("0.001"), "price-table", ref.id),
        path=ledger, admin_db_path=admin_db,
        now=1700000001, request_timeout_s=60,
    )
    assert replacement.id != winner.id
    with sqlite3.connect(ledger) as conn:
        assert conn.execute(
            "SELECT status, COUNT(*) FROM provider_spend GROUP BY status ORDER BY status"
        ).fetchall() == [("released", 1), ("reserved", 1)]
