"""Offline checks of the Status probe's opt-in whole-reply health rule."""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from status_endpoint import _probe


def _litellm_record():
    path = Path(__file__).resolve().parents[1] / "registry" / "services.yml"
    return next(r for r in yaml.safe_load(path.read_text(encoding="utf-8"))["records"] if r["id"] == "litellm")


@pytest.mark.parametrize(
    ("status_code", "payload", "config", "expected"),
    [
        (200, "I'm alive!", None, "up"),
        (503, "I'm alive!", None, "degraded"),
        (200, "I'm alive!", {}, "unknown"),
        (200, {"probe": {"ok": True}}, {"health_success_path": "probe.ok"}, "up"),
        (200, {"probe": {"ok": False}}, {"health_success_path": "probe.ok"}, "unknown"),
    ],
)
def test_status_health_uses_declared_body_rule_only_for_success(status_code, payload, config, expected):
    record = _litellm_record() if config is None else {"config": config}
    path = record["config"].get("health_endpoint", "/health")
    url = f"http://fixture.invalid{path}"

    async def run():
        def respond(request):
            assert str(request.url) == url
            return httpx.Response(status_code, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await _probe(client, "fixture", "Fixture", url, "required", None, service=record)

    row = asyncio.run(run())
    assert row["status"] == row["live_status"] == expected
