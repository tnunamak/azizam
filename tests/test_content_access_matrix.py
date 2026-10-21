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
    "prompt": {"read": {"PrivateSelf", "SharedPrompt"}, "delete": {"PrivateSelf"}, "reuse": {"PrivateSelf"}},
    "chat": {"read": {"PrivateSelfApp"}, "delete": {"PrivateSelfApp"}, "reuse": {"PrivateSelfApp"}},
    "voice": {"read": {"Own"}, "delete": {"Own"}, "reuse": {"Own"}},
    "receipt": {"read": {"Own"}},
    "job": {"read": {"Own"}, "cancel": {"Own"}},
    "request_history": {"read": {"PrivateSelf"}, "delete": {"PrivateSelf"}, "reuse": {"PrivateSelf"}},
    "debug_capture": {"read": {"PrivateSelf"}, "delete": {"PrivateSelf"}},
}
ACTIONS = ("read", "delete", "reuse", "share", "link", "cancel")
KEYS = frozenset({"app:a", "old:a"})
ACTORS = {
    "maker-session": (Viewer("a", "a", owner_keys=KEYS, privacy_permission=True), {"Own", "Self", "SelfApp", "Maker", "PrivateSelf"}),
    "maker-key": (Viewer("a", "a", owner_keys=KEYS), {"Own", "Self", "SelfApp", "Maker"}),
    "maker-key-with-privacy": (Viewer("a", "a", owner_keys=KEYS, privacy_permission=True), {"Own", "Self", "SelfApp", "Maker", "PrivateSelf"}),
    "mcp-session-grant": (Viewer("a", "a", owner_keys=KEYS, privacy_permission=True), {"Own", "Self", "SelfApp", "Maker", "PrivateSelf"}),
    "trusted-address": (Viewer(None, "a", owner_keys=KEYS), {"Own"}),
    "same-app-different-person": (Viewer("b", "a", owner_keys=KEYS), {"Own"}),
    "same-person-different-app": (Viewer("a", "b", owner_keys=frozenset({"app:b"})), {"Self"}),
    "service-app-key": (Viewer(None, "a", owner_keys=KEYS), {"Own"}),
    "personless-personal-key": (Viewer(None, "a", owner_keys=KEYS), {"Own"}),
    "accountable-service-session": (Viewer("c", "surface:c", owner_keys=KEYS), {"Own"}),
    "replacement-key-old-alias": (Viewer(None, "a", owner_keys=KEYS), {"Own"}),
    "owner-other": (Viewer("o", "o", owner_keys=frozenset({"app:o"}), is_owner=True), set()),
    "admin-other": (Viewer("d", "d", owner_keys=frozenset({"app:d"})), set()),
    "recipient": (Viewer("q", "q", owner_keys=frozenset({"app:q"})), {"Shared"}),
    "owner-recipient": (Viewer("q", "q", owner_keys=frozenset({"app:q"}), is_owner=True), {"Shared"}),
    "unrelated": (Viewer("b", "b", owner_keys=frozenset({"app:b"})), set()),
    "bearer-holder-without-identity": (Viewer(), set()),
    "empty-identity": (Viewer("", "", "", frozenset({""}), True), set()),
}


def records(*, active=True, include_prompt=False, available=True, legacy=False):
    grant = Share("share:1", "asset:1", "a", "q", include_prompt, "poster:1", active)
    owners = frozenset({"old:a" if legacy else "app:a"})
    return {
        "creation": CreationRecord("asset:1", owners, ("a",), shares=(grant,), available=available),
        "prompt": PromptRecord("asset:1", "a", shares=(grant,), available=available),
        "chat": ChatRecord("a", "a", available=available),
        "voice": VoiceRecord(owners, available=available),
        "receipt": ReceiptRecord(owners, available=available),
        "job": JobRecord(owners, available=available),
        "request_history": RequestHistoryRecord("a", available=available),
        "debug_capture": DebugCaptureRecord("a", available=available),
    }


@pytest.mark.parametrize("kind,action", tuple(product(MATRIX, ACTIONS)))
def test_every_kind_action_basis_cell(kind, action):
    for actor, (viewer, bases) in ACTORS.items():
        for active, include_prompt, available, legacy, role, privacy in product((False, True), repeat=6):
            basis = set(bases) - {"PrivateSelf"}
            if privacy and "Self" in basis:
                basis.add("PrivateSelf")
            if privacy and "SelfApp" in basis:
                basis.add("PrivateSelfApp")
            if not active:
                basis.discard("Shared")
            if "Shared" in basis and include_prompt and privacy:
                basis.add("SharedPrompt")
            allowed = available and bool(MATRIX[kind].get(action, set()) & basis)
            access = decide(replace(viewer, is_owner=role, privacy_permission=privacy), records(
                active=active, include_prompt=include_prompt, available=available, legacy=legacy)[kind], action)
            expected = (Shared if "Own" not in basis and "Self" not in basis
                        and "SelfApp" not in basis and "PrivateSelf" not in basis else Own) if allowed else Deny
            assert isinstance(access, expected), (kind, action, actor, active, include_prompt, available, legacy, role, privacy)
            if isinstance(access, Shared):
                assert (access.share_id, access.include_prompt) == ("share:1", include_prompt)


def test_maker_requires_own_and_exactly_one_maker():
    viewer = ACTORS["maker-session"][0]
    creation = records()["creation"]
    for makers in ((), ("a", "b"), ("a", "a"), ("b",)):
        assert isinstance(decide(viewer, replace(creation, maker_person_ids=makers), "share"), Deny)
    assert isinstance(decide(replace(viewer, owner_keys=frozenset()), creation, "share"), Deny)


def test_share_selects_only_its_asset_poster_and_maker_prompt_row():
    viewer = replace(ACTORS["recipient"][0], privacy_permission=True)
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


def test_null_self_never_matches_and_appless_chat_needs_exact_key():
    for action in ACTIONS:
        assert isinstance(decide(Viewer(), PromptRecord("a", None), action), Deny)
        assert isinstance(decide(Viewer(app_id="a"), ChatRecord(None, "a"), action), Deny)
    viewer = Viewer("a", key_hash="old:a", privacy_permission=True)
    chat = ChatRecord("a", owner_key_hash="old:a")
    assert isinstance(decide(viewer, chat, "read"), Own)
    assert isinstance(decide(viewer, replace(chat, owner_key_hash="old:b"), "read"), Deny)
    assert isinstance(decide(viewer, replace(chat, app_id="a"), "read"), Deny)
    assert isinstance(decide(replace(viewer, key_hash=None), chat, "read"), Deny)


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
