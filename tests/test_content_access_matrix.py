"""Independent oracle for every cell of OWNER_VISIBILITY v3 section 3.1.

Actor labels describe trusted identities, not authentication acceptance. Revoked
credentials cannot authenticate; their hashes remain readable historical aliases.
"""
from dataclasses import replace
from itertools import product

import pytest

from gateway.content_access import (
    ChatRecord, CreationRecord, Deny, JobRecord, Own, PromptRecord,
    ReceiptRecord, RequestHistoryRecord, DebugCaptureRecord, Share, Shared, Viewer, VoiceRecord, decide, operational, require,
)
from gateway.public_failure import PublicFailure

# Transcribed from the design, independently of production control flow.
MATRIX = {
    "creation": {"read": {"Own", "Shared"}, "delete": {"Own"}, "reuse": {"Own"},
                 "share": {"Maker"}, "link": {"Own", "Shared"}},
    "prompt": {"read": {"MakerContent", "SharedPrompt"}, "delete": {"MakerContent"}, "reuse": {"MakerContent"}},
    "chat": {"read": {"MakerContent"}, "delete": {"MakerContent"}, "reuse": {"MakerContent"}},
    "voice": {"read": {"Own"}, "delete": {"Own"}, "reuse": {"Own"}},
    "receipt": {"read": {"Own"}},
    "job": {"read": {"Own"}, "cancel": {"Own"}},
    "request_history": {"read": {"MakerContent"}, "delete": {"MakerContent"}, "reuse": {"MakerContent"}},
    "debug_capture": {"read": {"MakerContent"}, "delete": {"MakerContent"}},
}
ACTIONS = ("read", "delete", "reuse", "share", "link", "cancel")
KEYS = frozenset({"app:a", "old:a"})
ACTORS = {
    "maker-session": (Viewer("a", "a", owner_keys=KEYS), {"Own", "Self", "SameApp", "Session", "Maker"}),
    "maker-key": (Viewer("a", "a", owner_keys=KEYS, delegated=True), {"Own", "Self", "SameApp", "Maker"}),
    "mcp-grant-own-app": (Viewer("a", "a", owner_keys=KEYS, delegated=True), {"Own", "Self", "SameApp", "Maker"}),
    "mcp-grant-other-app": (Viewer("a", "surface:a", owner_keys=KEYS, delegated=True), {"Own", "Self", "Maker"}),
    "trusted-address": (Viewer(None, "a", owner_keys=KEYS, delegated=True), {"Own", "AppMaker"}),
    "same-app-different-person": (Viewer("b", "a", owner_keys=KEYS, delegated=True), {"Own"}),
    "same-person-other-app-session": (Viewer("a", "b", owner_keys=frozenset({"app:b"})), {"Self", "Session"}),
    "same-person-other-app-key": (Viewer("a", "b", owner_keys=frozenset({"app:b"}), delegated=True), {"Self"}),
    "same-person-appless-key": (Viewer("a", key_hash="old:a", delegated=True), {"Self"}),
    "service-app-key": (Viewer(None, "a", owner_keys=KEYS, delegated=True), {"Own", "AppMaker"}),
    "personless-personal-key": (Viewer(None, "a", owner_keys=KEYS, delegated=True), {"Own", "AppMaker"}),
    "accountable-service-session": (Viewer("c", "surface:c", owner_keys=KEYS), {"Own", "Session"}),
    "replacement-key-old-alias": (Viewer(None, "a", owner_keys=KEYS, delegated=True), {"Own", "AppMaker"}),
    "owner-other": (Viewer("o", "o", owner_keys=frozenset({"app:o"}), is_owner=True), {"Session"}),
    "admin-other": (Viewer("d", "d", owner_keys=frozenset({"app:d"})), {"Session"}),
    "recipient-session": (Viewer("q", "q", owner_keys=frozenset({"app:q"})), {"Shared", "Session"}),
    "recipient-key": (Viewer("q", "q", owner_keys=frozenset({"app:q"}), delegated=True), {"Shared"}),
    "owner-recipient": (Viewer("q", "q", owner_keys=frozenset({"app:q"}), is_owner=True), {"Shared", "Session"}),
    "unrelated": (Viewer("b", "b", owner_keys=frozenset({"app:b"})), {"Session"}),
    "bearer-holder-without-identity": (Viewer(), set()),
    "empty-identity": (Viewer("", "", "", frozenset({""}), True), set()),
}


def records(*, active=True, include_prompt=False, available=True, legacy=False, personless=False):
    grant = Share("share:1", "asset:1", "a", "q", include_prompt, "poster:1", active)
    person = None if personless else "a"
    owners = frozenset({"old:a" if legacy else "app:a"})
    return {
        "creation": CreationRecord("asset:1", owners, ("a",), shares=(grant,), available=available),
        "prompt": PromptRecord("asset:1", person, shares=(grant,), app_id="a", available=available),
        "chat": ChatRecord(person, "a", available=available),
        "voice": VoiceRecord(owners, available=available),
        "receipt": ReceiptRecord(owners, available=available),
        "job": JobRecord(owners, available=available),
        "request_history": RequestHistoryRecord(person, app_id="a", available=available),
        "debug_capture": DebugCaptureRecord(person, app_id="a", available=available),
    }


