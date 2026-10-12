from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import FileResponse

import proxy
from gateway.capabilities import Capability


def _request():
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models",
        "scheme": "http", "server": ("testserver", 80),
        "headers": [], "query_string": b"",
    })


def _transport(monkeypatch, *, body=b'{"data":[]}', status=200):
    events = []
    calls = []
    reads = []

    class Client:
        async def aclose(self):
            events.append("client.close")

    class Response:
        status_code = status

        async def aclose(self):
            events.append("response.close")

    client, response = Client(), Response()

    async def send(execution_request):
        calls.append(execution_request)
        return client, response

    async def read(got_client, got_response):
        assert got_client is client
        assert got_response is response
        reads.append(True)
        return body

    monkeypatch.setattr(proxy, "send_llm_execution_request", send)
    monkeypatch.setattr(proxy, "read_llm_response_body", read)
    monkeypatch.setattr(proxy, "litellm_headers", lambda *a, **kw: {})
    monkeypatch.setattr(proxy, "_litellm_gateway_metadata", lambda *a, **kw: {})
    runtime = SimpleNamespace(
        enabled=True, base_url="http://fixture.invalid", service_id="fixture",
        api_key="",
    )

    async def invoke():
        return await proxy._litellm_models_payload(
            _request(), object(), runtime_config=runtime
        )

    return SimpleNamespace(
        invoke=invoke, events=events, calls=calls, reads=reads,
        client=client, response=response,
    )


# Group A: malformed/status/read failures remain a local discovery fallback.
@pytest.mark.parametrize(
    "status,body,valid",
    [
        (200, b'{"data":[{"id":"fixture-model"}]}', True),
        (200, b"", True),  # Existing contract: empty body is {} plus marker.
        (200, b"[]", False),
        (200, b"null", False),
        (200, b"{broken", False),
        (200, b"\xff", False),
        (200, None, False),
        (401, b'{"data":[]}', False),
        (429, b'{"data":[]}', False),
        (503, b'{"data":[]}', False),
    ],
)
def test_litellm_catalog_payload_matrix(monkeypatch, status, body, valid):
    fake = _transport(monkeypatch, body=body, status=status)
    result = asyncio.run(fake.invoke())
    if valid:
        assert result["x_azizam_llm_execution"] == "litellm"
        if body:
            assert result["data"] == [{"id": "fixture-model"}]
    else:
        assert result is None
    assert len(fake.calls) == 1
    assert fake.calls[0].method == "GET"
    assert fake.calls[0].body == b""
    assert fake.calls[0].read_timeout == 10.0
    assert fake.events == ["response.close", "client.close"]
    assert len(fake.reads) == (1 if status == 200 else 0)


def test_litellm_catalog_read_error_closes_both(monkeypatch):
    fake = _transport(monkeypatch)

    async def broken_read(*args):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(proxy, "read_llm_response_body", broken_read)
    assert asyncio.run(fake.invoke()) is None
    assert fake.events == ["response.close", "client.close"]


# Group B: actual task cancellation after resource acquisition is propagated.
def test_litellm_catalog_cancellation_closes_both(monkeypatch):
    fake = _transport(monkeypatch)

    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()

        async def pending_read(*args):
            entered.set()
            await never.wait()

        monkeypatch.setattr(proxy, "read_llm_response_body", pending_read)
        task = asyncio.create_task(fake.invoke())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())
    assert fake.events == ["response.close", "client.close"]


# Group C: two callers overlap at the loaded-model await, without fixed sleeps.
def test_overlapping_model_filters_keep_caller_metadata_separate(monkeypatch):
    source = [
        {"id": "a"}, {"id": "b"}, {"id": "/private/path.gguf"},
        {"id": "category-denied"},
    ]
    before = copy.deepcopy(source)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {})
    monkeypatch.setattr(
        proxy.provider_execution, "litellm_provider_model_lookup", lambda reg: {}
    )
    monkeypatch.setattr(proxy.providers, "provider_refs", lambda reg: {})
    monkeypatch.setattr(proxy, "registered_model_capabilities", lambda reg: [])
    monkeypatch.setattr(
        proxy, "_profile_for_auth_context",
        lambda auth: SimpleNamespace(remote=SimpleNamespace(providers=set())),
    )
    monkeypatch.setattr(proxy, "_profile_model_allowlist", lambda auth: auth.allowed)
    monkeypatch.setattr(proxy, "_profile_target_visibility", lambda auth: "available")
    monkeypatch.setattr(proxy, "_profile_target_activation", lambda auth: "existing_only")
    monkeypatch.setattr(
        proxy, "_category_entry_visible",
        lambda auth, entry: entry.get("id") != "category-denied",
    )
    monkeypatch.setattr(proxy, "_comfy_target_available", lambda: False)
    monkeypatch.setattr(proxy, "EMBEDDINGS_ENABLED", False)

    async def scenario():
        both_entered = asyncio.Event()
        seen_metadata_sets = []

        async def loaded(*, metadata_ids=None):
            assert metadata_ids is not None
            seen_metadata_sets.append(metadata_ids)
            metadata_ids.add("synthetic-observation")
            if len(seen_metadata_sets) == 2:
                both_entered.set()
            await both_entered.wait()
            return {"a", "b", "/private/path.gguf", "category-denied"}

        monkeypatch.setattr(proxy, "_current_loaded_llm_model_ids", loaded)
        a_observed, b_observed = set(), set()
        a = SimpleNamespace(allowed={"a", "category-denied"})
        b = SimpleNamespace(allowed={"b", "category-denied"})
        results = await asyncio.wait_for(asyncio.gather(
            proxy._filter_model_list_for_profile(source, a, metadata_ids=a_observed),
            proxy._filter_model_list_for_profile(source, b, metadata_ids=b_observed),
        ), timeout=2)
        assert len(seen_metadata_sets) == 2
        assert seen_metadata_sets[0] is not seen_metadata_sets[1]
        assert a_observed == b_observed == {"synthetic-observation"}
        for (entries, metadata), expected in zip(results, ("a", "b")):
            assert entries == [{"id": expected}]
            assert metadata["azizam_current_model_ids"] == [expected]
            assert metadata["azizam_profile_model_allowlist"] == [expected]
            assert metadata["azizam_target_visibility"] == "available"
            assert metadata["azizam_target_activation"] == "existing_only"

    asyncio.run(scenario())
    assert source == before


