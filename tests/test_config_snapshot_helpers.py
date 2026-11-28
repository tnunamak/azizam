"""The test helpers that publish config snapshots put every patched derived value back when their block ends."""
from __future__ import annotations

import proxy
from tests.support.config_snapshot import derived_patch, publish_section

NAME = "GRAPH_ENABLED"


def test_a_section_published_inside_a_derived_patch_loses_the_patch_when_the_block_ends(monkeypatch):
    before = proxy.runtime().derived[NAME]
    with derived_patch(NAME, not before):
        publish_section(monkeypatch, "audio", proxy.config.get("audio"))
        assert proxy.runtime().derived[NAME] == (not before)
    assert proxy.runtime().derived[NAME] == before


def test_nested_patches_around_a_publish_unwind_one_level_at_a_time(monkeypatch):
    before = proxy.runtime().derived[NAME]
    with derived_patch(NAME, not before):
        with derived_patch(NAME, before):
            publish_section(monkeypatch, "audio", proxy.config.get("audio"))
            assert proxy.runtime().derived[NAME] == before
        with derived_patch(NAME, not before):
            publish_section(monkeypatch, "audio", proxy.config.get("audio"))
        assert proxy.runtime().derived[NAME] == (not before)
    assert proxy.runtime().derived[NAME] == before


def test_a_backends_publish_inside_a_patch_derives_afresh(monkeypatch):
    before = proxy.runtime().derived[NAME]
    with derived_patch(NAME, not before):
        publish_section(monkeypatch, "backends", proxy.config.get("backends"))
        assert proxy.runtime().derived[NAME] == before
    assert proxy.runtime().derived[NAME] == before
