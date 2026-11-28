"""A graph media job admits against the resource class and graph family its intent row declares."""

from __future__ import annotations

import pytest

import proxy
from gateway import media_job_intents

# The pairs each handler typed by hand before the row table drove them.
SHIPPED = {
    "image.generate": ("image", proxy._GRAPH_CAPABILITY),
    "image.edit": ("image", proxy._GRAPH_CAPABILITY),
    "image.variation": ("image", proxy._GRAPH_CAPABILITY),
    "video.generate": ("video", proxy._VIDEO_GRAPH_CAPABILITY),
    "video.extend": ("video", proxy._VIDEO_GRAPH_CAPABILITY),
    "video.remix": ("video", proxy._VIDEO_GRAPH_CAPABILITY),
    "video.edit": ("video", proxy._VIDEO_GRAPH_CAPABILITY),
}


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def fake(intent, **kwargs):
        calls.append((intent, kwargs))
        return "admitted"

    monkeypatch.setattr(proxy, "_dispatch_admission_response", fake)
    return calls


def _expect(intent, resource_class, family):
    return intent, {
        "auth_context": "ctx", "service_id": proxy._graph_service_id(family),
        "service_available": proxy._graph_service(family).enabled,
        "resource_class": resource_class, "queue_key": proxy._MEDIA_JOB_QUEUE_KEY,
    }


@pytest.mark.parametrize("intent", sorted(SHIPPED))
def test_shipped_rows_admit_as_the_handlers_typed_them(seen, intent):
    resource_class, family = SHIPPED[intent]
    assert proxy._graph_media_admission(intent, auth_context="ctx") == "admitted"
    assert seen == [_expect(intent, resource_class, family)]


def test_an_unfamiliar_intent_follows_its_row_modality(seen, monkeypatch):
    monkeypatch.setitem(media_job_intents.MODALITY_BY_INTENT, "still.revise", "video")
    proxy._graph_media_admission("still.revise", auth_context="ctx")
    assert seen == [_expect("still.revise", "video", proxy._VIDEO_GRAPH_CAPABILITY)]


def test_an_intent_without_a_row_fails_naming_the_intent_and_the_data_file(seen):
    with pytest.raises(ValueError, match=r"'still\.revise'.*media_job_intents\.yml"):
        proxy._graph_media_admission("still.revise", auth_context="ctx")
    assert seen == []
