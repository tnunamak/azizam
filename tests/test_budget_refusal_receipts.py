"""Spending-limit refusals name their limit on the receipt (design ask 10)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from gateway import admin_store
from tests.support.access import access, identity, plan_for  # noqa: E402
from gateway.providers import BudgetExceeded, Cost, ProviderNeedsReconciliation, ProviderRef, ensure_ledger, reserve_provider_spend
from gateway.receipts import RouteReceipt, serialize_receipt_for_audience

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_TEST_KEY", "test-key")


def _provider(**config) -> ProviderRef:
    return ProviderRef(id="typesafe", base_url="https://api.typesafe.test", protocols=("typesafe-systemone",),
                       api_key_env="TYPESAFE_TEST_KEY", cost_class="paid-remote",
                       price={"input_usd_per_million_tokens": "0.10"},
                       config={"credential_origin": "https://api.typesafe.test:443", **config})


def _profile(**caps):
    """An App whose plans list typesafe; ``caps`` are the plans' spending caps."""
    return identity(plan_for("family", paid=["typesafe"], **caps), app_id="app_family", accountable_person_id="usr_family")


def _spent(ledger: Path, usd: str, *, person_id: str | None = "usr_someone_else", app_id: str | None = "app_someone_else",
           status: str = "settled", at: int = NOW - 60) -> None:
    ensure_ledger(ledger)
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO provider_spend (provider, person_id, app_id, status, estimated_usd, actual_usd, cost_source, created_at) "
                     "VALUES ('typesafe', ?, ?, ?, ?, ?, 'test', ?)",
                     (person_id, app_id, status, usd, usd if status == "settled" else None, at))


def _refusal(tmp_path, profile, provider, *, admin=None) -> dict:
    admin_db = None
    if admin is not None:
        admin_db = tmp_path / "admin.db"
        admin_store.init_db(admin_db)
        admin_store.put_operator_setting("provider_admin", admin, path=admin_db)
    with pytest.raises(BudgetExceeded) as caught:
        reserve_provider_spend(profile, provider, Cost(Decimal("0.01"), "price-table", "typesafe"),
                               path=tmp_path / "ledger.db", admin_db_path=admin_db, now=NOW)
    return caught.value.refusal()


def test_provider_daily_cap_names_daily_provider(tmp_path):
    _spent(tmp_path / "ledger.db", "2.00")
    refusal = _refusal(tmp_path, _profile(), _provider(spend_caps_usd={"daily": "2.00"}))
    assert refusal == {"code": "budget_exceeded", "limit": {"period": "daily", "scope": "provider"}}


def test_global_monthly_cap_names_monthly_global(tmp_path):
    _spent(tmp_path / "ledger.db", "4.00")
    refusal = _refusal(tmp_path, _profile(), _provider(), admin={"providers": {}, "global_caps": {"monthly_usd": "4.00"}})
    assert refusal == {"code": "budget_exceeded", "limit": {"period": "monthly", "scope": "global"}}


def test_unknown_price_refusal_has_code_but_no_single_limit(tmp_path):
    with pytest.raises(BudgetExceeded) as caught:
        reserve_provider_spend(_profile(), _provider(spend_caps_usd={"daily": "1"}), Cost(None, "unknown", "typesafe"),
                               path=tmp_path / "ledger.db", now=NOW)
    assert caught.value.refusal() == {"code": "budget_exceeded", "limit": None}


def test_unreconciled_spend_refusal_keeps_its_own_code(tmp_path):
    _spent(tmp_path / "ledger.db", "0.10", status="needs_reconciliation")
    with pytest.raises(ProviderNeedsReconciliation) as caught:
        reserve_provider_spend(_profile(), _provider(spend_caps_usd={"daily": "1"}), Cost(Decimal("0.01"), "price-table", "typesafe"),
                               path=tmp_path / "ledger.db", now=NOW)
    assert caught.value.refusal() == {"code": "provider_needs_reconciliation", "limit": {"period": "daily", "scope": "provider"}}


def test_client_receipt_shows_refusal_codes_only():
    receipt = RouteReceipt(protocol="openai.chat", intent="chat.completions", backend="typesafe", app_profile="family")
    receipt.set_refusal({"code": "budget_exceeded", "limit": {"period": "daily", "scope": "person", "cap_usd": "1.00"},
                         "spent_usd": "0.99", "other_user": "usr_x"})
    client = receipt.to_dict("client")
    assert client["refusal"] == {"code": "budget_exceeded", "limit": {"period": "daily", "scope": "person"}}
    assert "1.00" not in str(client) and "usr_x" not in str(client)
    assert receipt.to_dict("admin")["refusal"] == client["refusal"]


def test_client_projection_drops_untrusted_stored_refusal_values():
    stored = {"request_id": "r", "status": "failed",
              "refusal": {"code": "budget_exceeded", "limit": {"period": "weekly", "scope": "person"}, "note": "x"}}
    assert serialize_receipt_for_audience(stored, "client")["refusal"] == {"code": "budget_exceeded", "limit": None}
    assert "refusal" not in serialize_receipt_for_audience({"request_id": "r", "refusal": {"code": "other"}}, "client")
    assert "refusal" not in RouteReceipt(protocol="p", intent="i").to_dict("client")


