"""Regressions for exact OIDC grants and bounded pending login state."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway import signin_rules
from gateway.admin_oidc import (
    AdminRole, SessionError, StateRecord, access_names_from_oidc_claims,
    bootstrap_admin_role, record_state,
)
from gateway.oidc_browser_flow import begin_browser_login
from test_admin_oidc import make_config


def _authorization_query(**flow) -> tuple[dict[str, list[str]], StateRecord]:
    """The query the provider would receive, and the pending flow Azizam stored for it."""
    store: dict[str, StateRecord] = {}
    response = begin_browser_login(make_config(), store, return_to="/", **flow)
    [record] = store.values()
    return parse_qs(urlsplit(response.headers["location"]).query), record


def admin_by_group(rule_group, claim_group):
    """Does a rule that makes `rule_group` administrators fire for a person who sent `claim_group`?"""
    rules = signin_rules.parse_rules({"rules": [
        {"id": "r", "field": "group", "values": [rule_group], "admin": True}]})
    claims = signin_rules.Claims(email="a@example.com", groups=(claim_group,))
    return signin_rules.evaluate(rules, claims).admin


@pytest.mark.parametrize("group", [
    "/untrusted/admins", "/trusted/ADMINS", "/trusted/ admins",
    "/trusted/admin-users", "/trusted/admin_users",
])
def test_sec07_group_aliases_never_authorize_distinct_groups(group):
    assert admin_by_group("/trusted/admins", group) is False
    assert admin_by_group("/trusted/admin users", group) is False


def test_sec07_short_configured_group_only_matches_root_full_path():
    assert admin_by_group("admins", "/admins") is True
    assert admin_by_group("admins", "/untrusted/admins") is False


def test_sec07_exact_group_ids_are_supported():
    assert admin_by_group("group-id-123", "group-id-123") is True
    assert admin_by_group("group-id-123", "GROUP-ID-123") is False


def test_sec07_profile_claims_have_no_authorization_aliases():
    assert access_names_from_oidc_claims(
        {"groups": ["/trusted/Admin Users"]}, client_id="azizam",
    )["oidc_group"] == ("/trusted/Admin Users",)


def test_sec06_existing_email_owner_cannot_transfer_to_new_identity():
    config = make_config(admin_emails=frozenset({"owner@example.com"}))
    assert bootstrap_admin_role(
        "owner@example.com", config=config,
        existing_roles={"owner@example.com": AdminRole.OWNER},
    ) == AdminRole.NONE


def test_sec13_abandoned_login_flows_are_pruned_on_insert():
    store = {}
    record_state(store, state="abandoned", nonce="n", pkce_verifier="v", now=0)
    record_state(store, state="fresh", nonce="n", pkce_verifier="v", now=601)
    assert set(store) == {"fresh"}


def test_sec13_pending_login_flows_are_bounded_without_evicting_live_flow():
    store = {}
    record_state(store, state="first", nonce="n", pkce_verifier="v", now=0, max_pending=1)
    with pytest.raises(SessionError, match="too many pending"):
        record_state(store, state="second", nonce="n", pkce_verifier="v", now=1, max_pending=1)
    assert set(store) == {"first"}


def test_a_tile_flow_is_fresh_even_when_the_caller_does_not_say_so():
    """A flow that starts from a remembered tile asks for a fresh sign-in by itself (A2 review L2)."""
    query, record = _authorization_query(tile_person_id="person_1")

    assert query["prompt"] == ["login"] and query["max_age"] == ["0"]
    assert record.fresh is True and record.tile_person_id == "person_1"


@pytest.mark.parametrize("flow", [{"fresh": True}, {"reauth": True}], ids=["sign-in-page", "step-up"])
def test_a_fresh_or_step_up_sign_in_asks_for_its_auth_time_as_an_essential_claim(flow):
    """The claims request parameter is standard OIDC Core 5.5. A provider without it ignores it, and one with it
    returns auth_time, which both kinds of flow must have to decide anything."""
    query, _record = _authorization_query(**flow)

    assert json.loads(query["claims"][0]) == {"id_token": {"auth_time": {"essential": True}}}


def test_a_plain_flow_asks_for_nothing_extra():
    query, record = _authorization_query()

    assert "prompt" not in query and "claims" not in query and record.fresh is False
