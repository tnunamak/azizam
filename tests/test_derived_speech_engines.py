"""A speech engine is derived from its service record (gateway/derived_engines.py).

The unit cases use plain registry-shaped records. The integration cases go through the real gateway: a raw record is
added with the operator route, and each reader of the engines table has to see the derived entry.
"""

from __future__ import annotations

import copy

import pytest

import proxy
from gateway import derived_engines, service_registry, stable_aliases
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.test_added_speech_engine_serves_speech import ADDED_MODEL, _app_key, _rig, _speak
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)


def _record(*models, enabled=True, capabilities=("audio", "speech"), **config):
    return {
        "enabled": enabled,
        "capabilities": list(capabilities),
        "config": {"models": [{"id": public, "model_group": public, "upstream_model": upstream} for public, upstream in models], **config},
    }


# ---- the pure derivation -------------------------------------------------------------------------------------------

def test_an_enabled_speech_record_gives_an_engine_for_each_model_row():
    engines = derived_engines.derive_speech_engines({
        "garage-voice": _record(("voice-a", "up-a"), ("voice-b", "up-b")),
    })

    assert engines == {
        "voice-a": {"service": "garage-voice", "model": "up-a"},
        "voice-b": {"service": "garage-voice", "model": "up-b"},
    }


def test_a_disabled_record_gives_no_engine():
    assert derived_engines.derive_speech_engines({"garage-voice": _record(("voice-a", "up-a"), enabled=False)}) == {}


def test_a_record_with_no_models_gives_no_engine():
    assert derived_engines.derive_speech_engines({"garage-voice": _record()}) == {}


def test_a_record_that_does_not_declare_speech_gives_no_engine():
    services = {"chatty": _record(("m", "m"), capabilities=("chat",)), "half": _record(("m2", "m2"), capabilities=("audio",))}

    assert derived_engines.derive_speech_engines(services) == {}


def test_a_derived_engine_never_turns_custom_voices_on():
    [entry] = derived_engines.derive_speech_engines({"garage-voice": _record(("voice-a", "up-a"))}).values()

    assert "custom_voices" not in entry


def test_two_records_offering_one_name_give_it_to_the_lower_service_id():
    engines = derived_engines.derive_speech_engines({"b-voice": _record(("same", "from-b")), "a-voice": _record(("same", "from-a"))})

    assert engines == {"same": {"service": "a-voice", "model": "from-a"}}


@pytest.mark.parametrize("where", ["config", "observations"])
def test_the_voice_list_comes_from_the_record_or_its_observation(where):
    record = _record(("voice-a", "up-a"))
    voices = [{"id": "nova"}, "echo"]
    if where == "config":
        record["config"]["voices"] = voices
        record["config"]["default_voice"] = "nova"
    else:
        record["observations"] = {"voices": voices, "default_voice": "nova"}

    [entry] = derived_engines.derive_speech_engines({"garage-voice": record}).values()

    assert entry["voices"] == voices and entry["default_voice"] == "nova"


def test_a_hand_entry_with_the_same_id_wins_whole_and_is_not_merged():
    hand = {"speech": {"engines": {"Voice-A": {"service": "other", "model": "hand-model"}}, "stable": {"tts": ["voice-a"]}}}
    derived = derived_engines.derive_speech_engines({"garage-voice": _record(("voice-a", "up-a"), voices=["nova"])})

    merged = derived_engines.with_derived_speech_engines(hand, derived)

    assert merged["speech"]["engines"] == {"Voice-A": {"service": "other", "model": "hand-model"}}
    assert merged["speech"]["stable"] == {"tts": ["voice-a"]}


def test_with_nothing_derived_the_aliases_come_back_unchanged():
    aliases = {"chat": {"stable": {"fast": None}}}
    merged = derived_engines.with_derived_speech_engines(aliases, {})

    assert merged == aliases and merged is not aliases


def test_a_derived_engine_sits_beside_a_hand_entry_with_another_id():
    hand = {"speech": {"engines": {"hand": {"service": "s", "model": "m"}}}}
    derived = derived_engines.derive_speech_engines({"garage-voice": _record(("voice-a", "up-a"))})

    engines = derived_engines.with_derived_speech_engines(hand, derived)["speech"]["engines"]

    assert set(engines) == {"hand", "voice-a"}


# ---- where the effective config is built ---------------------------------------------------------------------------

def _registry(**services):
    return {"services": services}


def test_the_effective_config_holds_the_derived_engine_and_the_alias_resolver_finds_it():
    registry = _registry(**{"garage-voice": {**_record(("voice-a", "up-a")), "host": "127.0.0.1", "port": 9}})

    effective = service_registry.apply_service_registry_to_config({"model_aliases": {"speech": {"stable": {"tts": None}}}}, registry)

    assert effective["model_aliases"]["speech"]["engines"] == {"voice-a": {"service": "garage-voice", "model": "up-a"}}
    resolution = stable_aliases.resolve_alias(effective["model_aliases"], "speech", "voice-a")
    assert resolution is not None and resolution.engine == "voice-a"
    assert (resolution.service, resolution.upstream_model) == ("garage-voice", "up-a")


