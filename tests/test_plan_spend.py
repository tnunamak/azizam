"""Plan spend uses the cap-enforcement accounting (design ask 9)."""
from __future__ import annotations

import calendar
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store
from gateway.admin_api import AdminContext, create_admin_router
from gateway.admin_runtime import AdminSQLiteStore
from gateway.policy import AppProfile, RemoteProviderPolicy
from gateway.providers import (BudgetExceeded, Cost, ProviderNeedsReconciliation, ProviderRef, current_process_generation,
                              ensure_ledger, plan_spend, plan_spend_many, recover_provider_spend, reserve_provider_spend)

NOW = calendar.timegm((2027, 3, 10, 12, 0, 0))
MONTH_START = calendar.timegm((2027, 3, 1, 0, 0, 0))
NEXT_MONTH = calendar.timegm((2027, 4, 1, 0, 0, 0))


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_TEST_KEY", "test-key")


def _profile(profile_id="family", **remote) -> AppProfile:
    return AppProfile(id=profile_id, remote=RemoteProviderPolicy(providers=frozenset({"typesafe"}), **remote))


def _charge(ledger: Path, usd: str | None, at: int, *, profile_id="family", status="settled", estimate="9.99",
            generation: str | None = None, expires_at: int | None = None) -> None:
    ensure_ledger(ledger)
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO provider_spend (provider, profile_id, status, estimated_usd, actual_usd, cost_source, created_at, "
                     "process_generation, expires_at) VALUES ('typesafe', ?, ?, ?, ?, 'test', ?, ?, ?)",
                     (profile_id, status, estimate, usd, at, generation, expires_at))


def _by_period(entries):
    return {entry["period"]: entry for entry in entries}


def test_daily_is_rolling_and_monthly_is_the_utc_calendar_month(tmp_path):
    ledger = tmp_path / "ledger.db"
    _charge(ledger, "0.50", NOW - 2 * 3600)            # today
    _charge(ledger, "0.25", NOW - 20 * 3600)           # within 24 hours, oldest daily charge
    _charge(ledger, "1.00", NOW - 3 * 86400)           # this month only
    _charge(ledger, "4.00", MONTH_START - 3600)        # last month
    _charge(ledger, "7.00", NOW - 60, profile_id="other-plan")
    _charge(ledger, None, NOW - 60, status="reserved", estimate="0.10",  # live, in flight: counts its estimate
            generation=current_process_generation(), expires_at=NOW + 60)
    spend = _by_period(plan_spend(_profile(budget_usd_per_day=Decimal("2"), budget_usd_per_month=Decimal("10")),
                                  path=ledger, now=NOW))
    assert spend["daily"] == {"period": "daily", "window": "rolling_24h", "spent_usd": "0.850000000",
                              "cap_usd": "2", "resets_at": NOW - 20 * 3600 + 86400}
    assert spend["monthly"] == {"period": "monthly", "window": "calendar_month_utc", "spent_usd": "1.850000000",
                                "cap_usd": "10", "resets_at": NEXT_MONTH}


def test_only_capped_periods_are_reported(tmp_path):
    assert plan_spend(_profile(), path=tmp_path / "ledger.db", now=NOW) == []
    only_monthly = plan_spend(_profile(budget_usd_per_month=Decimal("3")), path=tmp_path / "ledger.db", now=NOW)
    assert [entry["period"] for entry in only_monthly] == ["monthly"]


def test_missing_ledger_file_is_zero_like_enforcement_but_no_ledger_is_unknown(tmp_path):
    profile = _profile(budget_usd_per_day=Decimal("1"))
    entry = plan_spend(profile, path=tmp_path / "absent.db", now=NOW)[0]
    assert entry["spent_usd"] == "0.000000000"
    assert entry["resets_at"] is None
    assert not (tmp_path / "absent.db").exists()  # reading never creates the ledger
    unknown = plan_spend(_profile(budget_usd_per_day=Decimal("1"), budget_usd_per_month=Decimal("5")), path=None, now=NOW)
    assert [(e["spent_usd"], e["unknown_reason"], e["resets_at"]) for e in unknown] == [
        (None, "ledger_unavailable", None), (None, "ledger_unavailable", None)]


def test_unreconciled_charge_makes_spend_unknown_not_zero(tmp_path):
    ledger = tmp_path / "ledger.db"
    _charge(ledger, None, NOW - 40 * 86400, status="needs_reconciliation", estimate="0.01")
    entry = plan_spend(_profile(budget_usd_per_day=Decimal("1")), path=ledger, now=NOW)[0]
    assert entry["spent_usd"] is None
    assert entry["unknown_reason"] == "needs_reconciliation"
    assert entry["resets_at"] is None


def test_unreadable_ledger_is_unknown(tmp_path):
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"not a database" * 100)
    entry = plan_spend(_profile(budget_usd_per_day=Decimal("1")), path=ledger, now=NOW)[0]
    assert entry["spent_usd"] is None and entry["unknown_reason"] == "ledger_unavailable"


@pytest.mark.parametrize("spent,estimate,refused", [("0.98", "0.01", False), ("0.99", "0.02", True), ("1.00", "0.00", True)])
def test_reported_spend_is_what_the_cap_barrier_enforces(tmp_path, spent, estimate, refused):
    ledger = tmp_path / "ledger.db"
    _charge(ledger, spent, NOW - 3600)
    profile = _profile(budget_usd_per_day=Decimal("1.00"))
    reported = Decimal(plan_spend(profile, path=ledger, now=NOW)[0]["spent_usd"])
    assert reported == Decimal(spent)
    provider = ProviderRef(id="typesafe", base_url="https://api.typesafe.test", protocols=("typesafe-systemone",),
                           api_key_env="TYPESAFE_TEST_KEY", cost_class="paid-remote",
                           price={"input_usd_per_million_tokens": "0.10"},
                           config={"credential_origin": "https://api.typesafe.test:443"})
    expected_refusal = reported >= Decimal("1.00") or reported + Decimal(estimate) > Decimal("1.00")
    assert expected_refusal is refused
    try:
        reserve_provider_spend(profile, provider, Cost(Decimal(estimate), "price-table", "typesafe"), path=ledger, now=NOW)
        assert not refused
    except BudgetExceeded:
        assert refused


