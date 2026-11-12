"""The policy protocol and the workflow membership come from the frontend rows, not from intent spelling."""

from __future__ import annotations

import pytest

import proxy
from gateway import policy_protocol
from gateway.policy import PolicyRequest, evaluate
from gateway.policy_protocol import ProtocolUnresolved
from tests.support.access import access, plan_for


def _contract(intent: str, out_media: str, **inputs) -> dict:
    return {"intent": intent,
            "inputs": {"prompt": {"type": "string"}, **inputs},
            "outputs": {"result": {"media_type": out_media}}}


def test_one_unfamiliar_intent_with_two_bindings_resolves_by_each_workflows_contract():
    still = _contract("scene.render", "image/png")
    sound = _contract("scene.render", "audio/wav")

    assert policy_protocol.protocol_for_workflow("scene.render.a", still) == "openai.images"
    assert policy_protocol.protocol_for_workflow("scene.render.b", sound) == "openai.audio"


def test_a_second_unfamiliar_intent_resolves_from_its_contract():
    clip = _contract("reel.make", "video/mp4")

    assert policy_protocol.protocol_for_workflow("reel.make.x", clip) == "openai.video"
    assert not policy_protocol.lists_as_model("reel.make.x", clip)  # no listing row serves this intent
    assert policy_protocol.lists_as_model("video.generate.y", _contract("video.generate", "video/mp4"))


def test_an_unknown_intent_never_becomes_chat():
    with pytest.raises(ProtocolUnresolved):
        policy_protocol.protocol_for_intent("zzz.unheard-of")
    with pytest.raises(ProtocolUnresolved):
        policy_protocol.protocol_for_workflow("zzz.one", {"intent": "zzz.unheard-of", "inputs": {}, "outputs": {}})
    with pytest.raises(ProtocolUnresolved):
        proxy._protocol_for_intent("zzz.unheard-of")


@pytest.mark.parametrize("intent,protocol", [
    ("image.generate", "openai.images"),
    ("audio.generate", "openai.audio"),
    ("video.generate", "openai.video"),
    ("mesh.generate", "openai.mesh"),
    ("decision.systemone", "typesafe.systemone"),
    ("chat.generate", "openai.chat"),
])
def test_known_intents_keep_their_protocols(intent, protocol):
    assert policy_protocol.protocol_for_intent(intent) == protocol


def test_an_unknown_workflow_is_not_listed_as_a_model():
    assert not policy_protocol.lists_as_model("zzz.one", {"intent": "zzz", "inputs": {}, "outputs": {}})


def test_denied_discovery_and_denied_run_agree_without_an_upstream_call():
    contract = _contract("scene.render", "image/png")
    granted = plan_for(caps=("chat",), workflows=[])
    request = proxy._workflow_policy_request("scene.render.a", contract)

    decision = evaluate(access(granted, granted), request)

    assert decision.denied
    assert request.protocol == "openai.images"