def _limit(tmp_path, identity_, estimate="0.01"):
    with pytest.raises(BudgetExceeded) as caught:
        reserve_provider_spend(identity_, _provider(), Cost(Decimal(estimate), "price-table", "typesafe"),
                               path=tmp_path / "ledger.db", now=NOW)
    return caught.value.refusal()["limit"]


def test_person_plan_daily_cap_counts_the_persons_spend_across_apps(tmp_path):
    _spent(tmp_path / "ledger.db", "2.00", person_id="usr_family", app_id="app_other")
    assert _limit(tmp_path, _profile(usd_per_day="2.00")) == {"period": "daily", "scope": "person"}


def test_plan_cap_does_not_count_another_persons_spend(tmp_path):
    _spent(tmp_path / "ledger.db", "9.00", person_id="usr_other", app_id="app_other")
    reserve_provider_spend(_profile(usd_per_day="2.00"), _provider(), Cost(Decimal("0.01"), "price-table", "typesafe"),
                           path=tmp_path / "ledger.db", now=NOW)


def test_app_plan_monthly_cap_counts_only_the_apps_spend(tmp_path):
    person_plan = plan_for("person", paid=["typesafe"])
    app_plan = plan_for("app", paid=["typesafe"], usd_per_month="3.00")
    mixed = identity(access(person_plan, app_plan), app_id="app_family", accountable_person_id="usr_family")
    _spent(tmp_path / "ledger.db", "3.00", person_id="usr_family", app_id="app_family")
    assert _limit(tmp_path, mixed) == {"period": "monthly", "scope": "app"}


def test_per_request_cap_refuses_before_the_ledger_is_touched(tmp_path):
    assert _limit(tmp_path, _profile(usd_per_request="0.001")) == {"period": "request", "scope": "person"}
    assert not (tmp_path / "ledger.db").exists()


def test_changing_the_plan_does_not_reset_the_counter(tmp_path):
    _spent(tmp_path / "ledger.db", "2.00", person_id="usr_family", app_id="app_family")
    renamed = identity(plan_for("a-different-plan-id", paid=["typesafe"], usd_per_day="2.00"),
                       app_id="app_family", accountable_person_id="usr_family")
    assert _limit(tmp_path, renamed) == {"period": "daily", "scope": "person"}


def test_unknown_cost_is_refused_when_a_plan_cap_applies(tmp_path):
    with pytest.raises(BudgetExceeded):
        reserve_provider_spend(_profile(usd_per_day="2.00"), _provider(), Cost(None, "unknown", "typesafe"),
                               path=tmp_path / "ledger.db", now=NOW)


def test_unreconciled_charge_of_the_person_blocks_that_persons_capped_requests(tmp_path):
    _spent(tmp_path / "ledger.db", "0.10", person_id="usr_family", app_id="app_x", status="needs_reconciliation")
    with pytest.raises(ProviderNeedsReconciliation) as caught:
        reserve_provider_spend(_profile(usd_per_day="5.00"), _provider(), Cost(Decimal("0.01"), "price-table", "typesafe"),
                               path=tmp_path / "ledger.db", now=NOW)
    assert caught.value.refusal() == {"code": "provider_needs_reconciliation", "limit": {"period": "daily", "scope": "person"}}


def test_old_ledger_loses_profile_id_keeps_its_rows_and_is_backed_up(tmp_path):
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE provider_spend (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, "
                     "profile_id TEXT NOT NULL, status TEXT NOT NULL, estimated_usd TEXT NOT NULL, actual_usd TEXT, "
                     "cost_source TEXT NOT NULL, created_at INTEGER NOT NULL, settled_at INTEGER, request_id TEXT, "
                     "generation_id TEXT, process_generation TEXT, expires_at INTEGER)")
        conn.execute("CREATE INDEX idx_provider_spend_window ON provider_spend(provider, profile_id, created_at, status)")
        conn.execute("INSERT INTO provider_spend(provider, profile_id, status, estimated_usd, actual_usd, cost_source, created_at) "
                     "VALUES ('typesafe', 'owner-local', 'settled', '1.00', '1.00', 'test', ?)", (NOW - 60,))
    ensure_ledger(ledger)
    with sqlite3.connect(ledger) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_spend)")}
        rows = conn.execute("SELECT provider, actual_usd, app_id, person_id FROM provider_spend").fetchall()
    assert "profile_id" not in columns
    assert rows == [("typesafe", "1.00", None, None)]
    with sqlite3.connect(tmp_path / "ledger.db.bak.pre-access-plans") as backup:
        assert "profile_id" in {row[1] for row in backup.execute("PRAGMA table_info(provider_spend)")}
    # The old row still counts toward the provider's total.
    with pytest.raises(BudgetExceeded):
        reserve_provider_spend(_profile(), _provider(spend_caps_usd={"daily": "1.00"}), Cost(Decimal("0.01"), "price-table", "typesafe"),
                               path=ledger, now=NOW)
