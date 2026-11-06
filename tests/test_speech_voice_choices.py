"""Try's voice choices and the voice listing are exactly a model's engines' voices (gateway/speech_voices.py)."""
from gateway.speech_voices import resolve_voice, speech_voice_catalog, voice_listing


def test_choices_are_the_listed_voices_never_the_accepted_spellings():
    choices = speech_voice_catalog({
        "voices": ["F1", {"id": "M1", "name": "Calm"}],
        "voice_map": {"alloy": "F1", "echo": "M1"},
        "default_voice": "F1",
    })
    assert choices["voices"] == [{"id": "F1", "name": "F1"}, {"id": "M1", "name": "Calm"}]
    assert choices["default_voice"] == "F1"
    assert not choices["allow_other"]


def test_a_vendor_code_without_a_name_is_shown_as_written():
    choices = speech_voice_catalog({"voices": ["af_heart", "F1"]})
    assert [voice["name"] for voice in choices["voices"]] == ["af_heart", "F1"]


def test_an_engine_without_a_voice_list_takes_any_voice_id():
    assert speech_voice_catalog({})["allow_other"]
    assert speech_voice_catalog({"voice_map": {"calm": "speaker-1"}, "default_voice": "speaker-1"})["allow_other"]
    assert resolve_voice({}, {}, "my-voice") == "my-voice"
    assert resolve_voice({"voice_map": {"calm": "speaker-1"}}, {}, "Calm") == "speaker-1"


def test_a_record_without_an_id_names_no_voice():
    assert speech_voice_catalog({"voices": [{"name": "Nameless"}, {"id": "", "name": "Empty"}, "F1"]})["voices"] == [
        {"id": "F1", "name": "F1"},
    ]


def test_a_replacement_engine_follows_its_own_data():
    spec = {"voices": [{"id": "Narrator-7", "name": "Narrator"}], "voice_map": {"alloy": "Narrator-7"}, "default_voice": "Narrator-7"}
    assert resolve_voice(spec, {"old": "alloy"}, "narrator-7") == "Narrator-7"
    assert resolve_voice(spec, {"old": "alloy"}, "old") == "Narrator-7"
    assert resolve_voice(spec, {}, "") == "Narrator-7"
    assert resolve_voice(spec, {}, "alloy2") is None
    assert resolve_voice({"voices": ["a"]}, {}, "") == ""  # no default: nothing is invented


def test_a_tier_offers_every_engine_voice_in_engine_order_with_the_first_engine_default():
    fast = {"voices": ["F1", "M1"], "voice_map": {"alloy": "F1"}, "default_voice": "F1"}
    full = {"voices": [{"id": "alloy", "name": "Alloy"}, "f1", "de_female"], "default_voice": "alloy"}
    choices = speech_voice_catalog(fast, full)
    assert choices["voices"] == [
        {"id": "F1", "name": "F1"}, {"id": "M1", "name": "M1"},
        {"id": "alloy", "name": "Alloy"}, {"id": "de_female", "name": "de_female"},
    ]
    assert choices["default_voice"] == "F1"
    assert not choices["allow_other"]
    assert speech_voice_catalog(fast, {})["allow_other"]


def test_the_listing_is_every_voice_a_tier_accepts():
    fast = {"voices": ["F1"], "voice_map": {"alloy": "F1", "calm": "F1"}}
    full = {"voices": [{"id": "alloy", "language": "en"}, "de_female"]}
    listing = voice_listing([fast, full], {"old": "alloy", "older": "calm", "gone": "nobody"})
    assert [(entry["id"], entry["mapped_to"], entry["alias"]) for entry in listing] == [
        ("F1", "F1", False), ("alloy", "alloy", False), ("de_female", "de_female", False),
        ("calm", "F1", True), ("old", "alloy", True), ("older", "calm", True),
    ]
    accepted = {entry["id"].lower() for entry in listing}
    for voice in accepted:
        assert any(resolve_voice(spec, {"old": "alloy", "older": "calm"}, voice) is not None for spec in (fast, full))
    assert voice_listing([], {"old": "alloy"}) == []


def test_every_listing_entry_carries_the_exact_documented_fields():
    fast = {"voices": [{"id": "f1", "name": "Calm", "language": "en", "gender": "female", "description": "Soft"}],
            "voice_map": {"deep_voice": "f1", "f1": "f1"}}
    assert voice_listing([fast], {"legacy_one": "deep_voice", "f1": "f1", "legacy_two": "f1"}) == [
        {"voice_id": "f1", "id": "f1", "name": "Calm", "language": "en", "gender": "female",
         "description": "Soft", "mapped_to": "f1", "alias": False},
        {"voice_id": "deep_voice", "id": "deep_voice", "name": "Deep Voice", "language": "en", "gender": "female",
         "description": "Accepted spelling of 'f1'", "mapped_to": "f1", "alias": True},
        {"voice_id": "legacy_one", "id": "legacy_one", "name": "Legacy One", "language": "en", "gender": "female",
         "description": "Legacy alias of 'deep_voice'", "mapped_to": "deep_voice", "alias": True},
        {"voice_id": "legacy_two", "id": "legacy_two", "name": "Legacy Two", "language": "en", "gender": "female",
         "description": "Legacy alias of 'f1'", "mapped_to": "f1", "alias": True},
    ]


def test_a_listed_voice_without_a_name_is_named_by_its_id_in_the_listing():
    entry = voice_listing([{"voices": ["af_heart"]}], {})[0]
    assert (entry["name"], entry["language"], entry["gender"], entry["description"]) == ("af_heart", None, None, None)


def test_an_alias_never_shadows_a_listed_voice_or_an_accepted_spelling_or_an_unknown_target():
    spec = {"voices": ["F1", "M1"], "voice_map": {"calm": "F1"}}
    listing = voice_listing([spec], {"M1": "F1", "calm": "M1", "ghost": "nobody"})
    assert [(e["id"], e["mapped_to"]) for e in listing] == [("F1", "F1"), ("M1", "M1"), ("calm", "F1")]
