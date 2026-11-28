"""The ``resource_class`` fact (CODE_TO_DATA_S4_S8, S5): admission's class is record data, not a table keyed by intent.

The release file states it for every workflow with the value the admission table gave that workflow's intent. A stored
policy block written before the fact existed is completed once from the same values, so what admission does for it does
not change; a block that lacks any other fact is not completed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gateway.workflow_policy import (
    FACT_NAMES,
    facts_from_mapping,
    release_policy,
    resource_class_for_intent,
    stored_facts,
)

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "manifests" / "workflows"
FOUR = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "tags": []}


def test_the_resource_class_is_one_of_the_facts():
    assert "resource_class" in FACT_NAMES


def test_every_bundled_workflow_states_the_class_its_intent_had_before():
    # The values the intent-keyed admission table held, read here as an oracle written out by hand.
    before = {"image.generate": "image", "image.edit": "image", "audio.generate": "audio", "video.generate": "video"}
    policy = release_policy()
    for path in sorted(BUNDLED.glob("*.yml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert policy[manifest["id"]].resource_class == before.get(manifest["intent"], "media"), manifest["id"]


def test_a_block_without_the_class_is_not_facts_until_it_is_completed():
    assert facts_from_mapping(FOUR) is None
    completed = stored_facts(FOUR, "video.generate")
    assert completed is not None and completed.resource_class == "video"
    assert stored_facts(FOUR, "unlisted.intent").resource_class == "media"
    assert stored_facts(FOUR, None).resource_class == "media"


def test_completion_never_fills_another_fact_and_never_overrides_a_stated_class():
    assert stored_facts({k: v for k, v in FOUR.items() if k != "exposure"}, "video.generate") is None
    assert stored_facts({**FOUR, "resource_class": "audio"}, "video.generate").resource_class == "audio"
    assert stored_facts({**FOUR, "resource_class": ""}, "video.generate") is None
    assert stored_facts("not a mapping", "video.generate") is None


def test_the_intent_data_names_the_class_of_each_intent_it_lists():
    assert [resource_class_for_intent(i) for i in ("audio.generate", "music.generate", "image.identity")] == ["audio", "audio", "image"]
