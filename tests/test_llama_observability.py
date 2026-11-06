from __future__ import annotations

import asyncio

import status_endpoint


class _Response:
    status_code = 200

    def json(self):
        return {"model": "test"}


class _Client:
    def __init__(self, calls: list[str], **_kwargs):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str):
        self._calls.append(url)
        return _Response()


def test_llama_status_never_probes_task_queue(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        status_endpoint.httpx,
        "AsyncClient",
        lambda **kwargs: _Client(calls, **kwargs),
    )

    props, slots = asyncio.run(status_endpoint._llama_props_and_slots())

    assert props == {"model": "test"}
    assert slots is None
    assert calls == ["http://127.0.0.1:5051/props"]
    assert all(not url.endswith("/slots") for url in calls)
