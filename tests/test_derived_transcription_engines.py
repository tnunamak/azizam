"""A transcription engine is derived from its service record (gateway/derived_engines.py), as a speech engine is.

The integration cases go through the real gateway: raw records are added with the operator route, an App key posts a
multipart upload to ``/v1/audio/transcriptions`` and the fake servers (tests/support/fake_upstream, scenario
``openai-compatible/transcription-json``) show which record the request reached and with which model name.
"""

from __future__ import annotations

import io
import re
import wave

import pytest

import proxy
from gateway import derived_engines, media_jobs, receipt_store, service_registry, stable_aliases
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)


def _record(*models, enabled=True, capabilities=("audio", "transcriptions"), **config):
    return {
        "enabled": enabled,
        "capabilities": list(capabilities),
        "config": {"models": [{"id": public, "model_group": public, "upstream_model": upstream} for public, upstream in models], **config},
    }


def _registry(**services):
    return {"services": services}


# ---- the pure derivation -------------------------------------------------------------------------------------------

def test_an_enabled_transcription_record_gives_an_engine_for_each_model_row():
    engines = derived_engines.derive_transcription_engines({"garage-stt": _record(("ear-a", "up-a"), ("ear-b", "up-b"))})

    assert engines == {"ear-a": {"service": "garage-stt", "model": "up-a"}, "ear-b": {"service": "garage-stt", "model": "up-b"}}


def test_a_wildcard_model_row_gives_no_transcription_engine():
    engines = derived_engines.derive_transcription_engines({"garage-stt": _record(("ear-a", "up-a"), ("ear-any", "*"), ("*", "*"))})

    assert engines == {"ear-a": {"service": "garage-stt", "model": "up-a"}}


def test_a_disabled_record_or_one_with_no_models_gives_no_transcription_engine():
    assert derived_engines.derive_transcription_engines({"a": _record(("ear-a", "up-a"), enabled=False), "b": _record()}) == {}
    assert derived_engines.derive_transcription_engines({"a": _record(("ear-a", "up-a"), enabled=False)}, include_disabled=True) == {
        "ear-a": {"service": "a", "model": "up-a"}}


def test_each_capability_derives_only_from_the_records_that_declare_it():
    services = {"voice": _record(("v", "up-v"), capabilities=("audio", "speech")), "ear": _record(("e", "up-e")),
                "half": _record(("h", "up-h"), capabilities=("audio",)), "chat": _record(("c", "c"), capabilities=("chat",))}

    assert derived_engines.derive_transcription_engines(services) == {"e": {"service": "ear", "model": "up-e"}}
    assert derived_engines.derive_speech_engines(services) == {"v": {"service": "voice", "model": "up-v"}}


def test_the_dotted_capability_word_declares_transcription_too():
    services = {"ear": _record(("e", "up-e"), capabilities=("audio.transcriptions",))}

    assert derived_engines.derive_transcription_engines(services) == {"e": {"service": "ear", "model": "up-e"}}


def test_two_records_offering_one_name_give_it_to_the_lower_service_id():
    engines = derived_engines.derive_transcription_engines({"b-ear": _record(("same", "from-b")), "a-ear": _record(("same", "from-a"))})

    assert engines == {"same": {"service": "a-ear", "model": "from-a"}}


def test_only_limits_the_records():
    services = {"a-ear": _record(("a", "up-a")), "b-ear": _record(("b", "up-b"))}

    assert derived_engines.derive_transcription_engines(services, only={"b-ear"}) == {"b": {"service": "b-ear", "model": "up-b"}}


def test_a_derived_transcription_entry_carries_only_service_and_model():
    [entry] = derived_engines.derive_transcription_engines(
        {"garage-stt": {**_record(("ear-a", "up-a"), voices=["nova"], default_voice="nova"), "observations": {"voices": ["x"]}}}).values()

    assert set(entry) == {"service", "model"}


