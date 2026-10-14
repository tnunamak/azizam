"""Regressions for exact OIDC grants and bounded pending login state."""

import pytest

from gateway import signin_rules
from gateway.admin_oidc import (
    AdminRole, SessionError, access_names_from_oidc_claims,
    bootstrap_admin_role, record_state,
)
from test_admin_oidc import make_config


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
