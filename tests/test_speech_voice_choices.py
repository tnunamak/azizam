"""Try's voice choices are exactly an engine's listed voices (gateway/speech_voices.py)."""
from gateway.speech_voices import resolve_voice, speech_voice_catalog


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
