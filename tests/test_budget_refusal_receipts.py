"""Spending-limit refusals name their limit on the receipt (design ask 10)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from gateway import admin_store
from tests.support.access import identity, plan_for  # noqa: E402
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


def _profile():
    """An App whose plans list typesafe. Plans have no caps of their own (design v4 §2.11)."""
    return identity(plan_for("family", paid=["typesafe"]))


def _spent(ledger: Path, usd: str, *, profile_id: str = "family", status: str = "settled", at: int = NOW - 60) -> None:
    ensure_ledger(ledger)
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO provider_spend (provider, profile_id, status, estimated_usd, actual_usd, cost_source, created_at) "
                     "VALUES ('typesafe', ?, ?, ?, ?, 'test', ?)", (profile_id, status, usd, usd if status == "settled" else None, at))


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
    _spent(tmp_path / "ledger.db", "2.00", profile_id="someone-else")
    refusal = _refusal(tmp_path, _profile(), _provider(spend_caps_usd={"daily": "2.00"}))
    assert refusal == {"code": "budget_exceeded", "limit": {"period": "daily", "scope": "provider"}}


def test_global_monthly_cap_names_monthly_global(tmp_path):
    _spent(tmp_path / "ledger.db", "4.00", profile_id="someone-else")
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
    receipt.set_refusal({"code": "budget_exceeded", "limit": {"period": "daily", "scope": "plan", "cap_usd": "1.00"},
                         "spent_usd": "0.99", "other_user": "usr_x"})
    client = receipt.to_dict("client")
    assert client["refusal"] == {"code": "budget_exceeded", "limit": {"period": "daily", "scope": "plan"}}
    assert "1.00" not in str(client) and "usr_x" not in str(client)
    assert receipt.to_dict("admin")["refusal"] == client["refusal"]


def test_client_projection_drops_untrusted_stored_refusal_values():
    stored = {"request_id": "r", "status": "failed",
              "refusal": {"code": "budget_exceeded", "limit": {"period": "weekly", "scope": "plan"}, "note": "x"}}
    assert serialize_receipt_for_audience(stored, "client")["refusal"] == {"code": "budget_exceeded", "limit": None}
    assert "refusal" not in serialize_receipt_for_audience({"request_id": "r", "refusal": {"code": "other"}}, "client")
    assert "refusal" not in RouteReceipt(protocol="p", intent="i").to_dict("client")
