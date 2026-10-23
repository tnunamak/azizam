"""Who may read a kept request: the maker only, with D52's rules for delegated credentials. Owners never can.

docs/design/REQUEST_RETENTION.md section 4 (the core floor), 13.2 (MakerContent is the only egress, built after the scope
check) and 16.1.4 (the cross-principal matrix). DECISIONS.md D51 (maker-only; the owner may narrow, never widen) and
D52 (+addendum: a delegated credential reads what its own App or MCP connection created; anything else needs the
privacy permission, which only the person grants; a row with no person belongs to its App).
"""
from __future__ import annotations

import pytest

from gateway import content_access as ca
from gateway.public_failure import PublicFailure

MAKER = "usr_maker"
OTHER = "usr_other"


def _row(**overrides):
    row = {"record_id": "kr_1", "kind": "request_history", "person_id": MAKER, "app_id": "app_one",
           "origin_kind": "app_key", "origin_id": "cred_one", "created_at": 1_000, "kept_until": 9_000,
           "parts_json": '{"accepted": {"body": {"model": "m"}}}', "omissions_json": "[]",
           "summary_json": '{"route": "/v1/chat/completions", "model": "m", "status": 200, "latency_ms": 5}',
           "request_id": "req_1"}
    row.update(overrides)
    return row


VIEWERS = {
    "maker session": ca.Viewer(person_id=MAKER, auth_kind="session"),
    "maker app key, same App": ca.Viewer(person_id=MAKER, app_id="app_one", auth_kind="app_key",
                                         credential_id="cred_one", key_hash="h"),
    "maker app key, another App": ca.Viewer(person_id=MAKER, app_id="app_two", auth_kind="app_key",
                                            credential_id="cred_two", key_hash="h2"),
    "maker app key with privacy permission": ca.Viewer(person_id=MAKER, app_id="app_two", auth_kind="app_key",
                                                       credential_id="cred_two", privacy_permission=True),
    "maker MCP grant, its connection": ca.Viewer(person_id=MAKER, auth_kind="mcp_grant", connection_id="conn_one"),
    "maker MCP grant, another connection": ca.Viewer(person_id=MAKER, auth_kind="mcp_grant", connection_id="conn_two"),
    "another member's session": ca.Viewer(person_id=OTHER, auth_kind="session"),
    "owner's session": ca.Viewer(person_id="usr_owner", is_owner=True, auth_kind="session"),
    "owner key on the maker's row": ca.Viewer(person_id=None, is_owner=True, auth_kind="app_key", owner_keys=frozenset({"k"})),
    "service key": ca.Viewer(auth_kind="service_key", app_id="app_svc"),
    "trusted address": ca.Viewer(auth_kind="trusted_address", trusted_address="10.0.0.5"),
    "automation key with no person": ca.Viewer(auth_kind="app_key", app_id="app_auto"),
}

# (viewer, row origin) -> may read. Origins: the creating App's key, the maker's session, the maker's grant.
EXPECTED_READ = {
    ("maker session", "app_key"): True,
    ("maker session", "session"): True,
    ("maker session", "mcp_grant"): True,
    ("maker app key, same App", "app_key"): True,
    ("maker app key, same App", "session"): False,
    ("maker app key, same App", "mcp_grant"): False,
    ("maker app key, another App", "app_key"): False,
    ("maker app key, another App", "session"): False,
    ("maker app key with privacy permission", "app_key"): True,
    ("maker app key with privacy permission", "session"): True,
    ("maker MCP grant, its connection", "mcp_grant"): True,
    ("maker MCP grant, its connection", "session"): False,
    ("maker MCP grant, another connection", "mcp_grant"): False,
    ("another member's session", "app_key"): False,
    ("another member's session", "session"): False,
    ("owner's session", "app_key"): False,
    ("owner's session", "session"): False,
    ("owner key on the maker's row", "app_key"): False,
    ("service key", "app_key"): False,
    ("trusted address", "app_key"): False,
    ("automation key with no person", "app_key"): False,
}