def test_admin_profiles_list_carries_each_plan_spend(tmp_path):
    db, ledger = tmp_path / "admin.db", tmp_path / "provider_spend.db"
    admin_store.init_db(db)
    capped = admin_store.create_profile("Everyone", {"id": "everyone", "remote": {
        "providers": ["typesafe"], "budget_usd_per_day": "1", "budget_usd_per_month": "8"}}, path=db)
    uncapped = admin_store.create_profile("Local only", {"id": "local"}, path=db)
    _charge(ledger, "0.40", int(time.time()) - 60, profile_id="everyone")
    actor = AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="bearer")
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=lambda request: actor, spend_ledger_path=lambda: ledger))
    rows = {row["id"]: row for row in TestClient(app).get("/admin/api/profiles").json()["profiles"]}
    spend = _by_period(rows[capped["id"]]["plan_spend"])
    assert spend["daily"]["spent_usd"] == "0.400000000"
    assert spend["monthly"]["spent_usd"] in {"0.400000000", "0.000000000"}  # 0 only in a month's first minute
    assert spend["daily"]["cap_usd"] == "1" and spend["monthly"]["cap_usd"] == "8"
    assert rows[uncapped["id"]]["plan_spend"] == []
    # Without a wired ledger the spend is unknown, never zero.
    app = FastAPI()
    app.include_router(create_admin_router(AdminSQLiteStore(db), auth=lambda request: actor))
    rows = {row["id"]: row for row in TestClient(app).get("/admin/api/profiles").json()["profiles"]}
    assert all(entry["spent_usd"] is None for entry in rows[capped["id"]]["plan_spend"])


def _provider() -> ProviderRef:
    return ProviderRef(id="typesafe", base_url="https://api.typesafe.test", protocols=("typesafe-systemone",),
                       api_key_env="TYPESAFE_TEST_KEY", cost_class="paid-remote",
                       price={"input_usd_per_million_tokens": "0.10"},
                       config={"credential_origin": "https://api.typesafe.test:443"})


@pytest.mark.parametrize("generation,expires_at", [
    (None, NOW + 600),               # no owning process generation
    ("1:other-process", NOW + 600),  # another (dead) process generation
    ("current", NOW - 1),            # this generation, but expired
    ("current", None),               # no expiry
])
def test_stale_reservation_is_unknown_spend_exactly_as_the_barrier_sees_it(tmp_path, generation, expires_at):
    ledger = tmp_path / "ledger.db"
    generation = current_process_generation() if generation == "current" else generation
    _charge(ledger, None, NOW - 60, status="reserved", estimate="0.01", generation=generation, expires_at=expires_at)
    profile = _profile(budget_usd_per_day=Decimal("1"), budget_usd_per_month=Decimal("5"))
    spend = plan_spend(profile, path=ledger, now=NOW)
    assert [(e["spent_usd"], e.get("unknown_reason"), e["resets_at"]) for e in spend] == [
        (None, "needs_reconciliation", None), (None, "needs_reconciliation", None)]
    # The barrier first recovers stale reservations, then refuses on them.
    with pytest.raises(ProviderNeedsReconciliation):
        reserve_provider_spend(profile, _provider(), Cost(Decimal("0.01"), "price-table", "typesafe"), path=ledger, now=NOW)


def test_live_reservation_of_this_process_counts_as_spend(tmp_path):
    ledger = tmp_path / "ledger.db"
    _charge(ledger, None, NOW - 60, status="reserved", estimate="0.25",
            generation=current_process_generation(), expires_at=NOW + 600)
    assert recover_provider_spend(path=ledger, now=NOW) == 0  # the barrier would not touch it either
    entry = plan_spend(_profile(budget_usd_per_day=Decimal("1")), path=ledger, now=NOW)[0]
    assert entry["spent_usd"] == "0.250000000"
    assert entry["resets_at"] == NOW - 60 + 86400


def test_profile_window_reads_use_a_profile_led_index(tmp_path):
    ledger = tmp_path / "ledger.db"
    ensure_ledger(ledger)
    with sqlite3.connect(ledger) as conn:
        plan = " ".join(str(row[-1]) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT estimated_usd, actual_usd FROM provider_spend "
            "WHERE created_at >= ? AND status IN ('reserved', 'settled', 'needs_reconciliation') AND profile_id = ?",
            (NOW, "family")))
    assert "idx_provider_spend_profile_window" in plan


def test_many_plans_share_one_ledger_connection(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    _charge(ledger, "0.10", NOW - 60, profile_id="a")
    _charge(ledger, "0.20", NOW - 60, profile_id="b")
    import gateway.providers as providers_module
    opened = []
    real_connect = providers_module.sqlite3.connect
    monkeypatch.setattr(providers_module.sqlite3, "connect", lambda *a, **k: opened.append(a) or real_connect(*a, **k))
    spend = plan_spend_many([_profile("a", budget_usd_per_day=Decimal("1")), _profile("b", budget_usd_per_day=Decimal("1")),
                             _profile("c")], path=ledger, now=NOW)
    assert len(opened) == 1
    assert spend["a"][0]["spent_usd"] == "0.100000000"
    assert spend["b"][0]["spent_usd"] == "0.200000000"
    assert spend["c"] == []
