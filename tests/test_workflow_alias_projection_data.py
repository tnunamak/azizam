"""Data-driven alias projection, independent of default-alias migration."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from gateway import compatibility
from gateway.workflow_openapi import aliases_by_workflow, match_alias_target


SHIPPED_RELATIONS = {
    "images": [{"intent": "image.generate", "target_prefix": "image-"}],
    "image_edits": [{"intent": "image.edit", "target_prefix": "image-"}],
    "videos": [
        {"intent": "video.generate", "target_prefix": "video-"},
        {"intent": "video.edit", "target_prefix": "video-"},
    ],
    "audio_generate": [{"intent": "audio.generate", "target_prefix": "audio-"}],
}


def _record(intent, short):
    return SimpleNamespace(id=f"{intent}.{short}", intent=intent)


def _install(monkeypatch, relations):
    rows = {
        operation: [SimpleNamespace(**row) for row in entries]
        for operation, entries in relations.items()
    }
    monkeypatch.setattr(
        compatibility, "COMPATIBILITY", SimpleNamespace(alias_operation_intents=rows)
    )


def _section(*pairs):
    return {"stable": [{"alias": alias, "resolves_to": target} for alias, target in pairs]}


def _document(relations):
    return {
        "schema_version": 1,
        "migration": {
            "include_capabilities": ["old"],
            "exclude_capabilities": ["new"],
            "config_mapping": "mapping",
        },
        "legacy_alias_tables": {},
        "limit_key_aliases": {},
        "legacy_audio_result_keys": ["payload"],
        "alias_operation_intents": relations,
    }


def test_unfamiliar_operation_intent_and_prefix_are_data(monkeypatch):
    _install(monkeypatch, {
        "spectrograms": [{"intent": "spectral.compose", "target_prefix": "archive-"}],
    })
    record = _record("spectral.compose", "draft")
    aliases = {"spectrograms": _section(("preferred", "archive-draft"))}
    assert aliases_by_workflow([record], aliases) == {record.id: ["preferred"]}


def test_match_target_uses_declared_prefix_not_intent_family(monkeypatch):
    _install(monkeypatch, {
        "spectrograms": [{"intent": "spectral.compose", "target_prefix": "archive-"}],
    })
    assert match_alias_target("archive-draft-edit", "spectral.compose", ["draft"]) == "draft"
    assert match_alias_target("spectral-draft", "spectral.compose", ["draft"]) is None
    assert match_alias_target("draft", "unrelated.compose", ["draft"]) == "draft"
    assert match_alias_target("archive-draft", "unrelated.compose", ["draft"]) is None


def test_same_short_id_does_not_cross_operation_intents(monkeypatch):
    _install(monkeypatch, {
        "first": [{"intent": "nova.compose", "target_prefix": "old-"}],
        "second": [{"intent": "nova.revise", "target_prefix": "old-"}],
    })
    first, second = _record("nova.compose", "shared"), _record("nova.revise", "shared")
    assert aliases_by_workflow([first, second], {
        "first": _section(("one", "old-shared")),
        "second": _section(("two", "old-shared")),
    }) == {first.id: ["one"], second.id: ["two"]}


def test_exact_canonical_target_wins_over_short_name_collision(monkeypatch):
    _install(monkeypatch, {
        "compose": [
            {"intent": "nova.compose", "target_prefix": "old-"},
            {"intent": "nova.revise", "target_prefix": "old-"},
        ],
    })
    exact = _record("nova.compose", "shared")
    collision = _record("nova.revise", exact.id)
    assert aliases_by_workflow([collision, exact], {
        "compose": _section(("one", exact.id)),
    }) == {exact.id: ["one"]}


def test_canonical_target_outside_operation_does_not_fall_back(monkeypatch):
    _install(monkeypatch, {
        "compose": [{"intent": "nova.compose", "target_prefix": "old-"}],
    })
    other = _record("nova.revise", "shared")
    collision = _record("nova.compose", other.id)
    assert aliases_by_workflow([collision, other], {
        "compose": _section(("one", other.id)),
    }) == {}


@pytest.mark.parametrize(("operation", "intent", "prefix"), [
    ("images", "image.generate", "image-"),
    ("image_edits", "image.edit", "image-"),
    ("videos", "video.generate", "video-"),
    ("videos", "video.edit", "video-"),
    ("audio_generate", "audio.generate", "audio-"),
])
def test_every_shipped_relation_preserves_legacy_matches(monkeypatch, operation, intent, prefix):
    _install(monkeypatch, SHIPPED_RELATIONS)
    record = _record(intent, "sample")
    aliases = {operation: _section(
        ("raw", "sample"),
        ("prefixed", prefix + "sample"),
        ("suffix", "sample-edit"),
        ("both", prefix + "sample-edit"),
        ("raw", "sample"),
        ("unconfigured", None),
        ("unknown", "missing"),
    )}
    assert aliases_by_workflow([record], aliases) == {
        record.id: ["raw", "prefixed", "suffix", "both"],
    }
    assert match_alias_target(prefix + "sample", intent, [prefix + "sample", "sample"]) == prefix + "sample"


def test_shared_legacy_short_name_retains_both_compatible_intents(monkeypatch):
    _install(monkeypatch, SHIPPED_RELATIONS)
    generated, edited = _record("video.generate", "shared"), _record("video.edit", "shared")
    assert aliases_by_workflow([generated, edited], {
        "videos": _section(("legacy", "video-shared")),
    }) == {generated.id: ["legacy"], edited.id: ["legacy"]}


def test_duplicate_aliases_keep_first_seen_order_across_operations(monkeypatch):
    _install(monkeypatch, {
        "first": [{"intent": "nova.compose", "target_prefix": "old-"}],
        "second": [{"intent": "nova.compose", "target_prefix": "old-"}],
    })
    record = _record("nova.compose", "sample")
    assert aliases_by_workflow(iter([record]), {
        "first": _section(("one", "old-sample"), ("one", record.id), ("two", "sample")),
        "second": _section(("two", record.id), ("three", "sample")),
    }) == {record.id: ["one", "two", "three"]}


def test_empty_unconfigured_and_unmapped_sections(monkeypatch):
    _install(monkeypatch, SHIPPED_RELATIONS)
    records = [_record("image.generate", "sample")]
    for aliases in (None, {}, {"images": {}}, {"images": None}, {"images": {"stable": []}},
                    {"images": _section(("image", None), ("image-fast", ""))},
                    {"unmapped": _section(("ignored", "sample"))}):
        assert aliases_by_workflow(records, aliases) == {}
    assert aliases_by_workflow([], {"images": _section(("image", "sample"))}) == {}
    _install(monkeypatch, {})
    assert aliases_by_workflow(records, {"images": _section(("image", "sample"))}) == {}


def test_shipped_data_matches_frozen_relations_and_unconfigured_defaults(monkeypatch):
    loaded = compatibility.load_compatibility()
    actual = {
        operation: [row.model_dump() for row in entries]
        for operation, entries in loaded.alias_operation_intents.items()
    }
    assert actual == SHIPPED_RELATIONS
    defaults = yaml.safe_load(
        (compatibility.REGISTRY / "defaults" / "model_aliases.yml").read_text(encoding="utf-8")
    )
    aliases = {}
    for operation in SHIPPED_RELATIONS:
        assert all(target is None for target in defaults[operation]["stable"].values())
        aliases[operation] = _section(*defaults[operation]["stable"].items())
    monkeypatch.setattr(compatibility, "COMPATIBILITY", loaded)
    assert aliases_by_workflow([_record("image.generate", "sample")], aliases) == {}


def test_existing_loader_accepts_unfamiliar_valid_relations(tmp_path, monkeypatch):
    relations = {"spectrograms": [{"intent": "spectral.compose", "target_prefix": "archive-"}]}
    path = tmp_path / "compatibility.yml"
    path.write_text(yaml.safe_dump(_document(relations)), encoding="utf-8")
    loaded = compatibility.load_compatibility(path)
    monkeypatch.setattr(compatibility, "COMPATIBILITY", loaded)
    record = _record("spectral.compose", "draft")
    assert aliases_by_workflow([record], {
        "spectrograms": _section(("one", "archive-draft")),
    }) == {record.id: ["one"]}


def test_contract_accepts_empty_relation_and_consistent_shared_intent():
    assert compatibility.CompatibilityContract.model_validate(
        _document({})
    ).alias_operation_intents == {}
    relations = {
        "first": [{"intent": "nova.compose", "target_prefix": "old-"}],
        "second": [{"intent": "nova.compose", "target_prefix": "old-"}],
    }
    contract = compatibility.CompatibilityContract.model_validate(_document(relations))
    assert list(contract.alias_operation_intents) == ["first", "second"]


def test_contract_rejects_malformed_relations():
    row = {"intent": "nova.compose", "target_prefix": "old-"}
    invalid = [
        None, [], {"Upper": [row]}, {"bad-operation": [row]}, {"": [row]},
        {"compose": []}, {"compose": row}, {"compose": ["nova.compose"]},
        {"compose": [dict(row, intent="")]}, {"compose": [dict(row, intent="two words")]},
        {"compose": [dict(row, intent=17)]}, {"compose": [dict(row, target_prefix="")]},
        {"compose": [dict(row, target_prefix="two words")]},
        {"compose": [dict(row, target_prefix=False)]},
        {"compose": [{"intent": "nova.compose"}]},
        {"compose": [{"target_prefix": "old-"}]},
        {"compose": [dict(row, extra="not allowed")]},
        {"compose": [row, deepcopy(row)]},
        {"first": [row], "second": [dict(row, target_prefix="different-")]},
    ]
    for relations in invalid:
        with pytest.raises(ValidationError, match="alias_operation_intents"):
            compatibility.CompatibilityContract.model_validate(_document(relations))


def test_contract_requires_relation_section():
    document = _document({})
    del document["alias_operation_intents"]
    with pytest.raises(ValidationError, match="alias_operation_intents"):
        compatibility.CompatibilityContract.model_validate(document)


def test_loader_rejects_duplicate_relation_keys(tmp_path):
    path = tmp_path / "compatibility.yml"
    document = yaml.safe_dump(_document({}))
    document = document.replace("alias_operation_intents: {}", """alias_operation_intents:
  compose:
    - intent: nova.compose
      target_prefix: old-
  compose:
    - intent: nova.revise
      target_prefix: old-""")
    path.write_text(document, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError), match="(?i)duplicate"):
        compatibility.load_compatibility(path)
