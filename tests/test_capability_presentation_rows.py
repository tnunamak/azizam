"""Literal capability presentation maps and real row-loader contracts."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gateway" / "capability_ids.py"
PRESENTATION = ROOT / "gateway" / "model_presentation.py"
DATA = ROOT / "registry" / "capability_words.yml"

# Frozen from c8a7d134. Expected contents must not come from the new rows.
ICONS = {
    "chat": "chat", "image": "image", "video": "video", "speech": "speech",
    "transcribe": "transcribe", "music": "music", "sfx": "sfx", "decide": "decide",
    "embed": "embed", "rerank": "rerank", "mesh": "mesh", "audio": "sfx",
}
ALIASES = {
    "chat": "default", "image": "image", "video": "video", "music": "music",
    "sfx": "sfx", "speech": "tts", "transcribe": "stt", "decide": "decide",
    "embed": "embed", "rerank": "rerank",
}
SUMMARIES = {
    "image": "Generate or edit images from your prompts.",
    "video": "Video from a prompt or image.",
    "mesh": "A 3D model from one picture.",
    "music": "Songs and instrumentals from a prompt.",
    "sfx": "Sound effects from a description.",
    "speech": "Turn text into spoken audio.",
    "transcribe": "Turn recorded speech into text.",
    "decide": "Choose an outcome from your options.",
    "embed": "Create vectors for search and similarity.",
    "rerank": "Order search results by relevance.",
    "audio": "Generate audio from your prompts.",
}
TABLES = [
    ("CAPABILITY_ICON_KEYS", ICONS),
    ("RECOMMENDED_ALIASES", ALIASES),
    ("CAPABILITY_DESCRIPTIONS", SUMMARIES),
]


def _document():
    return {
        "schema_version": 1,
        "capabilities": [
            {"id": "chat", "label": "Chat"},
            {"id": "speech", "label": "Voice"},
            {"id": "transcribe", "label": "Transcription"},
            {"id": "embed", "label": "Document search"},
            {"id": "sfx", "label": "Sound effects"},
        ],
        "protocol_words": {"tts": "speech"},
        "route_intents": {"chat.generate": "chat"},
    }


def _load(source, name):
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path, document):
    source = tmp_path / "gateway" / "capability_ids.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    data = tmp_path / "registry" / "capability_words.yml"
    data.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    data.write_text(text, encoding="utf-8")
    return source, data


def _presentation(module, monkeypatch):
    # Import the real consumer against the isolated data loader, without
    # reloading shared modules or teaching it the fixture's capability name.
    monkeypatch.setitem(sys.modules, "gateway.capability_ids", module)
    return _load(PRESENTATION, "_capability_presentation_under_test")


@pytest.mark.parametrize("name, expected", TABLES)
def test_shipped_presentation_maps_match_literal_contents(tmp_path, name, expected):
    source, _ = _tree(tmp_path, DATA.read_text(encoding="utf-8"))
    module = _load(source, "_capability_presentation_rows")
    assert hasattr(module, name), f"the row loader must export {name}"
    assert dict(getattr(module, name)) == expected


@pytest.mark.parametrize("name, expected", TABLES)
def test_consumer_public_names_share_the_read_only_row_maps(tmp_path, monkeypatch, name, expected):
    source, _ = _tree(tmp_path, DATA.read_text(encoding="utf-8"))
    module = _load(source, "_capability_presentation_rows")
    assert hasattr(module, name), f"the row loader must export {name}"
    presentation = _presentation(module, monkeypatch)
    assert getattr(presentation, name) is getattr(module, name)
    assert dict(getattr(presentation, name)) == expected


@pytest.mark.parametrize("icon, expected", [(None, "fixture"), ("sfx", "sfx"), ("later", "later")])
def test_a_new_capability_changes_real_presentation_from_rows_only(tmp_path, monkeypatch, icon, expected):
    document = _document()
    row = {"id": "fixture", "label": "Fixture", "recommended_alias": "fixture-use",
           "summary": "Make fixture artifacts."}
    if icon is not None:
        row["icon"] = icon
    document["capabilities"].extend([row, {"id": "later", "label": "Later"}])
    source, _ = _tree(tmp_path, document)
    module = _load(source, "_capability_presentation_rows")
    presentation = _presentation(module, monkeypatch)
    result = presentation.capability_presentation(
        SimpleNamespace(intent="unlisted.operation", capability="fixture", model="sample", tags=())
    )
    assert (result["capability"], result["capability_title"]) == ("fixture", "Fixture")
    assert result["icon_key"] == expected
    assert result["summary"] == "Make fixture artifacts."
    assert presentation.RECOMMENDED_ALIASES["fixture"] == "fixture-use"
    assert module.CAPABILITY_ICON_KEYS["later"] == "later"
    assert "later" not in module.RECOMMENDED_ALIASES
    assert "later" not in module.CAPABILITY_DESCRIPTIONS


def test_omitted_fields_preserve_old_rows_and_loader_return_shapes(tmp_path):
    source, data = _tree(tmp_path, _document())
    module = _load(source, "_capability_presentation_rows")
    assert hasattr(module, "load_capability_presentation"), "a file loader must expose the three presentation maps"
    labels, protocols, words, intents = module._load_capability_data(data)
    assert list(labels) == ["chat", "speech", "transcribe", "embed", "sfx"]
    assert dict(protocols) == {}
    assert dict(words) == {"tts": "speech"}
    assert dict(intents) == {"tts": "speech", "chat.generate": "chat"}
    assert module.load_capability_words(data) == (words, intents)
    icons, aliases, summaries = module.load_capability_presentation(data)
    assert list(icons.items()) == [(key, key) for key in ("chat", "speech", "transcribe", "embed", "sfx")]
    assert dict(aliases) == dict(summaries) == {}


@pytest.mark.parametrize("field", ["icon", "recommended_alias", "summary"])
@pytest.mark.parametrize("value", [None, "", "   ", 7, True, [], {}, "\n\t"])
def test_invalid_presentation_fields_name_file_row_field_and_requirement(tmp_path, field, value):
    document = _document()
    document["capabilities"][1][field] = value
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _load(source, "_capability_presentation_rows")
    message = str(error.value)
    assert "capability_words.yml" in message
    assert "row 2" in message
    assert field in message
    assert "non-empty string" in message


@pytest.mark.parametrize("icon", ["unlisted", " sfx", "sfx "])
def test_icon_must_name_an_exact_loaded_capability(tmp_path, icon):
    document = _document()
    document["capabilities"][1]["icon"] = icon
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _load(source, "_capability_presentation_rows")
    message = str(error.value)
    assert "capability_words.yml" in message and "row 2" in message
    assert "icon" in message and "unknown capability" in message


@pytest.mark.parametrize("key", ["icon_key", "description", "unexpected"])
def test_unknown_row_keys_remain_refused_with_file_and_row(tmp_path, key):
    document = _document()
    document["capabilities"][1][key] = "not a declared field"
    source, _ = _tree(tmp_path, document)
    with pytest.raises(ValueError) as error:
        _load(source, "_capability_presentation_rows")
    message = str(error.value)
    assert "capability_words.yml" in message and "row 2" in message
    assert key in message and "unknown key" in message


@pytest.mark.parametrize("name, expected", TABLES)
@pytest.mark.parametrize("surface", ["module", "loader", "consumer"])
def test_presentation_maps_cannot_be_changed(tmp_path, monkeypatch, name, expected, surface):
    source, data = _tree(tmp_path, DATA.read_text(encoding="utf-8"))
    module = _load(source, "_capability_presentation_rows")
    assert hasattr(module, name), f"the row loader must export {name}"
    if surface == "module":
        table = getattr(module, name)
    elif surface == "consumer":
        table = getattr(_presentation(module, monkeypatch), name)
    else:
        tables = module.load_capability_presentation(data)
        table = tables[[item[0] for item in TABLES].index(name)]
    key = next(iter(expected))
    with pytest.raises(TypeError):
        table[key] = "changed"
    with pytest.raises(TypeError):
        del table[key]


def test_presentation_maps_load_once_and_reload_only_explicitly(tmp_path):
    document = _document()
    document["capabilities"][1].update(icon="sfx", recommended_alias="voice", summary="Read text aloud.")
    source, data = _tree(tmp_path, document)
    module = _load(source, "_capability_presentation_rows")
    document["capabilities"][1].update(icon="chat", recommended_alias="say", summary="Speak changed words.")
    data.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert module.CAPABILITY_ICON_KEYS["speech"] == "sfx"
    assert module.RECOMMENDED_ALIASES["speech"] == "voice"
    assert module.CAPABILITY_DESCRIPTIONS["speech"] == "Read text aloud."
    icons, aliases, summaries = module.load_capability_presentation(data)
    assert (icons["speech"], aliases["speech"], summaries["speech"]) == ("chat", "say", "Speak changed words.")
    data.unlink()
    assert module.CAPABILITY_DESCRIPTIONS["speech"] == "Read text aloud."