def test_a_config_with_no_speech_record_is_left_exactly_as_it_was():
    base = {"model_aliases": {"speech": {"stable": {"tts": None}}}}

    effective = service_registry.apply_service_registry_to_config(base, _registry(chatty=_record(("m", "m"), capabilities=("chat",))))

    assert effective["model_aliases"] == base["model_aliases"]


# ---- the validator of the stored section ---------------------------------------------------------------------------

def test_the_stable_tts_tier_may_name_a_derived_engine_id():
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"))})}
    tier = {"speech": {"stable": {"tts": ["voice-a"]}, "openai": [{"match": "tts-1", "to": "voice-a"}]}}

    assert stable_aliases.alias_problems(tier, context) == []


def test_a_tier_naming_an_engine_no_record_and_no_table_gives_is_still_refused():
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"))})}

    problems = stable_aliases.alias_problems({"speech": {"stable": {"tts": ["nowhere"]}}}, context)

    assert [problem.code for problem in problems] == ["alias_invalid"]
    assert "unknown engine" in problems[0].message


def test_a_tier_naming_a_derived_engine_stays_valid_while_its_record_is_disabled():
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"), enabled=False)})}

    assert stable_aliases.alias_problems({"speech": {"stable": {"tts": ["voice-a"]}}}, context) == []


# ---- every reader of the engines table, through the real gateway ----------------------------------------------------

def test_every_reader_of_the_engines_table_sees_the_derived_engine(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)

    # The module-level readers (_speech_engines, the default engine, the endpoint, the fallback engines).
    assert dict(proxy._speech_engines())[ADDED_MODEL] == {"service": "garage-voice", "model": "fake-speech-1"}
    assert proxy._speech_engine_endpoint(ADDED_MODEL)[0] == "garage-voice"
    assert proxy._speech_engine_for_service("garage-voice")["model"] == "fake-speech-1"
    # The registry snapshot's model catalog and the gateway alias view read the same published config.
    views = proxy._gateway_aliases_provider("speech")
    assert any(ADDED_MODEL in str(view) for view in views.values())
    # The voices listing resolves the model through the same table.
    voices = owner.get(f"/v1/audio/voices?model={ADDED_MODEL}", headers={"authorization": f"Bearer {key}"})
    assert voices.status_code == 200, voices.text
    # A derived engine accepts no custom voice.
    assert ADDED_MODEL not in proxy._custom_voice_engines()
    # And the route itself serves it (the fake server's contract needs one call).
    assert _speak(owner, key, ADDED_MODEL).status_code == 200


def test_the_tier_validator_and_the_runtime_agree_on_a_derived_engine(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]

    written = owner.patch(
        "/admin/api/config/changes",
        headers=csrf_for(owner),
        json={"items": [{"section": "model_aliases", "pointer": "/speech/stable/tts", "op": "set", "value": [ADDED_MODEL]}],
              "if_match": {"model_aliases": revision}},
    )
    assert written.status_code == 200, written.text

    spoken = _speak(owner, key, "tts")

    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    assert [call.json["model"] for call in fake.calls("speech")] == ["fake-speech-1"]


def test_a_hand_entry_for_the_same_id_beats_the_record_through_the_real_gateway(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]
    written = owner.patch(
        "/admin/api/config/changes",
        headers=csrf_for(owner),
        json={"items": [{"section": "model_aliases", "pointer": f"/speech/engines/{ADDED_MODEL}", "op": "set",
                         "value": {"service": "garage-voice", "model": "hand-chosen-model"}}],
              "if_match": {"model_aliases": revision}},
    )
    assert written.status_code == 200, written.text

    spoken = _speak(owner, key, ADDED_MODEL)

    assert spoken.status_code == 200, spoken.text
    assert [call.json["model"] for call in fake.calls("speech")] == ["hand-chosen-model"]


# ---- a record with no port (a finding of the S0 note; not fixed here) ------------------------------------------------

def test_a_raw_record_whose_base_url_has_no_port_is_stored_without_one_and_turns_speech_off(gateway, monkeypatch, tmp_path):
    from gateway import media_jobs, receipt_store

    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    etag = owner.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')

    added = owner.post(
        "/admin/api/operator/services",
        headers={**csrf_for(owner), "If-Match": etag},
        json={"name": "garage-voice", "base_url": "http://voice.fk.test", "protocol": "inline-tts",
              "capabilities": ["audio", "speech"], "models": [{"public": ADDED_MODEL, "upstream": "fake-speech-1"}]},
    )

    assert added.status_code == 201, added.text  # accepted: nothing says the record is half configured
    stored = proxy.config["backends"]["garage-voice"]
    assert stored.get("host") and not stored.get("port")
    assert service_registry.engine_base_address(stored) == ""
    assert proxy.DERIVED.TTS_ENABLED is False
    spoken = _speak(owner, key, ADDED_MODEL)
    assert spoken.status_code == 503 and "TTS not enabled" in spoken.text
