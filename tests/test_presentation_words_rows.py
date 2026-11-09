"""Literal cleanup pins and import-time presentation vocabulary contracts."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import re

import pytest
import yaml

from gateway.capabilities import Capability
from gateway import model_presentation as presentation


SHIPPED = {
    "schema_version": 1,
    "provider_suffixes": [" (provider)", " (Provider)"],
    "engine_prefixes": ["Local", "Remote"],
    "engine_tails": ["server", "provider", "engine"],
    "capability_engine_suffixes": {"transcribe": "stt", "speech": "tts"},
    "acronyms": {"Cpu": "CPU", "Gpu": "GPU", "Stt": "STT", "Tts": "TTS"},
    "parenthesized_hardware": ["CPU", "GPU"],
    "hardware_label_suffixes": ["CPU", "GPU", "STT", "TTS"],
    "bare_titles": ["tts", "stt"],
    "technical_patterns": [
        r":\d{2,5}\b",
        r"\b(?:port|host|localhost|127\.0\.0\.1)\b",
        r"\b(?:gguf|safetensors|checkpoint)\b",
        r"\b(?:contract|workflow|flac|khz)\b",
        r"\bcontext (?:of|window|length)\b",
        r"\bvia\s+(?:local|remote|backend|engine|server)\b",
        r"\b(?:served by|served via|running on)\b",
        r"[/\\][\w.-]+",
    ],
    "unclassified": {
        "capability": "other", "label": "AI", "icon": "chat",
        "service_capability": "chat", "summary": "Use this configured AI capability.",
    },
}
WORDS_FILE = Path(__file__).resolve().parents[1] / "registry/presentation_words.yml"


def image_row(description=None, **metadata):
    return presentation.capability_presentation(
        Capability(intent="image.generate", backend="test", model="demo"),
        {"description": description, **metadata},
    )


@pytest.mark.parametrize("source,expected", [
    ("Orion Cpu", "Orion (CPU)"), ("Orion Gpu", "Orion (GPU)"),
    ("Orion Stt", "Orion STT"), ("Orion Tts", "Orion TTS"),
    ("Orion cpu", "Orion cpu"), ("Orion cPu", "Orion cPu"),
])
def test_acronym_fixes_are_exact_and_only_hardware_gets_parentheses(source, expected):
    assert presentation._preserve_engine_acronyms(source) == expected


@pytest.mark.parametrize("name,caps,expected", [
    ("Orion(CPU) (provider)", [], "Orion(CPU)"),
    ("Orion(CPU) (Provider)", [], "Orion(CPU)"),
    ("Orion(CPU) (PROVIDER)", [], "Orion(CPU) (PROVIDER)"),
    ("Local Orion", ["chat"], "Orion (chat)"),
    ("Remote Orion", ["chat"], "Orion (chat)"),
    ("local Orion", ["chat"], "local Orion (chat)"),
    ("LOCAL Orion", ["chat"], "LOCAL Orion (chat)"),
    ("Remote Orion", [], "Remote Orion"),
    ("Orion server", ["chat"], "Orion (chat)"),
    ("Orion provider", ["chat"], "Orion (chat)"),
    ("Orion engine", ["chat"], "Orion (chat)"),
    ("Orion SERVER", ["chat"], "Orion (chat)"),
    ("Orion PROVIDER", ["chat"], "Orion (chat)"),
    ("Orion ENGINE", ["chat"], "Orion (chat)"),
    ("Orion engine", [], "Orion engine"),
    ("Orion-stt", ["transcriptions"], "Orion (transcription)"),
    ("Orion-STT", ["transcriptions"], "Orion (transcription)"),
    ("Orion-tts", ["speech"], "Orion (voice)"),
    ("Orion-TTS", ["speech"], "Orion (voice)"),
    ("Orion-stt", ["chat"], "Orion-stt (chat)"),
    ("Orion-tts", [], "Orion-tts"),
    ("Local Orion-stt server (provider)", ["transcriptions"], "Orion (transcription)"),
    ("Orion (CPU); internal", ["chat"], "Orion (CPU)"),
    ("Orion (GPU); internal", ["chat"], "Orion (GPU)"),
    ("Orion(STT)", ["chat"], "Orion(STT)"),
    ("Orion(TTS)", ["chat"], "Orion(TTS)"),
    ("Orion (STT)", ["chat"], "Orion (chat)"),
    ("Orion (cpu)", ["chat"], "Orion (cpu) (chat)"),
])
def test_each_strip_word_and_case_and_cleanup_order_is_pinned(name, caps, expected):
    assert presentation.engine_label({"name": name, "capabilities": caps}, "internal") == expected


@pytest.mark.parametrize("title", ["tts", "TTS", "stt", "STT"])
def test_bare_titles_and_recommendations_use_the_capability_label(title):
    row = image_row(title=title, recommended_model=title)
    assert row["title"] == row["recommended_model"] == "Images"


@pytest.mark.parametrize("match,near", [
    ("Draws on :8080.", "Draws on :8."),
    ("Holds a host for you.", "Hosts useful ideas."),
    ("Reads GGUF weights.", "Gguffy ideas become pictures."),
    ("A workflow for images.", "A workflowish way to draw."),
    ("A context window for art.", "A contextual window for art."),
    ("Draws via local engines.", "Draws via lovely engines."),
    ("Images served by friends.", "Images served beside friends."),
    ("Draws from /models.", "Draws from models."),
])
def test_each_technical_pattern_has_a_matching_and_near_miss_summary(match, near):
    assert presentation._is_technical_description(match) is True
    assert presentation._is_technical_description(near) is False
    assert image_row(match)["summary"] == "Generate or edit images from your prompts."
    assert image_row(near)["summary"] == near


@pytest.mark.parametrize("description,expected", [
    ("Draw something lovely. Ignore this technical workflow.", "Draw something lovely."),
    ("One two three four five six seven eight", "One two three four five six seven eight."),
    ("One two three four five six seven eight nine", "Generate or edit images from your prompts."),
    ("Draw something lovely!", "Draw something lovely!"),
    ("Draw something lovely?", "Draw something lovely?"),
    ("Draw something lovely...", "Draw something lovely."),
])
def test_sentence_word_limit_and_punctuation_are_not_data_rules(description, expected):
    assert image_row(description)["summary"] == expected


def test_service_and_unclassified_fallbacks_are_literal():
    assert presentation.service_presentation({}, "mystery-engine") == {
        "engine_label": "Mystery Engine", "capability_title": "AI",
        "capability_labels": [], "icon_key": "chat",
    }
    row = presentation.capability_presentation(Capability(intent="smell.infer", backend="test"))
    assert (row["capability"], row["capability_title"], row["icon_key"], row["summary"]) == (
        "other", "AI", "chat", "Use this configured AI capability.",
    )


def write_words(tmp_path, words):
    path = tmp_path / "custom-presentation-words.yml"
    path.write_text(yaml.safe_dump(words, sort_keys=False), encoding="utf-8")
    return path


def test_shipped_words_equal_the_literal_lists_in_order():
    data = yaml.safe_load(WORDS_FILE.read_text(encoding="utf-8"))
    assert data == SHIPPED
    assert list(data) == list(SHIPPED)
    for key in ("capability_engine_suffixes", "acronyms", "unclassified"):
        assert list(data[key]) == list(SHIPPED[key])


def test_loaded_words_keep_order_and_compile_every_technical_pattern(tmp_path):
    words = presentation._load_presentation_words(write_words(tmp_path, SHIPPED))
    for key, expected in SHIPPED.items():
        if key == "technical_patterns":
            assert all(isinstance(pattern, re.Pattern) for pattern in words[key])
            assert tuple(pattern.pattern for pattern in words[key]) == tuple(expected)
        elif isinstance(expected, list):
            assert words[key] == tuple(expected)
        elif isinstance(expected, dict):
            assert list(words[key].items()) == list(expected.items())
        else:
            assert words[key] == expected


def test_custom_file_changes_answers_at_import_and_is_never_read_again(tmp_path, monkeypatch):
    changed = deepcopy(SHIPPED)
    changed["acronyms"]["Npu"] = "NPU"
    changed["technical_patterns"].append(r"\bmarshmallow\b")
    custom = write_words(tmp_path, changed)
    default = presentation._PRESENTATION_WORDS_PATH
    original = Path.read_text
    reads = []

    def routed_read(path, *args, **kwargs):
        if path == default:
            reads.append(path)
            return original(custom, *args, **kwargs)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", routed_read)
    spec = importlib.util.spec_from_file_location("gateway._presentation_words_fixture", presentation.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert reads == [default]
    # File corruption after import cannot change the already loaded words.
    custom.write_text("not: [valid", encoding="utf-8")

    def no_read(path, *args, **kwargs):
        if path in (default, custom):
            raise AssertionError("presentation data reread during a request")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(re, "compile", lambda *a, **k: pytest.fail("pattern recompiled during a request"))
    for _ in range(2):
        assert isolated.engine_label({"name": "Orion Npu", "capabilities": ["chat"]}, "internal") == "Orion NPU (chat)"
        row = isolated.capability_presentation(
            Capability(intent="image.generate", backend="test", model="demo"),
            {"description": "Draws marshmallow clouds."},
        )
        assert row["summary"] == "Generate or edit images from your prompts."
    assert reads == [default]
    assert presentation.engine_label({"name": "Orion Npu", "capabilities": ["chat"]}, "internal") == "Orion Npu (chat)"
    assert image_row("Draws marshmallow clouds.")["summary"] == "Draws marshmallow clouds."


LIST_KEYS = [
    "provider_suffixes", "engine_prefixes", "engine_tails", "parenthesized_hardware",
    "hardware_label_suffixes", "bare_titles", "technical_patterns",
]


@pytest.mark.parametrize("key", LIST_KEYS)
@pytest.mark.parametrize("bad", [[], "not-a-list", [False], [""]], ids=["empty", "type", "entry-type", "blank"])
def test_every_list_refuses_bad_data_with_file_and_key(tmp_path, key, bad):
    data = deepcopy(SHIPPED)
    data[key] = bad
    path = write_words(tmp_path, data)
    with pytest.raises(ValueError) as exc:
        presentation._load_presentation_words(path)
    assert str(path) in str(exc.value) and key in str(exc.value)


@pytest.mark.parametrize("key,value,diagnostic", [
    ("schema_version", True, "schema_version"),
    ("schema_version", 2, "schema_version"),
    ("unexpected", ["word"], "unexpected"),
    ("acronyms", {}, "acronyms"),
    ("acronyms", [], "acronyms"),
    ("acronyms", {"Npu": False}, "acronyms.Npu"),
    ("acronyms", {"": "NPU"}, "acronyms"),
    ("capability_engine_suffixes", {}, "capability_engine_suffixes"),
    ("capability_engine_suffixes", {"smell": "xyz"}, "capability_engine_suffixes.smell"),
    ("capability_engine_suffixes", {"transcribe": []}, "capability_engine_suffixes.transcribe"),
    ("unclassified", {}, "unclassified"),
    ("unclassified", [], "unclassified"),
    ("unclassified", {**SHIPPED["unclassified"], "extra": "word"}, "unclassified.extra"),
    ("unclassified", {**SHIPPED["unclassified"], "summary": ""}, "unclassified.summary"),
    ("technical_patterns", ["["], "technical_patterns[0]"),
    ("technical_patterns", ["a{999999999999999999999999999}"], "technical_patterns[0]"),
    ("unclassified", {**SHIPPED["unclassified"], "service_capability": "missing"}, "unclassified.service_capability"),
])
def test_invalid_rows_and_patterns_name_the_file_and_exact_key(tmp_path, key, value, diagnostic):
    data = deepcopy(SHIPPED)
    data[key] = value
    path = write_words(tmp_path, data)
    with pytest.raises(ValueError) as exc:
        presentation._load_presentation_words(path)
    assert str(path) in str(exc.value) and diagnostic in str(exc.value)


@pytest.mark.parametrize("key", list(SHIPPED))
def test_missing_keys_are_refused(tmp_path, key):
    data = deepcopy(SHIPPED)
    del data[key]
    path = write_words(tmp_path, data)
    with pytest.raises(ValueError) as exc:
        presentation._load_presentation_words(path)
    assert str(path) in str(exc.value) and key in str(exc.value)


@pytest.mark.parametrize("text", ["[]", "null", "broken: [", "3"])
def test_bad_document_names_the_file_and_root(tmp_path, text):
    path = tmp_path / "bad-words.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        presentation._load_presentation_words(path)
    assert str(path) in str(exc.value) and "$" in str(exc.value)


def test_missing_file_names_the_file_and_root(tmp_path):
    path = tmp_path / "absent-words.yml"
    with pytest.raises(ValueError) as exc:
        presentation._load_presentation_words(path)
    assert str(path) in str(exc.value) and "$" in str(exc.value)


@pytest.mark.parametrize("key", [None, "acronyms", "capability_engine_suffixes", "unclassified"])
def test_maps_are_deeply_read_only(tmp_path, key):
    words = presentation._load_presentation_words(write_words(tmp_path, SHIPPED))
    mapping = words if key is None else words[key]
    with pytest.raises(TypeError):
        mapping["new"] = "changed"


@pytest.mark.parametrize("key", LIST_KEYS)
def test_lists_are_read_only_tuples(tmp_path, key):
    words = presentation._load_presentation_words(write_words(tmp_path, SHIPPED))
    assert isinstance(words[key], tuple)
    with pytest.raises(TypeError):
        words[key][0] = "changed"


def test_compiled_patterns_are_read_only(tmp_path):
    words = presentation._load_presentation_words(write_words(tmp_path, SHIPPED))
    with pytest.raises(AttributeError):
        words["technical_patterns"][0].pattern = "changed"
