from gateway.speech_voices import speech_voice_catalog
from gateway.transforms import TransformRegistry


def test_preset_choices_include_native_default_and_only_this_models_map():
    registry = TransformRegistry()
    first = speech_voice_catalog({"voice_map": {"alloy": "F1", "echo": "M1"}, "default_voice": "F1"}, registry)
    second = speech_voice_catalog({"voice_map": {"nova": "speaker-a"}, "default_voice": "speaker-a"}, registry)
    assert [voice["id"] for voice in first["voices"]] == ["alloy", "echo", "F1", "M1"]
    assert first["default_voice"] == "F1"
    assert not first["allow_other"]
    assert [voice["id"] for voice in second["voices"]] == ["nova", "speaker-a"]


def test_declarative_transform_controls_choices_and_default():
    registry = TransformRegistry()
    registry.register("configured-voices", {"kind": "value-map", "param": "voice", "map": {"calm": "speaker-1", "bright": "speaker-2"}, "default": "calm"})
    choices = speech_voice_catalog({"voice_transform": "configured-voices", "voice_map": {"ignored": "ignored"}}, registry)
    assert [voice["id"] for voice in choices["voices"]] == ["calm", "bright"]
    assert choices["default_voice"] == "calm"
    assert not choices["allow_other"]


def test_other_only_when_unknown_voice_ids_pass_through():
    registry = TransformRegistry()
    assert speech_voice_catalog({}, registry)["allow_other"]
    assert not speech_voice_catalog({"default_voice": "calm"}, registry)["allow_other"]
    registry.register("passthrough", {"kind": "value-map", "param": "voice", "map": {"calm": "speaker-1"}})
    assert speech_voice_catalog({"voice_transform": "passthrough"}, registry)["allow_other"]
    registry.register("opaque", {"kind": "default-params", "set": {"voice": "calm"}})
    assert not speech_voice_catalog({"voice_transform": "opaque"}, registry)["allow_other"]
