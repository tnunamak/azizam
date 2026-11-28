"""The image routes take their intents from the frontend rows, not from a table in proxy.py."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import proxy  # noqa: E402
from gateway import frontend_mount, operation_security  # noqa: E402
from tests.support.access import access_context  # noqa: E402


async def _endpoint(request):  # pragma: no cover - never called; the mount only needs a handler to hold
    return None


def _mounts(generate: str, edit: str) -> frontend_mount.FrontendMounts:
    operation = frontend_mount.Operation(
        _endpoint, frozenset({"POST"}), self_authenticates=True, auth=operation_security.APP_KEY)
    translators = frontend_mount.Translators({"identity": {"images.generate": operation, "images.edit": operation}})
    mounts = frontend_mount.FrontendMounts(FastAPI(), translators, authenticate=None)
    mounts.apply([{"id": "synthetic-images", "registered_routes": [
        {"methods": ["POST"], "translator": "identity", "intent": "images.generate",
         "canonical": "/v1/images/generations", "execution_intents": {"admission": generate}},
        {"methods": ["POST"], "translator": "identity", "intent": "images.edit",
         "canonical": "/v1/images/edits", "execution_intents": {"admission": edit}},
    ]}])
    return mounts


def test_receipt_records_the_intent_the_row_declares() -> None:
    with patch.object(proxy, "FRONTEND_MOUNTS", _mounts("still.synthesize", "still.revise")):
        generated = proxy.start_image_route_receipt("/v1/images/generations", access_context())
        edited = proxy.start_image_route_receipt("/v1/images/edits", access_context())
    assert (generated.intent, edited.intent) == ("still.synthesize", "still.revise")


def test_edit_workflow_lookup_asks_under_the_edit_row_intent() -> None:
    asked: list[str | None] = []

    def load(name, *, intent=None):
        asked.append(intent)
        return None

    with patch.object(proxy, "FRONTEND_MOUNTS", _mounts("still.synthesize", "still.revise")), \
            patch.object(proxy, "load_graph_workflow", load), \
            patch.object(proxy, "graph_workflow_refused", lambda name, *, intent=None: False), \
            patch.object(proxy, "_find_image_manifest", lambda intent, model: None):
        proxy._select_edit_workflow("plain-name", image_count=1, has_mask=False)
    assert asked and set(asked) == {"still.revise"}


def test_generate_refusal_uses_the_intent_it_is_given() -> None:
    seen: list[str] = []

    def owns(records, intent, model):
        seen.append(intent)
        return False

    with patch.object(proxy, "other_intent_owns_stem", owns):
        assert proxy._generate_refusal_for_other_intent_stem(None, "m", None, "still.synthesize") is None
    assert seen == ["still.synthesize"]


def test_handler_loads_its_workflow_under_the_intent_it_holds() -> None:
    asked: list[str | None] = []

    def load(name, *, intent=None):
        asked.append(intent)
        return None

    class Request:
        headers: dict = {}
        scope: dict = {}

        async def json(self):
            return {"model": "no-such-model", "prompt": "p"}

    intents = frontend_mount.ExecutionIntents(admission="still.synthesize")
    with patch.object(proxy, "load_graph_workflow", load), \
            patch.object(proxy, "_graph_media_admission", lambda *a, **k: None), \
            patch.object(proxy, "_try_protocol_request", None):
        try:
            asyncio.run(proxy.proxy_graph_image_request(Request(), access_context(), intents=intents))
        except Exception:  # noqa: BLE001 - only the lookup's intent matters; the request shim may stop earlier
            pass
    assert asked and set(asked) == {"still.synthesize"}


def test_unknown_route_raises_a_named_error() -> None:
    with patch.object(proxy, "FRONTEND_MOUNTS", _mounts("still.synthesize", "still.revise")):
        with pytest.raises(proxy.ImageRouteIntentError):
            proxy.start_image_route_receipt("/v1/images/not-a-route", access_context())
    with pytest.raises(proxy.ImageRouteIntentError):
        proxy.start_image_route_receipt("/v1/images/not-a-route", access_context())


@pytest.mark.parametrize("path, intent", [
    ("/v1/images/generations", "image.generate"),
    ("/v1/images/edits", "image.edit"),
    ("/v1/images/variations", "image.variation"),
])
def test_shipped_rows_give_the_three_image_intents(path: str, intent: str) -> None:
    assert proxy.start_image_route_receipt(path, access_context()).intent == intent
