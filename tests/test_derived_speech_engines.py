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
from tests.support.fake_upstream import load_scenario
from tests.test_added_speech_engine_serves_speech import ADDED_MODEL, _app_key, _rig, _speak, _wav
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

def test_a_wildcard_model_row_gives_no_engine():
    # A "*" row exposes whatever the server serves; a speech request must name one model, so it is not an engine.
    engines = derived_engines.derive_speech_engines({
        "garage-voice": _record(("voice-a", "up-a"), ("voice-any", "*"), ("*", "*")),
    })

    assert engines == {"voice-a": {"service": "garage-voice", "model": "up-a"}}


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


def test_a_tier_naming_an_engine_no_record_and_no_table_gives_is_shown_dangling_not_refused():
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"))})}

    problems = stable_aliases.alias_problems({"speech": {"stable": {"tts": ["nowhere"]}}}, context)

    assert [(p.pointer, p.code, p.severity) for p in problems] == [("/speech/stable/tts", "service_gone", "dangling")]
    assert "nowhere" in problems[0].message


def test_a_pattern_naming_an_engine_no_record_and_no_table_gives_is_shown_dangling_too():
    context = {"registry": _registry()}
    aliases = {"speech": {"openai": [{"match": "tts-1", "to": "gone-voice"}]}}

    assert [(p.pointer, p.severity) for p in stable_aliases.alias_problems(aliases, context)] == [("/speech/openai", "dangling")]
    assert [p.severity for p in stable_aliases.alias_problems(aliases, None)] == ["invalid"], "no registry: judged by structure"


def test_an_unknown_engine_of_another_operation_is_still_refused_with_a_registry():
    # An operation no record gives engines for (transcriptions did, once; tests/test_derived_transcription_engines.py).
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"))})}

    problems = stable_aliases.alias_problems({"rerank": {"stable": {"rr": ["nowhere"]}}}, context)

    assert [(p.code, p.severity) for p in problems] == [("alias_invalid", "invalid")]


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


# ---- a tier that names a derived engine whose record is later deleted ----------------------------------------------

def _set_tier(owner, value):
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]
    written = owner.patch(
        "/admin/api/config/changes",
        headers=csrf_for(owner),
        json={"items": [{"section": "model_aliases", "pointer": "/speech/stable/tts", "op": "set", "value": value}],
              "if_match": {"model_aliases": revision}},
    )
    assert written.status_code == 200, written.text


def _add_second_record(owner, fake, model):
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]
    added = owner.post(
        "/admin/api/operator/services",
        headers={**csrf_for(owner), "If-Match": etag},
        json={"name": "second-voice", "base_url": fake.base_url, "protocol": "inline-tts",
              "capabilities": ["audio", "speech"], "models": [{"public": model, "upstream": "fake-speech-2"}]},
    )
    assert added.status_code == 201, added.text


def _delete_record(owner, service_id):
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]
    removed = owner.delete(f"/admin/api/operator/services/{service_id}", headers={**csrf_for(owner), "If-Match": etag})
    assert removed.status_code == 200, removed.text


def test_a_tier_that_named_a_derived_engine_is_served_by_its_other_engines_once_the_record_is_deleted(
    gateway, fake_upstreams, monkeypatch, tmp_path,
):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    _add_second_record(owner, fake, "second-voice-model")
    _set_tier(owner, [ADDED_MODEL, "second-voice-model"])

    _delete_record(owner, "garage-voice")

    section = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()
    assert section["problems"] and all(problem["severity"] == "dangling" for problem in section["problems"]), section["problems"]
    assert [problem["pointer"] for problem in section["problems"]] == ["/speech/stable/tts"]
    spoken = _speak(owner, key, "tts")
    assert spoken.status_code == 200, spoken.text
    assert [call.json["model"] for call in fake.calls("speech")] == ["fake-speech-2"]


