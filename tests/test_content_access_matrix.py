"""Literal action table: OWNER_VISIBILITY_TESTS §2 / OV §3.1, c6968dc8 (v4.2).

The test table names independent fixture relations, never production helpers.
Revoked credentials fail authentication; replacement keys retain old aliases.
"""
from dataclasses import replace
from itertools import product

import pytest

from gateway.content_access import (
    ChatRecord, ContentOrigin, CreationRecord, DebugCaptureRecord, Deny, JobRecord,
    Own, PromptRecord, ReceiptRecord, RequestHistoryRecord, Share, Shared, Viewer,
    VoiceRecord, decide, operational, require,
)
from gateway.public_failure import PublicFailure

# Every column is transcribed separately, including the blank (Deny) cells.
MATRIX = {
    "creation": {"read": ("Own", "Shared"), "delete": ("Own",), "reuse": ("Own",), "share": ("Maker",), "link": ("Own", "Shared"), "cancel": ()},
    "prompt": {"read": ("ContentRead", "SharedPrompt"), "delete": ("Self",), "reuse": ("SelfContentRead",), "share": (), "link": (), "cancel": ()},
    "chat": {"read": ("ContentRead",), "delete": ("SelfAppOrKey",), "reuse": ("SelfAppOrKey",), "share": (), "link": (), "cancel": ()},
    "voice": {"read": ("Own",), "delete": ("Own",), "reuse": ("Own",), "share": (), "link": (), "cancel": ()},
    "receipt": {"read": ("Own",), "delete": (), "reuse": (), "share": (), "link": (), "cancel": ()},
    "job": {"read": ("Own",), "delete": (), "reuse": (), "share": (), "link": (), "cancel": ("Own",)},
    "request_history": {"read": ("ContentRead",), "delete": ("SelfPermission",), "reuse": ("SelfPermission",), "share": (), "link": (), "cancel": ()},
    "debug_capture": {"read": ("SelfPermission",), "delete": ("SelfPermission",), "reuse": (), "share": (), "link": (), "cancel": ()},
}
ACTIONS = ("read", "delete", "reuse", "share", "link", "cancel")
KEYS = frozenset({"app:a", "old:a"})
ACTORS = {
    "maker-session": Viewer("a", "a", owner_keys=KEYS, auth_kind="session"),
    "maker-key": Viewer("a", "a", owner_keys=KEYS, auth_kind="app_key", credential_id="key:a"),
    "mcp-grant-same-app": Viewer("a", "a", owner_keys=KEYS, auth_kind="mcp_grant", connection_id="connection:a"),
    "mcp-grant-other-app": Viewer("a", "surface:a", owner_keys=KEYS, auth_kind="mcp_grant", connection_id="connection:a"),
    "mcp-grant-missing-origin": Viewer("a", "a", owner_keys=KEYS, auth_kind="mcp_grant"),
    "mcp-grant-empty-origin": Viewer("a", "a", owner_keys=KEYS, auth_kind="mcp_grant", connection_id=""),
    "mcp-grant-different-person": Viewer("b", "a", owner_keys=KEYS, auth_kind="mcp_grant", connection_id="connection:a"),
    "trusted-address": Viewer(None, "a", owner_keys=KEYS, auth_kind="trusted_address", trusted_address="192.0.2.7"),
    "same-app-different-person": Viewer("b", "a", owner_keys=KEYS, auth_kind="app_key", credential_id="key:b"),
    "same-person-other-app-session": Viewer("a", "b", owner_keys=frozenset({"app:b"}), auth_kind="session"),
    "same-person-other-app-key": Viewer("a", "b", owner_keys=frozenset({"app:b"}), auth_kind="app_key", credential_id="key:other"),
    "same-person-appless-key": Viewer("a", key_hash="old:a", auth_kind="app_key", credential_id="key:legacy"),
    "service-app-key": Viewer(None, "a", owner_keys=KEYS, auth_kind="service_key", credential_id="key:service"),
    "personless-personal-key": Viewer(None, "a", owner_keys=KEYS, auth_kind="app_key", credential_id="key:personless"),
    "accountable-service-session": Viewer("c", "surface:c", owner_keys=KEYS, auth_kind="session"),
    "replacement-key-old-alias": Viewer(None, "a", key_hash="new:a", owner_keys=KEYS | {"new:a"}, auth_kind="app_key", credential_id="key:replacement"),
    "owner-other": Viewer("o", "o", owner_keys=frozenset({"app:o"}), is_owner=True, auth_kind="session"),
    "admin-other": Viewer("d", "d", owner_keys=frozenset({"app:d"}), auth_kind="session"),
    "recipient-session": Viewer("q", "q", owner_keys=frozenset({"app:q"}), auth_kind="session"),
    "recipient-key": Viewer("q", "q", owner_keys=frozenset({"app:q"}), auth_kind="app_key", credential_id="key:q"),
    "owner-recipient": Viewer("q", "q", owner_keys=frozenset({"app:q"}), is_owner=True, auth_kind="session"),
    "unrelated": Viewer("b", "b", owner_keys=frozenset({"app:b"}), auth_kind="session"),
    "bearer-holder-without-identity": Viewer(auth_kind="service_key"),
    "empty-identity": Viewer("", "", "", frozenset({""}), True, auth_kind="service_key"),
}
ORIGINS = (None, ContentOrigin("session"), ContentOrigin("app_key", credential_id="key:a"),
           ContentOrigin("app_key", credential_id="key:rotated"), ContentOrigin("app_key"),
           ContentOrigin("mcp_grant", connection_id="connection:a"),
           ContentOrigin("mcp_grant", connection_id="connection:b"), ContentOrigin("mcp_grant", connection_id=""))


