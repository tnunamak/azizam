"""OpenAI model families are data (registry/openai_model_families.yml), not a chain of name checks in the root module."""
from __future__ import annotations

import copy

import pytest
import yaml

import proxy
from gateway import openai_model_families as families
from tests.support.config_snapshot import section_published

SHIPPED = yaml.safe_load(families.FAMILIES_FILE.read_text(encoding="utf-8"))

# Captured from the code chain this file replaced; each id is one the generated model list does not hold.
EXPECTED = {
    "dall-e-2": "images", "dall-e-9": "images", "gpt-image-9": "images", "chatgpt-image-x": "images",
    "tts-1": "speech", "tts-1-hd": "speech", "tts-2": "speech", "kokoro-tts": "speech", "a-tts-b": "speech",
    "gpt-5-tts": "speech", "whisper-1": "transcriptions", "whisper-large": "transcriptions",
    "gpt-diarize": "transcriptions", "whisper-tts": "transcriptions", "my-embedding": "embeddings",
    "sora-embedding": "embeddings", "sora-3": "video", "omni-moderation-9": "moderation",
    "o3-deep-research-x": "deep_research", "gpt-realtime-9": "audio_chat", "gpt-audio-9": "audio_chat",
    "chatgpt-4o-realtime": "audio_chat", "gpt-5": "chat", "o1-x": "chat", "o4-mini": "chat", "codex-mini": "chat",
    "computer-use-x": "chat", "chatgpt-x": "chat", "llama-3": None, "qwen3": None, "o2": None,
}


def test_the_shipped_rules_give_the_families_the_old_code_gave():
    assert {model_id: families.FAMILIES.category_for(model_id) for model_id in EXPECTED} == EXPECTED


def test_a_new_family_needs_one_row_and_no_code(monkeypatch):
    data = copy.deepcopy(SHIPPED)
    data["rules"].insert(0, {"category": "video", "match": [[{"prefix": "lumen-"}]]})
    monkeypatch.setattr(proxy.openai_model_families, "FAMILIES", families.parse(data))
    with section_published("model_compatibility", {"aliases": {}}):
        assert proxy.infer_openai_model_category("lumen-1") == "video"
        assert proxy.infer_openai_model_category("gpt-5") == "chat"


def test_an_owner_alias_still_wins_over_a_family_rule():
    with section_published("model_compatibility", {"aliases": {"sora-2": {"category": "images"}}}):
        assert proxy.infer_openai_model_category("sora-2") == "images"


def test_every_group_of_a_rule_must_hold_and_the_first_rule_wins():
    parsed = families.parse({"schema_version": 1, "rules": [
        {"category": "first", "match": [[{"prefix": "a-"}], [{"suffix": "-z"}]]},
        {"category": "second", "match": [[{"prefix": "a-"}]]},
    ]})
    assert parsed.category_for("a-mid-z") == "first"
    assert parsed.category_for("a-mid") == "second"
    assert parsed.category_for("b-mid-z") is None


@pytest.mark.parametrize("broken, cause", [
    ({"schema_version": 2}, "schema_version"),
    ({"schema_version": 1, "extra": {}}, "unknown keys"),
    ({"schema_version": 1, "exact": {"a": ""}}, "exact.a"),
    ({"schema_version": 1, "rules": [{"category": "x"}]}, "exactly category and match"),
    ({"schema_version": 1, "rules": [{"category": "x", "match": []}]}, "nonempty list of groups"),
    ({"schema_version": 1, "rules": [{"category": "x", "match": [[{"regex": "a"}]]}]}, "one of prefix"),
    ({"schema_version": 1, "rules": [{"category": "x", "match": [[{"prefix": "a", "suffix": "b"}]]}]}, "one of prefix"),
    ({"schema_version": 1, "rules": [{"category": "", "match": [[{"prefix": "a"}]]}]}, "category"),
])
def test_a_malformed_file_is_refused_with_its_cause(broken, cause):
    with pytest.raises(families.FamiliesFileError, match=cause):
        families.parse(broken)