ORIGINS = {
    "app_key": {"origin_kind": "app_key", "origin_id": "cred_one", "app_id": "app_one"},
    "session": {"origin_kind": "session", "origin_id": None, "app_id": None},
    "mcp_grant": {"origin_kind": "mcp_grant", "origin_id": "conn_one", "app_id": None},
}


@pytest.mark.parametrize("viewer_name, origin", sorted(EXPECTED_READ))
def test_the_read_matrix_is_maker_only_and_follows_the_delegation_rules(viewer_name, origin):
    row = _row(**ORIGINS[origin])
    record = ca.request_history_record(row)
    access = ca.decide(VIEWERS[viewer_name], record, "read")
    assert isinstance(access, ca.Own) == EXPECTED_READ[(viewer_name, origin)]


@pytest.mark.parametrize("viewer_name", sorted(VIEWERS))
def test_no_viewer_but_the_maker_may_read_a_row_with_no_origin(viewer_name):
    """A legacy row with no recorded origin is readable through a grant only with the permission (fail closed)."""
    record = ca.request_history_record(_row(origin_kind="legacy", origin_id=None, app_id=None))
    access = ca.decide(VIEWERS[viewer_name], record, "read")
    if viewer_name in {"maker session", "maker app key with privacy permission"}:
        assert isinstance(access, ca.Own)
    else:
        assert isinstance(access, ca.Deny)


@pytest.mark.parametrize("viewer_name", ["maker session", "maker app key, same App", "owner's session",
                                         "another member's session", "maker app key with privacy permission"])
def test_delete_needs_the_maker_session_or_the_privacy_permission(viewer_name):
    record = ca.request_history_record(_row(**ORIGINS["app_key"]))
    access = ca.decide(VIEWERS[viewer_name], record, "delete")
    expected = viewer_name in {"maker session", "maker app key with privacy permission"}
    assert isinstance(access, ca.Own) == expected


def test_the_maker_content_projection_is_built_only_after_the_read_check_passes():
    viewer = VIEWERS["maker session"]
    content = ca.maker_content(viewer, _row(**ORIGINS["app_key"]))
    assert isinstance(content, ca.MakerContent)
    assert content.record_id == "kr_1"
    assert content.parts == {"accepted": {"body": {"model": "m"}}}
    assert content.summary["route"] == "/v1/chat/completions"


def test_the_projection_refuses_another_person_with_the_same_not_found_as_a_missing_row():
    with pytest.raises(PublicFailure) as refused:
        ca.maker_content(VIEWERS["owner's session"], _row(**ORIGINS["app_key"]))
    with pytest.raises(PublicFailure) as missing:
        ca.maker_content(VIEWERS["maker session"], _row(record_id="kr_gone", person_id=None))
    assert refused.value.status_code == 404 == missing.value.status_code
    assert str(refused.value) == str(missing.value)


def test_a_maker_content_cannot_be_built_by_hand():
    """Only maker_content, after the read check, builds the projection: a hand-built one is refused."""
    fields = {"record_id": "kr_1", "kind": "request_history", "created_at": 1, "kept_until": 2, "parts": {},
              "omissions": (), "summary": {}, "origin_kind": "app_key", "request_id": None}
    with pytest.raises(TypeError):
        ca.MakerContent(**fields)


def test_the_list_keeps_only_the_viewers_rows_and_is_filtered_before_any_page_is_cut():
    rows = [_row(record_id=f"kr_{index}", person_id=OTHER if index % 2 else MAKER) for index in range(6)]
    visible = ca.visible_maker_rows(VIEWERS["maker session"], rows)
    assert [row["record_id"] for row in visible] == ["kr_0", "kr_2", "kr_4"]
    assert ca.visible_maker_rows(VIEWERS["owner's session"], rows) == []