@pytest.mark.parametrize("kind,action", tuple(product(MATRIX, ACTIONS)))
def test_every_kind_action_basis_cell(kind, action):
    for actor, (viewer, bases) in ACTORS.items():
        for active, include_prompt, available, legacy, role, privacy, personless in product((False, True), repeat=7):
            basis = set(bases)
            if ((personless and "AppMaker" in basis) or
                    (not personless and "Self" in basis and
                     ("Session" in basis or "SameApp" in basis or privacy))):
                basis.add("MakerContent")
            if not active:
                basis.discard("Shared")
            if ("Shared" in basis and include_prompt and not personless
                    and ("Session" in basis or privacy)):
                basis.add("SharedPrompt")
            allowed = available and bool(MATRIX[kind].get(action, set()) & basis)
            access = decide(replace(viewer, is_owner=role, privacy_permission=privacy), records(
                active=active, include_prompt=include_prompt, available=available, legacy=legacy,
                personless=personless)[kind], action)
            own_basis = MATRIX[kind].get(action, set()) & basis & {"Own", "Maker", "MakerContent"}
            expected = (Own if own_basis else Shared) if allowed else Deny
            assert isinstance(access, expected), (kind, action, actor, active, include_prompt, available, legacy, role, privacy, personless)
            if isinstance(access, Shared):
                assert (access.share_id, access.include_prompt) == ("share:1", include_prompt)


def test_maker_requires_own_and_exactly_one_maker():
    viewer = ACTORS["maker-session"][0]
    creation = records()["creation"]
    for makers in ((), ("a", "b"), ("a", "a"), ("b",)):
        assert isinstance(decide(viewer, replace(creation, maker_person_ids=makers), "share"), Deny)
    assert isinstance(decide(replace(viewer, owner_keys=frozenset()), creation, "share"), Deny)


def test_share_selects_only_its_asset_poster_and_maker_prompt_row():
    viewer = replace(ACTORS["recipient-session"][0], privacy_permission=True)
    creation = records(include_prompt=True)["creation"]
    assert isinstance(decide(viewer, replace(creation, asset_id="poster:1"), "read"), Shared)
    assert isinstance(decide(viewer, replace(creation, asset_id="sibling:1"), "read"), Deny)
    prompt = records(include_prompt=True)["prompt"]
    for asset, person in (("poster:1", "a"), ("asset:1", "b"), ("sibling:1", "a")):
        assert isinstance(decide(viewer, replace(prompt, asset_id=asset, person_id=person), "read"), Deny)


def test_own_precedes_share_and_survives_its_revocation():
    viewer = replace(ACTORS["maker-session"][0], person_id="q")
    for active in (False, True):
        assert isinstance(decide(viewer, records(active=active)["creation"], "read"), Own)


def test_null_self_never_matches_and_app_maker_needs_exact_nonnull_app():
    app = Viewer(None, "a", delegated=True)
    for constructor in (PromptRecord, ChatRecord, RequestHistoryRecord, DebugCaptureRecord):
        own = constructor(None, app_id="a") if constructor is not PromptRecord else constructor("asset:1", None, app_id="a")
        assert isinstance(decide(app, own, "read"), Own)
        assert isinstance(decide(replace(app, app_id="b"), own, "read"), Deny)
        assert isinstance(decide(replace(app, person_id="a", privacy_permission=True), own, "read"), Deny)
        assert isinstance(decide(Viewer(key_hash="old:a", delegated=True), replace(own, app_id=None), "read"), Deny)
    row = ChatRecord("a", "a")
    session = Viewer("a", "b")
    key = Viewer("a", "b", delegated=True)
    assert isinstance(decide(session, row, "reuse"), Own)
    assert isinstance(decide(key, row, "read"), Deny)
    assert isinstance(decide(replace(key, privacy_permission=True), row, "read"), Own)
    assert isinstance(decide(replace(key, app_id="a"), row, "reuse"), Own)
    assert isinstance(decide(Viewer(), ChatRecord(None), "read"), Deny)


def test_unresolved_records_and_unknown_actions_deny():
    viewer = ACTORS["maker-session"][0]
    for record in (CreationRecord("a"), VoiceRecord(), ReceiptRecord(), JobRecord()):
        for action in ACTIONS:
            assert isinstance(decide(viewer, record, action), Deny)
    for record in records().values():
        assert isinstance(decide(viewer, record, "not-an-action"), Deny)


def test_require_has_the_same_not_found_for_unavailable_and_foreign():
    viewer = ACTORS["unrelated"][0]
    errors = []
    for available in (False, True):
        with pytest.raises(PublicFailure) as caught:
            require(viewer, records(available=available)["creation"], "read")
        errors.append((caught.value.code, caught.value.status_code))
    assert errors == [("not_found", 404)] * 2
    assert isinstance(require(ACTORS["maker-session"][0], records()["creation"], "read"), Own)


def test_only_operational_reads_owner_role():
    assert operational(Viewer(is_owner=True))
    assert not operational(Viewer())