def test_the_speech_derivation_is_the_shared_rule_plus_voices():
    record = _record(("voice-a", "up-a"), capabilities=("audio", "speech"), voices=["nova"], default_voice="nova")

    assert derived_engines.derive_speech_engines({"v": record}) == {
        "voice-a": {"service": "v", "model": "up-a", "voices": ["nova"], "default_voice": "nova"}}
    assert derived_engines.derive_engines({"v": record}, derived_engines.SPEECH_CAPABILITY) == {
        "voice-a": {"service": "v", "model": "up-a"}}


def test_a_hand_entry_with_the_same_id_wins_whole_and_is_not_merged():
    hand = {"transcriptions": {"engines": {"Ear-A": {"service": "other", "model": "hand-model"}}, "stable": {"stt": ["ear-a"]}}}
    derived = derived_engines.derive_transcription_engines({"garage-stt": _record(("ear-a", "up-a"))})

    merged = derived_engines.with_derived_transcription_engines(hand, derived)

    assert merged["transcriptions"]["engines"] == {"Ear-A": {"service": "other", "model": "hand-model"}}
    assert merged["transcriptions"]["stable"] == {"stt": ["ear-a"]}


def test_a_name_the_owners_section_resolves_stays_the_owners():
    hand = {"transcriptions": {"stable": {"whisper-1": ["hand"]}, "openai": [{"match": "gpt-4o-*", "to": "hand"}],
                               "engines": {"hand": {"service": "s", "model": "m"}}}}
    derived = derived_engines.derive_transcription_engines(
        {"garage-stt": _record(("whisper-1", "up-1"), ("gpt-4o-transcribe", "up-2"), ("ear-a", "up-a"))})

    engines = derived_engines.with_derived_transcription_engines(hand, derived)["transcriptions"]["engines"]

    assert set(engines) == {"hand", "ear-a"}


def test_a_derived_transcription_engine_touches_no_other_section():
    aliases = {"speech": {"engines": {"v": {"service": "s", "model": "m"}}}, "chat": {"stable": {"fast": None}}}
    derived = derived_engines.derive_transcription_engines({"garage-stt": _record(("ear-a", "up-a"))})

    merged = derived_engines.with_derived_transcription_engines(aliases, derived)

    assert merged["speech"] == aliases["speech"] and merged["chat"] == aliases["chat"]
    assert merged["transcriptions"]["engines"] == {"ear-a": {"service": "garage-stt", "model": "up-a"}}


def test_with_nothing_derived_the_aliases_come_back_unchanged():
    aliases = {"chat": {"stable": {"fast": None}}}
    merged = derived_engines.with_derived_transcription_engines(aliases, {})

    assert merged == aliases and merged is not aliases


# ---- where the effective config is built ---------------------------------------------------------------------------

def _with_address(record):
    return {**record, "host": "127.0.0.1", "port": 9}


def test_the_effective_config_holds_the_derived_engine_and_the_alias_resolver_finds_it_for_both_operations():
    registry = _registry(**{"garage-stt": _with_address(_record(("ear-a", "up-a")))})

    effective = service_registry.apply_service_registry_to_config({"model_aliases": {"transcriptions": {"stable": {"stt": None}}}}, registry)

    assert effective["model_aliases"]["transcriptions"]["engines"] == {"ear-a": {"service": "garage-stt", "model": "up-a"}}
    for operation in ("transcriptions", "translations"):
        resolution = stable_aliases.resolve_alias(effective["model_aliases"], operation, "ear-a")
        assert resolution is not None and resolution.engine == "ear-a"
        assert (resolution.service, resolution.upstream_model) == ("garage-stt", "up-a")


def test_one_record_may_give_speech_and_transcription_engines_each_to_its_own_section():
    registry = _registry(both=_with_address(_record(("m", "up-m"), capabilities=("audio", "speech", "transcriptions"))))

    effective = service_registry.apply_service_registry_to_config({}, registry)

    assert effective["model_aliases"]["speech"]["engines"] == {"m": {"service": "both", "model": "up-m"}}
    assert effective["model_aliases"]["transcriptions"]["engines"] == {"m": {"service": "both", "model": "up-m"}}


