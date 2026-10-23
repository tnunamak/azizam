"""Owning an App grants no content (D51). Receipts, jobs, creations and voices are read by their maker.

An App-less row is read by its App's own credential and by no person (D52). These cases name the owner,
an admin, two members, an App key of a service App and an MCP grant, so each actor's decision is pinned.
"""
from __future__ import annotations

import pytest

from gateway.content_access import (
    ContentOrigin, CreationRecord, Deny, JobRecord, Own, ReceiptRecord, Shared, Share, VoiceRecord,
    Viewer, decide, makers_of,
)

SERVICE = "app:svc"  # a service App owned by the gateway owner
OWNER = Viewer("owner", owner_keys=frozenset({SERVICE, "user:owner"}), is_owner=True, auth_kind="session")
ADMIN = Viewer("admin", owner_keys=frozenset({"user:admin"}), auth_kind="session")
MEMBER_A = Viewer("alice", owner_keys=frozenset({"app:alice-app", "user:alice"}), auth_kind="session")
MEMBER_B = Viewer("bob", owner_keys=frozenset({"app:bob-app", "user:bob"}), auth_kind="session")
SERVICE_KEY = Viewer(None, "svc", owner_keys=frozenset({SERVICE}), auth_kind="app_key", credential_id="key:svc")
OTHER_KEY = Viewer(None, "other", owner_keys=frozenset({"app:other"}), auth_kind="app_key", credential_id="key:other")
GRANT = Viewer("alice", "alice-app", owner_keys=frozenset({"app:alice-app"}), auth_kind="mcp_grant", connection_id="conn:1")
ACTORS = {
    "owner": OWNER, "admin": ADMIN, "member-a": MEMBER_A, "member-b": MEMBER_B,
    "service-app-key": SERVICE_KEY, "other-app-key": OTHER_KEY, "mcp-grant": GRANT,
}


def member_row(kind: str, *, origin: ContentOrigin | None = None):
    """A row alice made through the owner's service App."""
    if kind == "receipt":
        return ReceiptRecord(person_id="alice", app_id="svc", origin=origin)
    if kind == "job":
        return JobRecord(person_id="alice", app_id="svc", origin=origin)
    if kind == "voice":
        return VoiceRecord(person_id="alice", app_id="svc", origin=origin)
    return CreationRecord("asset:1", frozenset({SERVICE}), ("alice",), app_id="svc", origin=origin)


@pytest.mark.parametrize("kind", ["receipt", "job", "voice", "creation"])
def test_owner_admin_and_other_members_and_keys_read_no_member_row_in_a_service_app(kind):
    row = member_row(kind)
    for name in ("owner", "admin", "member-b", "service-app-key", "other-app-key"):
        assert isinstance(decide(ACTORS[name], row, "read"), Deny), (kind, name)


@pytest.mark.parametrize("kind", ["receipt", "job", "voice", "creation"])
def test_the_maker_reads_her_own_row_in_a_service_app(kind):
    assert isinstance(decide(MEMBER_A, member_row(kind), "read"), Own)


def test_a_grant_reads_a_member_row_only_from_its_own_connection():
    assert isinstance(decide(GRANT, member_row("receipt", origin=ContentOrigin("mcp_grant", connection_id="conn:1")), "read"), Own)
    assert isinstance(decide(GRANT, member_row("receipt", origin=ContentOrigin("mcp_grant", connection_id="conn:2")), "read"), Deny)
    # A browser-session row needs the maker's permission for a grant (D52), so the grant is refused here.
    assert isinstance(decide(GRANT, member_row("receipt", origin=ContentOrigin("session")), "read"), Deny)


def test_a_person_less_row_is_read_only_by_its_own_app_credential():
    row = ReceiptRecord(person_id=None, app_id="svc")
    assert isinstance(decide(SERVICE_KEY, row, "read"), Own)
    for name in ("owner", "admin", "member-a", "member-b", "other-app-key", "mcp-grant"):
        assert isinstance(decide(ACTORS[name], row, "read"), Deny), name


def test_the_owner_of_a_personal_app_still_reads_its_own_rows():
    row = JobRecord(person_id="owner", app_id="owner-app")
    assert isinstance(decide(OWNER, row, "read"), Own)
    assert isinstance(decide(OWNER, row, "cancel"), Own)
    assert isinstance(decide(MEMBER_A, row, "read"), Deny)


def test_only_the_maker_may_cancel_a_job_and_nobody_deletes_a_receipt():
    job = member_row("job")
    assert isinstance(decide(OWNER, job, "cancel"), Deny)
    assert isinstance(decide(MEMBER_A, job, "cancel"), Own)
    assert isinstance(decide(MEMBER_A, member_row("receipt"), "delete"), Deny)


def test_the_maker_shares_her_creation_and_no_owner_key_can():
    creation = member_row("creation")
    assert isinstance(decide(MEMBER_A, creation, "share"), Own)
    assert isinstance(decide(OWNER, creation, "share"), Deny)
    assert isinstance(decide(SERVICE_KEY, creation, "share"), Deny)
    shared = CreationRecord("asset:1", frozenset({SERVICE}), ("alice",), app_id="svc",
                            shares=(Share("share:1", "asset:1", "alice", "bob"),))
    assert isinstance(decide(MEMBER_B, shared, "read"), Shared)
    assert isinstance(decide(OWNER, shared, "read"), Deny)


def test_a_creation_with_two_makers_is_read_by_neither_of_them():
    row = CreationRecord("asset:2", frozenset({SERVICE}), ("alice", "bob"), app_id="svc")
    assert isinstance(decide(MEMBER_A, row, "read"), Deny)
    assert isinstance(decide(MEMBER_B, row, "read"), Deny)


def test_makers_of_prefers_the_recorded_maker_and_never_takes_a_service_app_owner():
    assert makers_of(["alice"], app_person="owner") == ("alice",)
    assert makers_of([], app_person="owner") == ("owner",)
    # A service App names no person, so an App row with no recorded maker stays person-less.
    assert makers_of([], app_person=None, key_persons=()) == ()
    assert makers_of(["", ""], app_person=None, key_persons=["bob", "bob"]) == ("bob",)
    assert makers_of([], key_persons=["alice", "bob"]) == ("alice", "bob")
