"""Literal backend-capability pins and data-only extension contracts."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from gateway import local_capabilities as local

ROWS = Path(__file__).resolve().parents[1] / "registry" / "local_capability_rows.yml"


def capabilities(triggers, **spec):
    return local.build_local_capabilities({"backends": {"demo": {"capabilities": triggers, **spec}}})


def document():
    return yaml.safe_load(ROWS.read_text())


def write_rows(tmp_path, data):
    path = tmp_path / "backend-rows.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


@pytest.mark.parametrize("trigger,intent,inputs,outputs,streaming,gpu", [
    ("chat", "chat.completions", ("text",), ("text",), True, True),
    ("completions", "text.completions", ("text",), ("text",), True, True),
    ("embeddings", "text.embeddings", ("text",), ("text",), False, False),
    ("speech", "speech.synthesis", ("text",), ("audio",), True, True),
    ("tts", "speech.synthesis", ("text",), ("audio",), True, True),
    ("transcriptions", "speech.transcription", ("audio",), ("text",), False, True),
    ("stt", "speech.transcription", ("audio",), ("text",), False, True),
    ("translations", "speech.translation", ("audio",), ("text",), False, True),
    ("translation", "speech.translation", ("audio",), ("text",), False, True),
])
def test_shipped_backend_answers_are_literal(trigger, intent, inputs, outputs, streaming, gpu):
    (cap,) = capabilities([trigger])
    assert (cap.intent, cap.input_modalities, cap.output_modalities, cap.streaming,
            cap.execution, cap.resources.gpu_required, cap.backend) == (
        intent, inputs, outputs, streaming, "sync", gpu, "demo",
    )


@pytest.mark.parametrize("trigger", ["translations", "translation"])
def test_translation_caveat_and_recording_words(trigger):
    (cap,) = capabilities([trigger])
    assert cap.caveats == ("Only advertised when the local STT backend declares translation support.",)
    assert (cap.action_label, cap.recordable_inputs) == ("Translate", ("audio",))


@pytest.mark.parametrize("trigger", ["chat", "completions", "speech", "tts"])
def test_chat_and_speech_keep_gpu_need_instead_of_backend_override(trigger):
    (cap,) = capabilities([trigger], resources={"gpu_required": False, "vram_gb_estimate": 8})
    assert cap.resources.gpu_required is True
    assert cap.resources.vram_gb_estimate is None


def test_embeddings_reads_the_resource_override_with_existing_type_parsing():
    (cap,) = capabilities(["embeddings"], resources={
        "gpu_required": "yes", "vram_gb_estimate": "3.5", "max_parallel": "2", "duration_class": "short",
    })
    assert (cap.resources.gpu_required, cap.resources.vram_gb_estimate,
            cap.resources.max_parallel, cap.resources.duration_class) == (True, 3.5, 2, "short")


def test_vision_tags_and_recognition_labels_keep_literal_words():
    caps = capabilities(["chat", "vision", "speech", "transcriptions"])
    assert [(cap.intent, cap.input_modalities) for cap in caps] == [
        ("chat.completions", ("text", "image")),
        ("speech.synthesis", ("text",)), ("speech.transcription", ("audio",)),
    ]
    assert caps[1].tags == (
        "response_format:wav", "response_format:mp3", "response_format:opus",
        "response_format:aac", "response_format:flac", "response_format:pcm",
    )
    assert (caps[2].action_label, caps[2].recordable_inputs) == ("Transcribe", ("audio",))


def test_group_then_backend_then_row_order_and_synonym_deduplication():
    triggers = ["translation", "translations", "tts", "speech", "embeddings", "completions", "chat", "stt", "transcriptions"]
    caps = local.build_local_capabilities({"backends": {
        "second-name": {"capabilities": triggers}, "first-name": {"capabilities": triggers},
    }})
    assert [(cap.backend, cap.intent) for cap in caps] == [
        ("second-name", "chat.completions"), ("second-name", "text.completions"), ("second-name", "text.embeddings"),
        ("first-name", "chat.completions"), ("first-name", "text.completions"), ("first-name", "text.embeddings"),
        ("second-name", "speech.synthesis"), ("first-name", "speech.synthesis"),
        ("second-name", "speech.transcription"), ("second-name", "speech.translation"),
        ("first-name", "speech.transcription"), ("first-name", "speech.translation"),
    ]


def test_new_trigger_and_family_need_only_a_test_row(tmp_path, monkeypatch):
    data = document()
    data.setdefault("backend_rows", []).append({
        "group": "endpoints", "triggers": ["new-trigger"], "flag": "llm", "intent": "new.feature",
        "inputs": ["text"], "outputs": ["vector"], "streaming": False, "execution": "job",
        "gpu_required": False,
    })
    rows = local.load_backend_capability_rows(write_rows(tmp_path, data))
    monkeypatch.setattr(local, "_BACKEND_ROWS", rows)
    (cap,) = capabilities(["new-trigger"])
    assert (cap.intent, cap.input_modalities, cap.output_modalities, cap.streaming,
            cap.execution, cap.resources.gpu_required) == (
        "new.feature", ("text",), ("vector",), False, "job", False,
    )
    assert local.build_local_capabilities(
        {"backends": {"demo": {"capabilities": ["new-trigger"]}}}, enabled_flags={"llm": False},
    ) == []


@pytest.mark.parametrize("fault,field,value,message", [
    ("set", "extra", True, "unknown fields"),
    ("delete", "intent", None, "missing fields"),
    ("set", "triggers", [], "triggers"),
    ("duplicate", None, None, "duplicate trigger"),
    ("set", "group", "unknown", "group"),
    ("set", "flag", "", "flag"),  # any named flag is valid row data; an empty one is not
    ("set", "execution", "stream", "execution"),
    ("set", "streaming", "yes", "streaming"),
    ("set", "gpu_required", "yes", "gpu_required"),
    ("set", "resources_from", "secret-path", "resources_from"),
    ("set", "vision", {"unknown": True}, "vision"),
    ("set", "inputs", [], "inputs"),
])
def test_invalid_backend_row_names_file_and_row(tmp_path, fault, field, value, message):
    data = document()
    row_number = 1
    if fault == "duplicate":
        data["backend_rows"].append(deepcopy(data["backend_rows"][0]))
        row_number = len(data["backend_rows"])
    elif fault == "delete":
        del data["backend_rows"][0][field]
    else:
        data["backend_rows"][0][field] = value
    path = write_rows(tmp_path, data)
    with pytest.raises(ValueError) as caught:
        local.load_backend_capability_rows(path)
    assert str(path) in str(caught.value)
    assert f"backend_rows row {row_number}" in str(caught.value)
    assert message in str(caught.value)


def test_backend_rows_are_deeply_read_only():
    rows = local.load_backend_capability_rows(ROWS)
    with pytest.raises(TypeError):
        rows[0] = rows[0]
    with pytest.raises(TypeError):
        rows[0]["intent"] = "changed"
    with pytest.raises(TypeError):
        rows[0]["triggers"][0] = "changed"
    with pytest.raises(TypeError):
        rows[0]["vision"]["inputs"][0] = "changed"


def test_building_does_not_read_files_and_skips_unknown_or_malformed_triggers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("builder reloaded package data")
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert capabilities(["UNKNOWN", 4, "speech.synthesis"]) == []
    assert capabilities("chat") == []
    assert capabilities(["CHAT"])[0].intent == "chat.completions"
