"""Freeze the shipped storage vocabulary and compatibility behavior before extraction."""
from typing import get_args

from gateway import owner_projection as projection
from gateway import stt_chunk_transform_migration as migration
from gateway.stable_aliases import LEGACY_ALIAS_TABLES, fold_workflow_aliases, resolve_alias
from gateway.video_limits import limits_for_workflow


def test_shipped_storage_and_compatibility_golden():
    kinds = ("image", "video", "audio", "voice", "other")
    assert get_args(projection.StorageView.model_fields["media_kind"].annotation)[0].__args__ == kinds
    for kind in kinds:
        assert projection.project("storage", {"category": "gateway", "media_kind": kind}).media_kind == kind
    assert projection.project("storage", {"category": "gateway", "media_kind": "undeclared"}).media_kind is None
    assert LEGACY_ALIAS_TABLES == (("videos", "videos"), ("audio_generate", "audio"))
    for spelling in ("transcriptions", "translations", "translation", "stt", "audio.transcriptions", "audio.translations"):
        cfg = {}
        assert migration.attach_timing_row(cfg, [spelling])
        assert cfg == {"transforms": [{"kind": "drop-params", "phase": "response", "drop": ["timing"]}]}
        assert not migration.attach_timing_row(cfg, [spelling])
        assert not migration.attach_timing_row({}, [spelling, "chat"])
    cfg = {"model_aliases": {"videos": {"stable": {"clip": "video-ltx-2.5-long"}},
                              "audio_generate": {"stable": {"sound": "tangoflux"}}},
           "videos": {}, "audio": {}}
    fold_workflow_aliases(cfg)
    assert cfg["videos"]["aliases"] == {"clip": "video-ltx-2.5-long"}
    assert cfg["audio"]["aliases"] == {"sound": "tangoflux"}
    assert resolve_alias(cfg["model_aliases"], "videos", "clip").target == cfg["videos"]["aliases"]["clip"]
    limits = {"video-ltx-2.5-long": {"max_total_seconds": 40}, "video-scail-2-replace": {"max_seconds": 10}}
    assert limits_for_workflow(limits, "video.generate.ltx-2.5-long") == {"max_total_seconds": 40}
    assert limits_for_workflow(limits, "video.edit.scail-2-replace") == {"max_seconds": 10}