def _before_transcription_derivation(monkeypatch, base, registry):
    """The effective config as it was when only speech was derived: the transcription derivation turned off."""
    with monkeypatch.context() as patch:
        patch.setattr(derived_engines, "derive_transcription_engines", lambda *args, **kwargs: {})
        return service_registry.apply_service_registry_to_config(base, registry)


def _unchanged_fixtures():
    speech = _with_address(_record(("voice-a", "up-a"), capabilities=("audio", "speech"), voices=["nova"], default_voice="nova"))
    return {
        "no_records": (_registry(), {}),
        "speech_only": (_registry(**{"garage-voice": speech}), {"model_aliases": {"speech": {"stable": {"tts": None}}}}),
        "speech_and_chat": (_registry(**{"garage-voice": speech, "chatty": _record(("m", "m"), capabilities=("chat",))}),
                            {"model_aliases": {"speech": {"stable": {"tts": None}}}}),
        "transcription_record_with_no_models": (_registry(ear=_with_address(_record())), {"model_aliases": {"transcriptions": {"stable": {"stt": None}}}}),
        "transcription_record_with_only_wildcards": (_registry(ear=_with_address(_record(("*", "*"), ("any", "*")))), {}),
        "disabled_transcription_record": (_registry(ear=_with_address(_record(("ear-a", "up-a"), enabled=False))),
                                          {"model_aliases": {"transcriptions": {"engines": {"hand": {"service": "s", "model": "m"}}}}}),
        "transcription_model_rows_on_a_chat_record": (_registry(chatty=_with_address(_record(("m", "m"), capabilities=("chat",)))), {}),
    }


@pytest.mark.parametrize("case", sorted(_unchanged_fixtures()))
def test_a_config_with_no_enabled_transcription_record_listing_models_gives_the_same_effective_config(monkeypatch, case):
    registry, base = _unchanged_fixtures()[case]

    after = service_registry.apply_service_registry_to_config(base, registry)

    assert after == _before_transcription_derivation(monkeypatch, base, registry)
    if case == "speech_only":  # a fixed expectation, so a speech change shared by both sides still fails here
        assert after["model_aliases"]["speech"]["engines"] == {
            "voice-a": {"service": "garage-voice", "model": "up-a", "voices": ["nova"], "default_voice": "nova"}}


# ---- the validator of the stored section ---------------------------------------------------------------------------

def test_a_transcription_tier_may_name_a_derived_engine_id():
    context = {"registry": _registry(**{"garage-stt": _record(("ear-a", "up-a"))})}
    tier = {"transcriptions": {"stable": {"stt": ["ear-a"]}, "openai": [{"match": "whisper-1", "to": "ear-a"}]}}

    assert stable_aliases.alias_problems(tier, context) == []


def test_a_transcription_tier_naming_a_disabled_records_engine_stays_valid_while_it_is_off():
    # The validator derives names with include_disabled, so the name is still known (it neither dangles nor is refused).
    context = {"registry": _registry(**{"garage-stt": _record(("ear-a", "up-a"), enabled=False)})}

    assert stable_aliases.alias_problems({"transcriptions": {"stable": {"stt": ["ear-a"]}}}, context) == []


def test_a_transcription_tier_naming_an_engine_no_record_gives_is_shown_dangling_with_the_service_gone_reason():
    # What a deleted record's engine looks like: the name is in no table and no record.
    context = {"registry": _registry(**{"garage-stt": _record(("ear-a", "up-a"))})}

    problems = stable_aliases.alias_problems({"transcriptions": {"stable": {"stt": ["ear-gone"]}}}, context)

    assert [(p.pointer, p.code, p.severity) for p in problems] == [("/transcriptions/stable/stt", "service_gone", "dangling")]
    assert "ear-gone" in problems[0].message


def test_a_transcription_pattern_naming_an_engine_no_record_gives_is_shown_dangling_and_without_a_registry_invalid():
    aliases = {"transcriptions": {"openai": [{"match": "whisper-1", "to": "ear-gone"}]}}

    assert [(p.pointer, p.severity) for p in stable_aliases.alias_problems(aliases, {"registry": _registry()})] == [
        ("/transcriptions/openai", "dangling")]
    assert [p.severity for p in stable_aliases.alias_problems(aliases, None)] == ["invalid"], "no registry: judged by structure"