def records(*, active=True, include_prompt=False, available=True, legacy=False, personless=False, origin=None):
    grant = Share("share:1", "asset:1", "a", "q", include_prompt, "poster:1", active)
    person = None if personless else "a"
    owners = frozenset({"old:a" if legacy else "app:a"})
    app = None if legacy else "a"
    return {
        "creation": CreationRecord("asset:1", owners, ("a",), shares=(grant,), available=available),
        "prompt": PromptRecord("asset:1", person, shares=(grant,), app_id=app, available=available, origin=origin),
        "chat": ChatRecord(person, app, "old:a", available=available, origin=origin),
        "voice": VoiceRecord(owners, available=available),
        "receipt": ReceiptRecord(owners, available=available),
        "job": JobRecord(owners, available=available),
        "request_history": RequestHistoryRecord(person, app_id=app, available=available, origin=origin),
        "debug_capture": DebugCaptureRecord(person, app_id=app, available=available, origin=origin),
    }


def oracle_relations(v, row):
    """Fixture facts for TD §2, expressed independently from decide's dispatch."""
    self_person = bool(v.person_id and getattr(row, "person_id", None) == v.person_id)
    own = bool((v.owner_keys - {""}) & (getattr(row, "owner_keys", frozenset()) - {""}))
    app_match = bool(v.app_id and getattr(row, "app_id", None) == v.app_id)
    origin = getattr(row, "origin", None)
    output = bool(origin and ((v.auth_kind == "app_key" and app_match and origin.auth_kind == "app_key" and origin.credential_id)
                             or (v.auth_kind == "mcp_grant" and origin.auth_kind == "mcp_grant" and v.connection_id and origin.connection_id == v.connection_id)))
    permission = v.auth_kind == "session" or v.privacy_permission
    content_read = ((self_person and (permission or output)) if getattr(row, "person_id", None) is not None else
                    v.person_id is None and v.auth_kind != "session" and app_match)
    share = next((s for s in getattr(row, "shares", ()) if s.active and s.share_id and s.maker_person_id
                  and v.person_id and s.recipient_person_id == v.person_id and
                  ((row.kind == "creation" and row.asset_id in (s.asset_id, s.poster_asset_id)) or
                   (row.kind == "prompt" and row.asset_id == s.asset_id and row.person_id == s.maker_person_id and s.include_prompt))), None)
    legacy_key = bool(v.key_hash and getattr(row, "owner_key_hash", None) == v.key_hash)
    return {"Own": own, "Maker": own and bool(v.person_id) and getattr(row, "maker_person_ids", ()) == (v.person_id,),
            "Self": self_person, "ContentRead": content_read,
            "SelfContentRead": self_person and content_read,
            "SelfAppOrKey": self_person and (app_match if getattr(row, "app_id", None) else legacy_key),
            "SelfPermission": self_person and permission,
            "Shared": bool(share), "SharedPrompt": bool(share) and permission}


@pytest.mark.parametrize("kind,action", tuple(product(MATRIX, ACTIONS)))
def test_every_kind_action_basis_cell(kind, action):
    for actor, viewer in ACTORS.items():
        for state, origin in product(product((False, True), repeat=7), ORIGINS):
            active, included, available, legacy, role, privacy, personless = state
            row = records(active=active, include_prompt=included, available=available, legacy=legacy, personless=personless, origin=origin)[kind]
            v = replace(viewer, is_owner=role, privacy_permission=privacy)
            relations = oracle_relations(v, row)
            allowed = [cell for cell in MATRIX[kind][action] if relations[cell]] if available else []
            expected = Own if any(cell not in {"Shared", "SharedPrompt"} for cell in allowed) else Shared if allowed else Deny
            access = decide(v, row, action)
            assert isinstance(access, expected), (kind, action, actor, state, origin)
            if isinstance(access, Shared):
                assert (access.share_id, access.include_prompt) == ("share:1", included)


