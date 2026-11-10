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


def test_the_generated_model_list_agrees_with_the_family_rows():
    """Every family the generated list stores is the one the rows give, so the two sources cannot drift apart."""
    generated = yaml.safe_load((families.FAMILIES_FILE.parent / "openai_model_aliases.generated.yml").read_text(encoding="utf-8"))
    drift = {}
    for row in generated["models"]:
        family = families.FAMILIES.category_for(proxy.normalize_model_id(row["id"]))
        if (family or "unknown") != row["category"]:
            drift[row["id"]] = (row["category"], family)
    assert drift == {}


# -- refresh-openai-models: the listing, classified by the rows, written to the generated list -----------------------

LISTING_URL = "https://models.example.com/v1/models"


class _Listing:
    """The model listing a refresh reads: one GET, answered with *status* and *body*; no network."""

    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body, self.gets = status, body, []
        self.headers, self.text = {}, ""

    def json(self):
        assert self._body is not None, "a refused listing must not be parsed"
        return self._body

    def client(self, **_):
        listing = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **_):
                listing.gets.append(url)
                return listing

        return _Client()


def _refresh(monkeypatch, tmp_path, listing: _Listing, **compat) -> tuple[int, object]:
    import asyncio

    target = tmp_path / "openai_model_aliases.generated.yml"
    monkeypatch.setenv("OPENAI_MODEL_DISCOVERY_API_KEY", "fixture-value")
    monkeypatch.setattr(proxy.engine_egress, "async_client", listing.client)
    monkeypatch.setattr(proxy, "openai_model_aliases_file", lambda: target)
    with section_published("model_compatibility", {"aliases": {}, "openai_models_api_url": LISTING_URL, **compat}):
        code = asyncio.run(proxy.refresh_openai_model_aliases())
    assert listing.gets == [LISTING_URL]
    return code, target


def test_a_refresh_classifies_an_unfamiliar_id_by_a_new_row_and_writes_it(monkeypatch, tmp_path):
    rows = tmp_path / "families.yml"
    rows.write_text(yaml.safe_dump({"schema_version": 1, "exact": {}, "rules": [
        {"category": "video", "match": [[{"prefix": "lumen-"}]]}]}), encoding="utf-8")
    monkeypatch.setattr(proxy.openai_model_families, "FAMILIES", families.load(rows))
    owner = sorted(proxy.OPENAI_PUBLIC_MODEL_OWNERS)[0]
    listing = _Listing(200, {"data": [{"id": "lumen-fixture-1", "owned_by": owner, "created": 123}]})
    code, target = _refresh(monkeypatch, tmp_path, listing)
    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert code == 0 and written["source"] == LISTING_URL and written["generated_at"]
    assert written["models"] == [{"id": "lumen-fixture-1", "category": "video", "created": 123, "owned_by": owner}]


@pytest.mark.parametrize("include_private, kept", [
    (False, ["lumen-public"]),
    (True, ["ft:lumen-public", "lumen-private", "lumen-public", "lumen-public:custom"]),
])
def test_a_refresh_keeps_private_and_fine_tuned_ids_only_when_asked(monkeypatch, tmp_path, include_private, kept):
    owner = sorted(proxy.OPENAI_PUBLIC_MODEL_OWNERS)[0]
    stranger = "fixture-account"
    assert stranger not in proxy.OPENAI_PUBLIC_MODEL_OWNERS
    listing = _Listing(200, {"data": [
        {"id": "lumen-public:custom", "owned_by": owner}, {"id": "lumen-private", "owned_by": stranger},
        {"id": "", "owned_by": owner}, {"id": "ft:lumen-public", "owned_by": owner}, {"id": "lumen-public", "owned_by": owner},
    ]})
    code, target = _refresh(monkeypatch, tmp_path, listing, include_private=include_private)
    assert code == 0
    assert [row["id"] for row in yaml.safe_load(target.read_text(encoding="utf-8"))["models"]] == kept


def test_a_refused_listing_leaves_the_generated_list_as_it_was(monkeypatch, tmp_path, capsys):
    target = tmp_path / "openai_model_aliases.generated.yml"
    target.write_bytes(b"generated_at: before\nmodels: []\n")
    code, written = _refresh(monkeypatch, tmp_path, _Listing(503))
    assert code == 1 and written == target and target.read_bytes() == b"generated_at: before\nmodels: []\n"
    assert "refresh failed" in capsys.readouterr().err
