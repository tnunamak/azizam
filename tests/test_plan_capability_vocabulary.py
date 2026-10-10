"""Access-plan capability ids keep one meaning whichever spelling a plan saved.

Runtime routes stamp the plan vocabulary (``chat.completions``,
``image.generate``, ``speech.synthesis``...). Protocol frontends describe the
same work in route spellings (``images.generate``, ``audio.speech``...), and the
access-plan editor used to offer those as choices. A plan saved from those
choices must allow the same requests as the seeded "Media creator" plan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import proxy
from gateway import route_registry, service_registry
from gateway.admin_catalog import policy_editor_contract, profile_policy_catalog
from gateway.default_profiles import DEFAULT_PROFILE_SEEDS
from gateway.plan_vocabulary import canonical_intent, intent_aliases, intent_aliases_from_catalog
from gateway.profiles import load_profile

_MEDIA_CREATOR = next(seed["profile"] for seed in DEFAULT_PROFILE_SEEDS if seed["profile"]["id"] == "media-creator")
_ROUTE_SPELLED = {
    **_MEDIA_CREATOR,
    "id": "route-spelled",
    "allow": {"intents": [
        "chat.completions", "text.completions", "text.embeddings", "images.generate", "images.edit",
        "audio.speech", "audio.transcriptions", "audio.generate", "video.generate", "capabilities.list",
    ]},
}
_CHAT_BODY = json.dumps({"model": "any", "messages": [{"role": "user", "content": "hi"}]}).encode()


def _auth(profile_document: dict) -> proxy.AuthContext:
    identity = proxy.AuthIdentity(name="plan-key", key_hash="sha256:plan-key", profile=load_profile(profile_document))
    return proxy.AuthContext(identity=identity)


def _allowed(decision: proxy.PolicyDecision | None) -> bool:
    assert decision is not None, "route has no policy in the bundled route data"
    return decision.allowed


def _decisions(auth: proxy.AuthContext) -> dict[str, bool]:
    """Ask the proxy's own enforcement helpers, fed by the bundled route data."""
    out = {
        "chat": _allowed(proxy._azizam_policy_decision(auth, "/v1/chat/completions", _CHAT_BODY)),
        "responses": _allowed(proxy._protocol_policy_decision(auth, protocol="openai.responses", intent="chat.responses")),
        "embeddings": _allowed(proxy._azizam_policy_decision(auth, "/v1/embeddings", b"{}")),
        "speech": _allowed(proxy._runtime_route_policy_decision(auth, "/v1/audio/speech")),
        "transcription": _allowed(proxy._runtime_route_policy_decision(auth, "/v1/audio/transcriptions")),
        "models": _allowed(proxy._runtime_route_policy_decision(auth, "/v1/models")),
    }
    for name, path in (("image", "/v1/images/generations"), ("image_edit", "/v1/images/edits")):
        receipt = proxy.start_image_route_receipt(path, auth)
        out[name] = proxy._apply_image_route_policy(receipt, auth).allowed
    return out


@pytest.mark.parametrize("plan", [_MEDIA_CREATOR, _ROUTE_SPELLED], ids=["plan-spelling", "route-spelling"])
def test_media_creator_plan_allows_its_work_in_either_spelling(plan: dict) -> None:
    assert _decisions(_auth(plan)) == {
        "chat": True, "responses": True, "embeddings": True, "speech": True,
        "transcription": True, "models": True, "image": True, "image_edit": True,
    }


def test_a_chat_only_plan_still_denies_media_in_either_spelling() -> None:
    for intents in (["chat"], ["chat.completions"]):
        decisions = _decisions(_auth({"id": "chat-only", "allow": {"intents": intents}}))
        assert decisions["chat"] and decisions["responses"]
        assert not any(decisions[name] for name in ("speech", "transcription", "image", "image_edit", "models", "embeddings"))


def test_route_spellings_load_as_plan_intents_and_unknown_ids_pass_through() -> None:
    profile = load_profile({"allow": {"intents": ["audio.speech", "images.edit", "providers.native", "chat", "extension.custom"]}})
    assert profile.allow.intents == {"speech.synthesis", "image.edit", "provider.native", "chat", "extension.custom"}
    assert load_profile({"allow": {"intents": []}}).allow.intents == frozenset()
    assert load_profile({}).allow.intents is None


@pytest.mark.parametrize("options, message", [
    ([{"id": "a", "aliases": ["x"]}, {"id": "b", "aliases": ["x"]}], "maps to both"),
    ([{"id": "a", "aliases": ["b"]}, {"id": "b"}], "is itself a plan intent"),
    ([{"id": "a", "aliases": "x"}], "must be a list"),
])
def test_alias_data_must_be_unambiguous(options: list, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        intent_aliases_from_catalog(options)


def test_editor_offers_each_enforced_intent_once_with_a_plain_label() -> None:
    registry = service_registry.build_service_registry_seed({})
    choices = policy_editor_contract(registry)["profile"]["choices"]["intents"]
    ids = [choice["id"] for choice in choices]
    assert len(ids) == len(set(ids))
    assert all(choice["label"] != choice["id"] for choice in choices)
    assert not set(ids) & set(intent_aliases()), "a route spelling is offered beside its plan id"
    stamped = [policy["intent"] for policy in route_registry.runtime_route_policy(registry=registry).values()]
    stamped += [intent for _protocol, intent in route_registry.azizam_policy(registry=registry).values()]
    enforced = {canonical_intent(intent) for intent in stamped}
    assert enforced <= set(ids)
    by_id = {choice["id"]: choice for choice in choices}
    assert by_id["speech.synthesis"]["aliases"] == ["audio.speech"]
    assert by_id["provider.native"]["label"] == "Provider passthrough"


def test_member_task_labels_match_the_shared_task_names() -> None:
    terms = (Path(__file__).resolve().parent.parent / "web/src/lib/terms.ts").read_text(encoding="utf-8")
    block = terms[terms.index("export const taskNames"):]
    task_names = dict(re.findall(r'^\s+(\w+): "([^"]+)",$', block[: block.index("} as const")], re.M))
    labels = {option["id"]: option["label"] for option in profile_policy_catalog()["intents"]}
    for intent, task in {
        "chat": "chat", "image.generate": "image", "video.generate": "video", "speech.synthesis": "speech",
        "speech.transcription": "transcribe", "audio.generate": "audio", "decision.systemone": "decisions",
        "text.embeddings": "embeddings",
    }.items():
        assert labels[intent] == task_names[task], intent