def test_authentication_kind_is_required_and_duplicate_actors_are_distinct():
    with pytest.raises(TypeError):
        Viewer("a", "x")
    actors = [ACTORS[n] for n in ("trusted-address", "service-app-key", "personless-personal-key", "replacement-key-old-alias")]
    assert len(set(actors)) == 4


def test_h1_h2_debug_never_uses_own_output_or_app_only_authority():
    for name in ("maker-key", "mcp-grant-same-app", "service-app-key", "trusted-address"):
        for person in ("a", None):
            row = DebugCaptureRecord(person, "a", origin=ORIGINS[2])
            assert isinstance(decide(ACTORS[name], row, "read"), Deny)


def test_h3_history_mutations_need_delegated_permission_even_for_own_outputs():
    for name, origin in (("maker-key", ORIGINS[2]), ("mcp-grant-same-app", ORIGINS[5])):
        row = RequestHistoryRecord("a", "a", origin=origin)
        assert isinstance(decide(ACTORS[name], row, "read"), Own)
        for action in ("delete", "reuse"):
            assert isinstance(decide(ACTORS[name], row, action), Deny)
            assert isinstance(decide(replace(ACTORS[name], privacy_permission=True), row, action), Own)


def test_h4_h5_chat_mutations_require_self_and_app_or_exact_legacy_key():
    assert isinstance(decide(ACTORS["same-person-other-app-session"], ChatRecord("a", "a"), "delete"), Deny)
    v = ACTORS["same-person-appless-key"]
    for action in ("delete", "reuse"):
        assert isinstance(decide(v, ChatRecord("a", None, "old:a"), action), Own)
        assert isinstance(decide(v, ChatRecord("a", None, "other"), action), Deny)


def test_h6_personless_mutations_deny_while_continuation_uses_read():
    for kind in ("chat", "prompt", "request_history"):
        row = records(personless=True)[kind]
        for name in ("service-app-key", "trusted-address"):
            assert isinstance(decide(ACTORS[name], row, "read"), Own)
            for action in ("delete", "reuse"):
                assert isinstance(decide(ACTORS[name], row, action), Deny)


def test_h7_app_key_legacy_browser_and_mcp_rows_need_flag():
    v = ACTORS["maker-key"]
    for kind in ("prompt", "chat", "request_history"):
        for origin in (None, ORIGINS[1], ORIGINS[5]):
            row = records(origin=origin)[kind]
            assert isinstance(decide(v, row, "read"), Deny)
            assert isinstance(decide(replace(v, privacy_permission=True), row, "read"), Own)
        for origin in ORIGINS[2:4]:
            assert isinstance(decide(v, records(origin=origin)[kind], "read"), Own)


def test_maker_requires_own_and_exactly_one_maker():
    viewer = ACTORS["maker-session"]
    creation = records()["creation"]
    for makers in ((), ("a", "b"), ("a", "a"), ("b",)):
        assert isinstance(decide(viewer, replace(creation, maker_person_ids=makers), "share"), Deny)
    assert isinstance(decide(replace(viewer, owner_keys=frozenset()), creation, "share"), Deny)


def test_share_selects_only_its_asset_poster_and_maker_prompt_row():
    v = ACTORS["recipient-session"]
    row = records(include_prompt=True)["creation"]
    assert isinstance(decide(v, replace(row, asset_id="poster:1"), "read"), Shared)
    assert isinstance(decide(v, replace(row, asset_id="sibling:1"), "read"), Deny)
    prompt = records(include_prompt=True)["prompt"]
    for asset, person in (("poster:1", "a"), ("asset:1", "b"), ("sibling:1", "a")):
        assert isinstance(decide(v, replace(prompt, asset_id=asset, person_id=person), "read"), Deny)


def test_own_precedes_share_and_survives_revocation():
    viewer = replace(ACTORS["maker-session"], person_id="q")
    for active in (False, True):
        assert isinstance(decide(viewer, records(active=active)["creation"], "read"), Own)


def test_require_and_unknown_actions_fail_closed():
    for available in (False, True):
        with pytest.raises(PublicFailure) as caught:
            require(ACTORS["unrelated"], records(available=available)["creation"], "read")
        assert (caught.value.code, caught.value.status_code) == ("not_found", 404)
    for row in records().values():
        assert isinstance(decide(ACTORS["maker-session"], row, "not-an-action"), Deny)
    assert operational(Viewer(is_owner=True, auth_kind="session"))
    assert not operational(Viewer(auth_kind="session"))