def test_a_speech_record_does_not_make_a_transcription_name_known():
    context = {"registry": _registry(**{"garage-voice": _record(("voice-a", "up-a"), capabilities=("audio", "speech"))})}

    problems = stable_aliases.alias_problems({"transcriptions": {"stable": {"stt": ["voice-a"]}}}, context)

    assert [p.severity for p in problems] == ["dangling"]


def test_an_unknown_engine_of_an_operation_no_record_gives_is_still_refused_with_a_registry():
    context = {"registry": _registry(**{"garage-stt": _record(("ear-a", "up-a"))})}

    problems = stable_aliases.alias_problems({"rerank": {"stable": {"rr": ["nowhere"]}}}, context)

    assert [(p.code, p.severity) for p in problems] == [("alias_invalid", "invalid")]


# ---- every reader of the engines table, through the real gateway ----------------------------------------------------

FIRST_MODEL = "first-ear"
ADDED_MODEL = "added-ear"
TRANSCRIPTION_WORDS = ["audio", "transcriptions"]


def _clip() -> bytes:
    """Two seconds of silence: long enough for the gateway to decode, short enough to be sent whole."""
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0" * 32_000)
    return out.getvalue()


def _add_record(owner, name, fake, public, upstream, *, capabilities=TRANSCRIPTION_WORDS):
    etag = owner.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    added = owner.post(
        "/admin/api/operator/services",
        headers={**csrf_for(owner), "If-Match": etag},
        json={"name": name, "base_url": fake.base_url, "protocol": "chunked-stt", "capabilities": list(capabilities),
              "models": [{"public": public, "upstream": upstream}]},
    )
    assert added.status_code == 201, added.text


def _rig(gateway, fake_upstreams, monkeypatch, tmp_path):
    """Two transcription records, each with one model row. Returns (owner client, App key, first fake, second fake)."""
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    first = fake_upstreams.loopback(load_scenario("openai-compatible/transcription-json"))
    second = fake_upstreams.loopback(load_scenario("openai-compatible/transcription-json"))
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    _add_record(owner, "a-stt", first, FIRST_MODEL, "fake-first-1")
    _add_record(owner, "garage-stt", second, ADDED_MODEL, "fake-stt-2")
    return owner, key, first, second


def _transcribe(owner, key, model, path="/v1/audio/transcriptions"):
    return owner.post(
        path,
        headers={"authorization": f"Bearer {key}"},
        data={"model": model, "response_format": "json"},
        files={"file": ("clip.wav", _clip(), "audio/wav")},
    )


def _model_field(call) -> str:
    """The ``model`` field of the multipart upload a fake received."""
    found = re.search(rb'name="model"\r\n\r\n([^\r]*)\r\n', call.body)
    assert found, call.body[:300]
    return found.group(1).decode()