# Group D: synthetic files only. Direct calls isolate the handler's containment
# contract from URL/router normalization, which merits a separate HTTP test.
@pytest.mark.parametrize("skill_id", ["", ".hidden", "../outside", "nested/name"])
def test_skill_handler_rejects_nonflat_ids(monkeypatch, tmp_path, skill_id):
    root = tmp_path / "skills"
    root.mkdir()
    # A real SKILL.md exists at every path the id would resolve to, so only
    # the id guard (not a missing file) can produce the 404.
    for target in (root / skill_id / "SKILL.md", tmp_path / "outside" / "SKILL.md"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Synthetic", encoding="utf-8")
    monkeypatch.setattr(proxy, "_SKILL_ROOT", root)
    response = asyncio.run(proxy.well_known_skill_file(skill_id))
    assert response.status_code == 404


def test_skill_handler_serves_inside_root_and_rejects_missing(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    file = root / "allowed" / "SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("Synthetic public skill", encoding="utf-8")
    monkeypatch.setattr(proxy, "_SKILL_ROOT", root)
    response = asyncio.run(proxy.well_known_skill_file("allowed"))
    assert isinstance(response, FileResponse)
    assert response.status_code == 200
    assert response.path == file.resolve()
    assert response.media_type.startswith("text/markdown")
    missing = asyncio.run(proxy.well_known_skill_file("missing"))
    assert missing.status_code == 404


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_skill_handler_rejects_symlink_escape(monkeypatch, tmp_path, link_kind):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "SKILL.md"
    outside_file.write_text("SYNTHETIC_OUTSIDE_MARKER", encoding="utf-8")
    if link_kind == "directory":
        (root / "escape").symlink_to(outside, target_is_directory=True)
    else:
        (root / "escape").mkdir()
        (root / "escape" / "SKILL.md").symlink_to(outside_file)
    monkeypatch.setattr(proxy, "_SKILL_ROOT", root)
    response = asyncio.run(proxy.well_known_skill_file("escape"))
    assert response.status_code == 404
    assert not isinstance(response, FileResponse)
    assert b"SYNTHETIC_OUTSIDE_MARKER" not in response.body


# Group E: availability presentation fallback must preserve policy and allow
# task cancellation to escape rather than returning a success catalog.
@pytest.mark.parametrize("failure_stage", ["evidence", "projection"])
def test_capability_availability_failure_preserves_policy(monkeypatch, failure_stage):
    caps = [
        Capability(intent="chat.completions", backend="fixture", model="allowed"),
        Capability(
            intent="chat.completions", backend="private", model="denied",
            policy_available=False, policy_reasons=("synthetic denial",),
        ),
    ]
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda req: object())
    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", lambda auth: caps)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {})
    monkeypatch.setattr(proxy, "_workflow_availability_services", lambda reg: reg)
    monkeypatch.setattr(proxy.GATEWAY_WORKFLOW_REGISTRY, "records", lambda: [])

    async def evidence(*args):
        if failure_stage == "evidence":
            raise RuntimeError("synthetic evidence failure")
        return {}

    def project(*args, **kwargs):
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(proxy, "_capabilities_availability_evidence", evidence)
    monkeypatch.setattr(proxy, "project_capabilities", project)
    result = asyncio.run(proxy._gateway_capabilities_provider(_request()))
    assert result is caps
    assert result[1].policy_available is False
    assert result[1].policy_reasons == ("synthetic denial",)


def test_capability_availability_does_not_swallow_cancellation(monkeypatch):
    monkeypatch.setattr(proxy, "_auth_context_from_scope", lambda req: object())
    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", lambda auth: [])
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {})
    monkeypatch.setattr(proxy, "_workflow_availability_services", lambda reg: reg)

    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(proxy, "_capabilities_availability_evidence", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(proxy._gateway_capabilities_provider(_request()))
