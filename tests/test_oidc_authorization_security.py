"""Regressions for exact OIDC grants and bounded pending login state."""

import pytest

from gateway.admin_oidc import (
    AdminRole, SessionError, access_names_from_oidc_claims,
    admin_role_from_oidc_claims, bootstrap_admin_role, record_state,
)
from test_admin_oidc import make_config


@pytest.mark.parametrize("group", [
    "/untrusted/admins", "/trusted/ADMINS", "/trusted/ admins",
    "/trusted/admin-users", "/trusted/admin_users",
])
def test_sec07_group_aliases_never_authorize_distinct_groups(group):
    config = make_config(admin_groups=frozenset({"/trusted/admins", "/trusted/admin users"}))
    assert admin_role_from_oidc_claims({"groups": [group]}, config=config) == AdminRole.NONE


def test_sec07_short_configured_group_only_matches_root_full_path():
    config = make_config(admin_groups=frozenset({"admins"}))
    assert admin_role_from_oidc_claims({"groups": ["/admins"]}, config=config) == AdminRole.ADMIN
    assert admin_role_from_oidc_claims({"groups": ["/untrusted/admins"]}, config=config) == AdminRole.NONE


def test_sec07_exact_group_ids_are_supported():
    config = make_config(admin_groups=frozenset({"group-id-123"}))
    assert admin_role_from_oidc_claims({"groups": ["group-id-123"]}, config=config) == AdminRole.ADMIN
    assert admin_role_from_oidc_claims({"groups": ["GROUP-ID-123"]}, config=config) == AdminRole.NONE


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