def test_a_tier_that_named_only_the_deleted_engine_answers_the_existing_no_engine_error(
    gateway, fake_upstreams, monkeypatch, tmp_path,
):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    fake.waive("speech")  # the point of this case: nothing may reach the speech server
    _set_tier(owner, [ADDED_MODEL])

    _delete_record(owner, "garage-voice")

    section = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()
    assert [(problem["pointer"], problem["code"], problem["severity"]) for problem in section["problems"]] == [
        ("/speech/stable/tts", "service_gone", "dangling")], section["problems"]
    assert section["effective"]["speech"]["stable"]["tts"] == [ADDED_MODEL], "the owner's tier is kept, not skipped"
    spoken = _speak(owner, key, "tts")
    assert spoken.status_code == 503 and "TTS not enabled" in spoken.text and fake.calls("speech") == [], spoken.text


# ---- a base URL with no port means the scheme's default port ---------------------------------------------------------

def test_a_base_url_with_no_port_gets_the_default_port_of_its_scheme():
    assert service_registry.split_base_url("http://voice.fk.test") == {"host": "voice.fk.test", "port": 80}
    assert service_registry.split_base_url("voice.fk.test/v1") == {"host": "voice.fk.test", "port": 80, "base_path": "/v1"}
    assert service_registry.split_base_url("https://voice.fk.test/v1") == {
        "host": "voice.fk.test", "port": 443, "scheme": "https", "base_path": "/v1"}


def test_a_base_url_with_an_explicit_port_is_stored_as_before():
    assert service_registry.split_base_url("http://voice.fk.test:8080/v1") == {"host": "voice.fk.test", "port": 8080, "base_path": "/v1"}
    assert service_registry.split_base_url("https://voice.fk.test:8443") == {"host": "voice.fk.test", "port": 8443, "scheme": "https"}


def test_an_https_url_with_an_explicit_port_is_addressed_as_https():
    raw = service_registry.service_form_to_raw({"name": "garage-voice", "base_url": "https://voice.fk.test:8443", "protocol": "inline-tts"})

    assert service_registry.engine_base_address(raw) == "https://voice.fk.test:8443"


def test_the_default_port_gives_the_record_an_address_of_its_scheme():
    for url, address in (("http://voice.fk.test", "http://voice.fk.test:80"), ("https://voice.fk.test", "https://voice.fk.test:443")):
        raw = service_registry.service_form_to_raw({"name": "garage-voice", "base_url": url, "protocol": "inline-tts"})
        assert service_registry.engine_base_address(raw) == address


def test_a_remote_provider_keeps_its_whole_base_url_as_its_address():
    form = {"name": "cloud-voice", "base_url": "https://api.fk.test/v1", "protocol": "inline-tts", "remote_provider": True}

    raw = service_registry.service_form_to_raw(form)

    assert "port" not in raw and raw["base_url"] == "https://api.fk.test/v1"
    assert service_registry.engine_base_address(raw) == "https://api.fk.test/v1"


def test_a_raw_record_whose_base_url_has_no_port_keeps_the_default_port_and_serves_speech(
    gateway, fake_upstreams, monkeypatch, tmp_path,
):
    from gateway import media_jobs, receipt_store

    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/speech-buffered", wav_hex=_wav().hex()), host="voice.fk.test")
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    etag = owner.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')

    added = owner.post(
        "/admin/api/operator/services",
        headers={**csrf_for(owner), "If-Match": etag},
        json={"name": "garage-voice", "base_url": fake.base_url, "protocol": "inline-tts",
              "capabilities": ["audio", "speech"], "models": [{"public": ADDED_MODEL, "upstream": "fake-speech-1"}]},
    )

    assert added.status_code == 201, added.text
    stored = proxy.config["backends"]["garage-voice"]
    assert stored["port"] == 80 and service_registry.engine_base_address(stored) == "http://voice.fk.test:80"
    assert proxy.DERIVED.TTS_ENABLED is True
    spoken = _speak(owner, key, ADDED_MODEL)
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    assert [call.json["model"] for call in fake.calls("speech")] == ["fake-speech-1"]
