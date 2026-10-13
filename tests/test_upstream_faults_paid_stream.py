"""The paid LiteLLM path against a stream that stays open after ``[DONE]``."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest

from gateway import provider_execution, receipt_store
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from gateway.policy import AppProfile, RemoteProviderPolicy
from gateway.providers import ProviderRef
from starlette.requests import Request

from tests.support.fake_upstream import KnownBug, expect_fixed, fake_upstreams, load_scenario  # noqa: F401

# Bounds a stream that never ends; a gateway that ends at [DONE] returns at once.
OPEN_STREAM_BOUND_SECONDS = 2
BODY = {"model": "openrouter/openai/gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}


def _request() -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(BODY).encode(), "more_body": False}
        await asyncio.Future()

    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive)


def test_paid_stream_ends_and_settles_at_done_while_the_upstream_stays_open(fake_upstreams, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-provider")
    receipts = []
    monkeypatch.setattr(receipt_store, "begin", lambda _receipt: None)
    monkeypatch.setattr(receipt_store, "finalize", lambda receipt: receipts.append(receipt) or True)
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/stream-open-after-done"), host="litellm.fk.test")
    ledger = tmp_path / "provider_spend.db"
    provider = ProviderRef(
        id="openrouter", base_url="https://openrouter.ai/api/v1", protocols=("openai",), api_key_env="OPENROUTER_API_KEY",
        cost_class="paid-remote",
        price={"input_usd_per_million_tokens": "1.00", "output_usd_per_million_tokens": "2.00"},
        config={"credential_origin": "https://openrouter.ai", "models": [{"id": "openai/gpt-4.1-mini"}]},
    )
    profile = AppProfile(id="paid-app", remote=RemoteProviderPolicy(
        providers=frozenset({"openrouter"}), max_usd_per_request=Decimal("1.00"),
        budget_usd_per_day=Decimal("5.00"), budget_usd_per_month=Decimal("20.00"),
    ))
    runtime = LiteLlmRuntimeConfig(enabled=True, base_url=fake.base_url, api_key="sk-litellm", service_id="litellm")

    # The stream assembly imports litellm on first use; keep that cold import out of the open-stream bound.
    from litellm.main import stream_chunk_builder  # noqa: F401

    async def run() -> tuple[bytes, bool]:
        response = await provider_execution.execute_paid_litellm(
            _request(), request_path="/v1/chat/completions", body=dict(BODY), provider=provider,
            profile=profile, runtime=runtime, ledger_path=ledger,
        )
        assert response.status_code == 200
        received = b""
        stream = response.body_iterator.__aiter__()
        try:
            async with asyncio.timeout(OPEN_STREAM_BOUND_SECONDS):
                async for chunk in stream:
                    received += chunk
            return received, True
        except TimeoutError:
            return received, False
        finally:
            await stream.aclose()

    received, ended = asyncio.run(run())

    assert received.endswith(b"data: [DONE]\n\n")  # the client got the whole answer either way
    [forwarded] = fake.calls("chat")
    assert forwarded.json["stream"] is True
    expect_fixed("FK-B8", ended, True)
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall() == [("settled", "0.000222000")]
    assert receipts[0].cost["reconciliation_status"] == "settled"