def test_a_second_transcription_record_is_reached_by_its_model_name(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")  # the point of this case: the request must not go to the first record

    heard = _transcribe(owner, key, ADDED_MODEL)

    assert heard.status_code == 200, heard.text
    assert heard.json()["text"] == "hello from the fake"
    assert first.calls("transcription") == []
    assert [_model_field(call) for call in second.calls("transcription")] == ["fake-stt-2"]


def test_a_hand_entry_for_the_same_id_beats_the_record_through_the_real_gateway(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")
    _patch_aliases(owner, [{"pointer": f"/transcriptions/engines/{ADDED_MODEL}", "op": "set",
                            "value": {"service": "garage-stt", "model": "hand-chosen-model"}}])

    heard = _transcribe(owner, key, ADDED_MODEL)

    assert heard.status_code == 200, heard.text
    assert [_model_field(call) for call in second.calls("transcription")] == ["hand-chosen-model"]


def test_a_tier_may_name_a_derived_transcription_engine_and_reaches_its_record(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")
    _patch_aliases(owner, [{"pointer": "/transcriptions/stable/stt", "op": "set", "value": [ADDED_MODEL]}])

    heard = _transcribe(owner, key, "stt")

    assert heard.status_code == 200, heard.text
    assert [_model_field(call) for call in second.calls("transcription")] == ["fake-stt-2"]


def test_the_first_record_is_reached_by_its_own_model_name_too(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    second.waive("transcription")

    heard = _transcribe(owner, key, FIRST_MODEL)

    assert heard.status_code == 200, heard.text
    assert [_model_field(call) for call in first.calls("transcription")] == ["fake-first-1"]


def test_a_derived_transcription_engine_serves_translations_through_its_own_record(gateway, fake_upstreams, monkeypatch, tmp_path):
    """Translations share the transcriptions alias table (registry/local_capability_rows.yml, shared_operations)."""
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")
    second.waive("transcription")

    heard = _transcribe(owner, key, ADDED_MODEL, path="/v1/audio/translations")

    assert heard.status_code == 200, heard.text
    assert first.calls("translation") == []
    assert [_model_field(call) for call in second.calls("translation")] == ["fake-stt-2"]


def test_a_model_no_record_gives_is_still_refused_before_any_upstream_call(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")
    second.waive("transcription")

    heard = _transcribe(owner, key, "no-such-ear")

    assert heard.status_code == 404 and heard.json()["error"]["code"] == "model_not_found", heard.text
    assert first.calls("transcription") == [] and second.calls("transcription") == []


def test_a_derived_engine_goes_when_its_record_is_deleted(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, first, second = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    first.waive("transcription")
    second.waive("transcription")
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]
    removed = owner.delete("/admin/api/operator/services/garage-stt", headers={**csrf_for(owner), "If-Match": etag})
    assert removed.status_code == 200, removed.text

    heard = _transcribe(owner, key, ADDED_MODEL)

    assert heard.status_code == 404 and heard.json()["error"]["code"] == "model_not_found", heard.text
    assert second.calls("transcription") == []


def _patch_aliases(owner, items):
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]
    written = owner.patch(
        "/admin/api/config/changes",
        headers=csrf_for(owner),
        json={"items": [{"section": "model_aliases", **item} for item in items], "if_match": {"model_aliases": revision}},
    )
    assert written.status_code == 200, written.text


@pytest.mark.parametrize("operation,capability", [("transcriptions", "transcriptions"), ("speech", "speech")])
def test_a_tier_naming_a_derived_engine_the_owners_own_pattern_shadows_is_shown_dangling(operation, capability):
    # The merge leaves a derived id out when the owner's pattern already resolves it, so a tier naming it names nothing.
    context = {"registry": _registry(**{"garage": _record(("ear-a", "up-a"), capabilities=("audio", capability))})}
    aliases = {operation: {"stable": {"x": ["ear-a"]}, "openai": [{"match": "ear-*", "passthrough": True}]}}

    problems = stable_aliases.alias_problems(aliases, context)

    assert [(p.pointer, p.severity) for p in problems] == [(f"/{operation}/stable/x", "dangling")]
    assert "ear-a" in problems[0].message


# ---- a record with a default model still sends the engine's own model --------------------------------------------------

def test_a_request_naming_an_engine_sends_that_engines_model_not_the_records_default(gateway, fake_upstreams, monkeypatch, tmp_path):
    """Every alias counted as an OpenAI alias, so a record's ``default_model`` replaced the model of the engine asked for."""
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/transcription-json"))
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    etag = owner.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    added = owner.post(
        "/admin/api/operator/services", headers={**csrf_for(owner), "If-Match": etag},
        json={"name": "garage-stt", "base_url": fake.base_url, "protocol": "chunked-stt", "capabilities": TRANSCRIPTION_WORDS,
              "default_model": "record-default", "models": [{"public": ADDED_MODEL, "upstream": "fake-stt-2"}]})
    assert added.status_code == 201, added.text

    heard = _transcribe(owner, key, ADDED_MODEL)

    assert heard.status_code == 200, heard.text
    assert [_model_field(call) for call in fake.calls("transcription")] == ["fake-stt-2"]
