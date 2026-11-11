"""An audio workflow's id is its registry record's id, and audio records are the media_job_intents audio rows."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway import media_job_intents
from tests.support.access import access_context
from tests.support.audio_workflow import audio_manifest
from tests.support.config_snapshot import derived_patch

ODD_ID = "catalog-entry-17"


def _record(record_id: str, intent: str, name: str) -> SimpleNamespace:
    manifest = audio_manifest(name, {"prompt": [("1", "text")]})
    manifest["id"], manifest["intent"] = record_id, intent
    return SimpleNamespace(id=record_id, intent=intent, manifest=manifest)


def _registry(*records: SimpleNamespace):
    return patch.object(proxy.GATEWAY_WORKFLOW_REGISTRY, "records", new=lambda: iter(records))


class _Json:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return dict(self._body)


def _spy_policy_requests():
    seen: list[str | None] = []
    real = proxy.make_policy_request

    def spy(*args, **kwargs):
        seen.append(kwargs.get("workflow"))
        return real(*args, **kwargs)

    return seen, patch.object(proxy, "make_policy_request", new=spy)


def test_an_audio_record_with_an_unrelated_id_is_found_by_its_name():
    with _registry(_record(ODD_ID, "audio.generate", "fx")):
        assert proxy._audio_workflow_id("fx") == ODD_ID


def test_a_name_that_no_record_names_has_no_id():
    with _registry(_record(ODD_ID, "audio.generate", "fx")):
        assert proxy._audio_workflow_id("other") is None


def test_the_listing_carries_the_records_id_to_the_policy_check():
    seen, spy = _spy_policy_requests()
    cfg = {"max_seconds": 30}
    with _registry(_record(ODD_ID, "music.generate", "fx")), spy, patch.object(proxy, "_audio_workflow_table", new=lambda: {"fx": cfg}):
        models = proxy._audio_models_for_caller(access_context())
    assert seen == [ODD_ID]
    assert [m["id"] for m in models] == ["fx"]


def test_a_listed_name_without_a_record_is_not_offered():
    with _registry(), patch.object(proxy, "_audio_workflow_table", new=lambda: {"fx": {}}):
        assert proxy._audio_models_for_caller(access_context()) == []


def test_the_route_policy_request_carries_the_records_id():
    seen, spy = _spy_policy_requests()
    with _registry(_record(ODD_ID, "audio.generate", "fx")), spy, derived_patch("COMFY_ENABLED", True), \
            patch.object(proxy, "_service_availability_response", new=lambda *_a, **_k: None), \
            patch.object(proxy, "_resolve_audio_workflow", return_value=("fx", {"file": "x.json"})), \
            patch.object(proxy, "_load_audio_workflow_file", new=lambda _cfg: {"1": {"inputs": {"text": ""}}}), \
            patch.object(proxy, "_build_graph_submit_payload", side_effect=RuntimeError("stop before the wire")):
        response = asyncio.run(proxy.proxy_audio_generate(_Json({"model": "fx", "prompt": "x"}), access_context()))
    assert ODD_ID in seen, (getattr(response, "status_code", None), getattr(response, "body", None), seen)


def test_a_resolved_name_with_no_record_is_an_unknown_model_on_the_route():
    with _registry(), derived_patch("COMFY_ENABLED", True), \
            patch.object(proxy, "_service_availability_response", new=lambda *_a, **_k: None), \
            patch.object(proxy, "_resolve_audio_workflow", return_value=("fx", {"file": "x.json"})):
        response = asyncio.run(proxy.proxy_audio_generate(_Json({"model": "fx", "prompt": "x"}), access_context()))
    assert response.status_code == 404 or json.loads(response.body)["error"]["code"] == "model_not_found"


def test_a_record_whose_intent_is_not_an_audio_row_is_not_an_audio_record():
    assert "audio.fake" not in media_job_intents.AUDIO_INTENTS
    with _registry(_record("audio.generate.imposter", "audio.fake", "fx"), _record(ODD_ID, "sfx.generate", "fy")):
        assert [r.id for r in proxy._audio_workflow_records()] == [ODD_ID]
        assert proxy._audio_workflow_id("fx") is None


def test_every_shipped_audio_record_has_an_audio_row_and_the_spelled_id():
    audio = [r for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records() if r.intent.startswith(("audio.", "music.", "sfx."))]
    assert audio
    assert all(r.intent in media_job_intents.AUDIO_INTENTS for r in audio)
    assert {r.id for r in audio} == {r.id for r in proxy._audio_workflow_records()}
