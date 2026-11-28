"""Compare the reviewer oracle on its original small exhaustive identity domain."""
from itertools import product
from types import SimpleNamespace

import pytest

from gateway.content_access import ChatRecord, ContentOrigin, CreationRecord, DebugCaptureRecord, Deny, JobRecord, PromptRecord, ReceiptRecord, RequestHistoryRecord, Share, Viewer, VoiceRecord, decide
from tests.support.owner_visibility_td_oracle import expected

V = (None, "", "a", "b")
CONN = (None, "", "c1", "c2")


@pytest.mark.parametrize("kind,action", tuple(product(("prompt", "chat", "request_history", "debug_capture"), ("read", "delete", "reuse", "share", "link", "cancel"))))
def test_reviewer_td_oracle_comparisons(kind, action):
    constructors = {"prompt": PromptRecord, "chat": ChatRecord, "request_history": RequestHistoryRecord, "debug_capture": DebugCaptureRecord}
    for person, app, delegation, conn, privacy, key_hash in product(V, V, (None, "app_key", "mcp_grant"), CONN, (False, True), (None, "k")):
        if (delegation is None and not person) or person == "" or (delegation != "mcp_grant" and conn) or (delegation == "mcp_grant" and not conn):
            continue
        viewer = Viewer(person, app, key_hash, privacy_permission=privacy, auth_kind=delegation or "session", connection_id=conn)
        td_viewer = SimpleNamespace(person_id=person, app_id=app, key_hash=key_hash, delegation=delegation, connection_id=conn, privacy_permission=privacy)
        for rp, ra, rc in product(V, V, CONN):
            for origin_label in (("n/a",) if rc else ("app_key_attributed", "legacy")):
                origin = ContentOrigin("mcp_grant", connection_id=rc) if rc else ContentOrigin("app_key", credential_id="key:seeded") if origin_label == "app_key_attributed" else None
                fields = dict(person_id=rp, app_id=ra, origin=origin)
                if kind == "prompt":
                    fields["asset_id"] = "asset:1"
                if kind == "chat":
                    fields["owner_key_hash"] = "k"
                row = constructors[kind](**fields)
                td_row = SimpleNamespace(kind=kind, person_id=rp, app_id=ra, connection_id=rc, owner_key_hash="k", available=True, shares=())
                got = not isinstance(decide(viewer, row, action), Deny)
                assert got == bool(expected(td_viewer, td_row, action, origin_label)), (kind, action, person, app, delegation, conn, privacy, rp, ra, rc, origin_label)


@pytest.mark.parametrize("kind,action", tuple(product(("creation", "voice", "receipt", "job"), ("read", "delete", "reuse", "share", "link", "cancel"))))
def test_reviewer_td_oracle_principal_and_share_comparisons(kind, action):
    share = Share("s1", "asset:1", "a", "b", True, "poster:1")
    key_sets = (frozenset(), frozenset({"app:a"}), frozenset({""}))
    for person, app, auth_kind, conn, privacy, keys, key_hash in product(V, V, ("session", "app_key", "mcp_grant"), CONN, (False, True), key_sets, (None, "k")):
        if (auth_kind == "session" and not person) or person == "" or (auth_kind != "mcp_grant" and conn) or (auth_kind == "mcp_grant" and not conn):
            continue
        viewer = Viewer(person, app, key_hash, keys, auth_kind=auth_kind, connection_id=conn, privacy_permission=privacy)
        td_viewer = SimpleNamespace(person_id=person, app_id=app, key_hash=key_hash, owner_keys=keys, delegation=None if auth_kind == "session" else auth_kind, connection_id=conn, privacy_permission=privacy)
        for owners in key_sets:
            rows = {"voice": [VoiceRecord(owners)], "receipt": [ReceiptRecord(owners)], "job": [JobRecord(owners)],
                    "creation": [CreationRecord("asset:1", owners, makers, shares=(share,)) for makers in ((), ("a",), ("a", "b"))]}
            for row in rows[kind]:
                assert (not isinstance(decide(viewer, row, action), Deny)) == bool(expected(td_viewer, row, action, "legacy")), (kind, action, viewer, row)


@pytest.mark.parametrize("kind,action", tuple(product(
    ("prompt", "chat", "request_history", "debug_capture", "creation", "voice", "receipt", "job"),
    ("read", "delete", "reuse", "share", "link", "cancel", "bogus"))))
def test_round2_independent_oracle(kind, action):
    from tests.support.td_oracle_r2 import expected as expected_r2
    from gateway.content_access import Shared
    origins = (None, ContentOrigin("session"), ContentOrigin("app_key", credential_id="cred1"),
               ContentOrigin("app_key", credential_id="cred2"), ContentOrigin("app_key"),
               ContentOrigin("app_key", credential_id=""), ContentOrigin("mcp_grant", connection_id="c1"),
               ContentOrigin("mcp_grant", connection_id="c2"), ContentOrigin("mcp_grant", connection_id=""),
               ContentOrigin("mcp_grant"), ContentOrigin("mcp_grant", credential_id="cred1"))
    share = Share("s1", "asset:1", "a", "b", True, "poster:1")
    key_sets = (frozenset(), frozenset({""}), frozenset({"app:a"}), frozenset({"app:b", "user:a"}))
    rows = []
    if kind in {"prompt", "chat", "request_history", "debug_capture"}:
        for person, app, origin, available in product(V, V, origins, (True, False)):
            if kind == "prompt":
                rows.extend(PromptRecord(asset, person, shares=shares, app_id=app, available=available, origin=origin)
                            for asset, shares in product(("asset:1", "poster:1"), ((), (share,))))
            elif kind == "chat":
                rows.extend(ChatRecord(person, app, key, available=available, origin=origin) for key in (None, "", "k"))
            else:
                rows.append((RequestHistoryRecord if kind == "request_history" else DebugCaptureRecord)(person, app, available, origin))
    else:
        for keys, available in product(key_sets, (True, False)):
            if kind == "creation":
                rows.extend(CreationRecord(asset, keys, makers, shares=shares, available=available)
                            for asset, makers, shares in product(("asset:1", "poster:1", "x"), ((), ("a",), ("b",), ("a", "b"), ("",)), ((), (share,))))
            else:
                rows.append({"voice": VoiceRecord, "receipt": ReceiptRecord, "job": JobRecord}[kind](keys, available=available))
    compared = 0
    for auth_kind, person, app, privacy, owner in product(
            ("session", "app_key", "mcp_grant", "trusted_address", "service_key"),
            (None, "a", "b"), (None, "a", "b"), (False, True), (False, True)):
        if auth_kind in {"trusted_address", "service_key"} and person is not None:
            continue  # invalid authentication states are covered by builder tests
        viewer = Viewer(person, app, "k", frozenset({"", "app:a"}), owner, privacy,
                        auth_kind=auth_kind, connection_id="c1" if auth_kind == "mcp_grant" else None,
                        credential_id="cred1" if auth_kind in {"app_key", "service_key"} else None)
        for row in rows:
            actual = decide(viewer, row, action)
            label = "deny" if isinstance(actual, Deny) else "shared" if isinstance(actual, Shared) else "own"
            assert label == expected_r2(viewer, row, action, False), (viewer, row, action)
            compared += 1
    print(f"R2 oracle {kind}:{action}: {compared} comparisons; zero divergences")
