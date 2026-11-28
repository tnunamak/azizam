"""A healthy subscription is not reported as broken: readiness of a connection-backed service comes from its
connection (not from a stored key), the catalog says a plan model is included in the subscription, and an
access plan summary names a subscription by its label, not by its connection id."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import models_catalog, provider_admin, provider_connections  # noqa: E402
from gateway.access_plans import plan_summary  # noqa: E402
from gateway.subscription_store import NEEDS_RECONNECT, SubscriptionStore, store_path  # noqa: E402
from tests.support.access import plan  # noqa: E402

CONFIG = {
    "provider_id": "chatgpt", "remote_provider": True, "base_url": "https://example.test/v1",
    "credential_source": {"kind": "connection", "connection_id": "chatgpt-owner"},
    "request_protocol": "openai-responses-plan", "billing_mode": "plan", "cost_class": "plan", "models": [],
}


def _store(tmp_path: Path) -> SubscriptionStore:
    return SubscriptionStore(lambda: store_path(tmp_path / "admin.db"), secrets_path=lambda: tmp_path / "admin.db")


def _connect(store: SubscriptionStore) -> None:
    store.save_connection(kind="chatgpt", connected_by="usr_o", client_id="c", host_id="h", tokens=None,
                          origin="https://example.test", scopes=[], email=None, subject=None, session=None)


def _projected(tmp_path: Path) -> dict:
    service = {"id": "chatgpt-plan", "name": "ChatGPT plan", "enabled": True, "config": CONFIG}
    return provider_admin._project_provider("chatgpt", service, {}, {}, True, {}, tmp_path / "admin.db", {})


def test_connected_subscription_needs_no_key_and_is_reachable(tmp_path):
    _connect(_store(tmp_path))
    view = _projected(tmp_path)
    assert view["key_status"]["state"] == "connection"
    assert view["key_status"]["connection_status"] == "connected"
    assert view["reachable"] is True


def test_needs_reconnect_subscription_is_down_not_keyless(tmp_path):
    store = _store(tmp_path)
    _connect(store)
    store.invalidate("chatgpt", status=NEEDS_RECONNECT)
    view = _projected(tmp_path)
    assert view["key_status"]["state"] == "connection"
    assert view["key_status"]["connection_status"] == "needs_reconnect"
    assert view["reachable"] is False


def test_unconnected_subscription_is_off_not_keyless(tmp_path):
    view = _projected(tmp_path)
    assert view["key_status"]["state"] == "connection"
    assert view["key_status"]["connection_status"] == "not_connected"
    assert view["reachable"] is None


def test_catalog_cost_of_a_plan_model_names_the_subscription():
    service = {"id": "chatgpt-plan", "config": CONFIG}
    cost = models_catalog._cost(service, {"id": "gpt-6"})
    assert cost == {"kind": "included", "price": None, "plan_label": "ChatGPT"}


def test_catalog_cost_of_a_pay_per_token_model_is_unchanged():
    service = {"id": "x", "config": {"remote_provider": True, "billing_mode": "metered"}}
    assert models_catalog._cost(service, {"id": "m"}) == {"kind": "paid", "price": None}


def test_plan_summary_names_the_subscription_by_label():
    granted = plan(connections=frozenset({"chatgpt-owner"}))
    assert "Subscriptions: ChatGPT." in plan_summary(granted, {}, {"chatgpt-owner": "ChatGPT"})
    assert "chatgpt-owner" not in plan_summary(granted, {}, {"chatgpt-owner": "ChatGPT"})


def test_plan_summary_falls_back_to_the_id_without_a_label():
    granted = plan(connections=frozenset({"chatgpt-owner"}))
    assert "Subscriptions: chatgpt-owner." in plan_summary(granted, {})


def test_connection_labels_come_from_the_kind_display_name(tmp_path):
    _connect(_store(tmp_path))
    assert provider_connections.connection_labels(tmp_path / "admin.db") == {"chatgpt-owner": "ChatGPT", "claude-owner": "Claude"}


@pytest.fixture
def stored_plan(tmp_path):
    from gateway import admin_store
    from tests.support.access import store_plan
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    return path, store_plan("Household", path=path, connections=["chatgpt-owner"])


def test_every_caller_of_plan_summary_passes_the_label(stored_plan):
    from gateway import accounts_api, admin_runtime, admin_store, user_runtime
    path, record = stored_plan
    wanted = "Subscriptions: ChatGPT."
    assert wanted in accounts_api._plan_choice(record["id"], path, {})["summary"]
    assert wanted in admin_runtime._plan_record(record, {}, path)["summary"]
    app = {"access_plan": record["id"], "owner_user_id": "usr_x"}
    assert wanted in user_runtime._plan_summary(app, path=path)["summary"]
    assert "chatgpt-owner" not in user_runtime._plan_summary(app, path=path)["summary"]
