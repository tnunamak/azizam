"""Offline checks of the Status probe's opt-in whole-reply health rule."""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from status_endpoint import _probe


class _Client:
    """The one call the probe makes (an awaited get), answered by a function; no HTTP stack is faked."""

    def __init__(self, respond):
        self._respond = respond

    async def get(self, url, **_kwargs):
        return self._respond(httpx.Request("GET", url))


def _litellm_record():
    path = Path(__file__).resolve().parents[1] / "registry" / "services.yml"
    return next(r for r in yaml.safe_load(path.read_text(encoding="utf-8"))["records"] if r["id"] == "litellm")


@pytest.mark.parametrize(
    ("status_code", "payload", "config", "expected"),
    [
        (200, "I'm alive!", None, "up"),
        (503, "I'm alive!", None, "degraded"),
        (200, "I'm alive!", {}, "unknown"),
        # Guess: the neighbouring string row is "up". The shipped rule
        # (health_success_path ".") matches any non-empty string, but the
        # probe itself is outside this brief's scope, so check it on the run.
        (200, "down", None, "up"),
        # HTTP status wins over the missing rule: _probe returns on the 503
        # before it ever parses the body or consults health_success_path.
        (503, "I'm alive!", {}, "degraded"),
        # Non-2xx is not one bucket: 500 falls through to "down", while 404
        # has its own "unknown" branch (shared with 405).
        (500, "I'm alive!", None, "down"),
        (404, "I'm alive!", None, "unknown"),
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

        return await _probe(_Client(respond), "fixture", "Fixture", url, "required", None, service=record)

    row = asyncio.run(run())
    assert row["status"] == row["live_status"] == expected
