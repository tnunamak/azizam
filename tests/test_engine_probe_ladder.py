"""Add engine: the probe ladder against fake servers of families A, B and C.

Each fake mirrors a row group of add-engine-probe-table.md (Tim's live engines,
measured 2026-10-14). The rules under test are the spec's section 5a:
a validating 4xx is "possible", never "likely"; 501 or "not supported" is
against; a 404 like the made-up path is "not found"; a 5xx is no evidence;
a capabilities array of only "completion" is not chat evidence; 401 beside a
404 baseline means "needs the key"; a 405-everywhere server needs the baseline.
"""

from __future__ import annotations

import asyncio
import json


from gateway import engine_check
from gateway.engine_check import KeyInput, make_http_fetch, run_check, run_real_test
from gateway.engine_definitions import load_engine_catalog
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture

TOKEN = "t0k3n"


async def _no_dns(host: str) -> list[str]:
    raise OSError("fake hosts do not resolve; the fake transport answers them")


FETCH = make_http_fetch(resolve=_no_dns)


def _check(definition_id: str, address: str, *, key: str | None = None, **values):
    catalog = load_engine_catalog()
    definition = catalog.definition(definition_id)
    return asyncio.run(run_check(catalog, definition, {"address": address, **values},
                                 key=KeyInput(key, "pasted") if key else KeyInput(), fetch=FETCH, baseline_token=TOKEN))


def _levels(result) -> dict[str, str]:
    return {row["id"]: row["level"] for row in result["capabilities"]}


def _serve(fakes, name, **values):
    return fakes.asgi(load_scenario(f"engine-probe/{name}", token=TOKEN, **values), host=f"{name}.fk.test")


def _only_get_and_empty_post(fake) -> None:
    for request in fake.requests:
        assert request.method in {"GET", "POST"}, request.summary()
        if request.method == "POST":
            assert request.body == b"{}", f"a check sent a real body: {request.summary()}"


def test_family_b_embedding_server_does_not_get_chat_suggested_as_likely(fake_upstreams):
    fake = _serve(fake_upstreams, "family-b-embedding-server")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    levels = _levels(result)
    # The 400 "'messages' is required" is a validating 4xx: possible, not likely (5a).
    assert levels["chat"] == "possible"
    assert levels["embeddings"] == "possible"
    assert set(levels) == {"chat", "embeddings"}, "v1 probes only the work an added engine can serve"
    chat = next(row for row in result["capabilities"] if row["id"] == "chat")
    assert chat["ticked"] is False, "a Possible capability stays unticked until a real test passes"
    assert "'messages' is required" in chat["evidence"]
    assert "not that a model can do this" in chat["why"]
    assert result["outcome"] == "ok" and result["findings"] == []
    assert result["models"] == [{"id": "nomic-embed-text-v1.5.f16.gguf", "public_name": "nomic-embed-text-v1.5",
                                 "context_window": 2048, "use": "Not known yet"}]
    _only_get_and_empty_post(fake)
    baseline = [r for r in fake.requests if "zz-not-a-route" in r.path]
    assert {r.method for r in baseline} == {"GET", "POST"}, "the random-path baseline is read for both methods"


def test_a_capabilities_array_of_only_completion_is_not_chat_evidence(fake_upstreams):
    fake = _serve(fake_upstreams, "family-b-embedding-server")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    assert all(row["level"] != "likely" for row in result["capabilities"])


def test_family_b_chat_server_refusing_embeddings_with_501_is_against(fake_upstreams):
    fake = _serve(fake_upstreams, "family-b-chat-server")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    levels = _levels(result)
    assert levels["chat"] == "possible"
    assert levels["embeddings"] == "against"
    model = result["models"][0]
    assert model["context_window"] == 202752
    assert model["public_name"] == "qwen3.8-27b"


def test_family_a_route_listed_by_the_api_description_and_validated_is_likely(fake_upstreams):
    fake = _serve(fake_upstreams, "family-a-routes-when-served")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    levels = _levels(result)
    assert levels["chat"] == "likely"
    chat = next(row for row in result["capabilities"] if row["id"] == "chat")
    assert chat["ticked"] is True and "/v1/chat/completions" in chat["evidence"]
    assert levels["embeddings"] == "none"
    assert result["models"][0]["public_name"] == "qwen3-8b"
    assert result["models"][0]["context_window"] == 40960
    # The description's title is a framework name, so the name comes from the address.
    assert result["name_source"] == "address"


def test_family_c_a_speech_server_offers_nothing_v1_can_serve(fake_upstreams):
    """Its model is marked for speech to text. Version 1 routes only chat and embeddings to an added engine."""
    fake = _serve(fake_upstreams, "family-c-task-metadata")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    assert _levels(result) == {"chat": "none", "embeddings": "none"}
    assert not any(row["ticked"] for row in result["capabilities"])
    assert result["models"][0]["use"] == "Not known yet"


def test_a_task_field_is_evidence_for_the_work_it_names():
    catalog = load_engine_catalog()
    fetch = _table({("GET", "/v1/models"): (200, {"data": [{"id": "bge-m3", "task": "embedding"}]})})
    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.10:8000/v1"},
                                   key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    embeddings = next(row for row in result["capabilities"] if row["id"] == "embeddings")
    assert embeddings["level"] == "likely" and "bge-m3" in embeddings["evidence"]
    assert result["models"][0]["use"] == "Embeddings"


def test_a_5xx_on_a_route_is_no_evidence():
    catalog = load_engine_catalog()
    fetch = _table({("GET", "/v1/models"): (200, {"data": [{"id": "m"}]}),
                    ("POST", "/v1/chat/completions"): (500, {"error": "missing required json key"})})
    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.10:8000/v1"},
                                   key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    chat = next(row for row in result["capabilities"] if row["id"] == "chat")
    assert chat["level"] == "none" and "server error" in chat["evidence"]


def test_a_route_the_description_lists_but_that_answers_501_is_against(fake_upstreams):
    fake = _serve(fake_upstreams, "openapi-lists-a-disabled-route")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    levels = _levels(result)
    assert levels["chat"] == "against"
    # Nothing it can serve and no model list: an error, so it is not added turned on.
    assert result["outcome"] == "error"
    assert [f["code"] for f in result["findings"]] == ["path_not_found"]


def test_a_possible_route_without_a_model_list_is_a_warning(fake_upstreams):
    fake = _serve(fake_upstreams, "chat-route-without-model-list")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    assert _levels(result)["chat"] == "possible"
    assert result["outcome"] == "warning"
    assert [f["code"] for f in result["findings"]] == ["models_unavailable"]


def test_a_401_beside_a_404_baseline_asks_for_the_key_first(fake_upstreams):
    fake = _serve(fake_upstreams, "key-gated-routes")
    result = _check("openai-compatible", fake.base_url)
    assert result["outcome"] == "error" and result["auth"] == "needed"
    finding = result["findings"][0]
    assert finding["code"] == "needkey" and finding["field"] == "key"
    assert "a made-up path answered “404 Not Found”" in finding["why"]
    assert {row["level"] for row in result["capabilities"]} == {"needs_key"}
    assert "YOUR_KEY" not in finding["details"]["request"], "no key was sent, so the repeat request has none"
    assert not any("chat/completions" in r.path for r in fake.requests), "nothing else is probed before the key"


def test_a_key_that_still_gets_401_is_refused(fake_upstreams):
    fake = _serve(fake_upstreams, "key-gated-routes")
    result = _check("openai-compatible", fake.base_url, key="sk-wrong-key")
    assert result["auth"] == "rejected"
    finding = result["findings"][0]
    assert finding["code"] == "auth_rejected"
    assert 'Authorization: Bearer YOUR_KEY' in finding["details"]["request"]
    assert "sk-wrong-key" not in json.dumps(result), "the key is never echoed back"
    models = [r for r in fake.requests if r.path == "/models"]
    assert models and models[0].headers.get("authorization") == "Bearer sk-wrong-key"


def test_a_401_on_every_path_says_the_server_wants_a_key_first(fake_upstreams):
    fake = _serve(fake_upstreams, "locked-everywhere")
    result = _check("openai-compatible", f"{fake.base_url}/v1")
    finding = result["findings"][0]
    assert finding["code"] == "needkey"
    assert "even a made-up one" in finding["why"]


def test_an_administrator_reads_the_owner_note_for_a_key_gated_server(fake_upstreams):
    fake = _serve(fake_upstreams, "key-gated-routes")
    catalog = load_engine_catalog()
    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": fake.base_url},
                                   key=KeyInput(), fetch=FETCH, viewer_role="admin", baseline_token=TOKEN))
    finding = result["findings"][0]
    assert finding["fix"].startswith("This server needs a key, and only the owner can add one.")
    assert finding["summary"] == "This server needs a key. Only the owner can add one."
    assert fake.base_url in finding["owner_note"]


def test_a_405_everywhere_node_graph_server_shows_nothing_possible(fake_upstreams):
    fake = _serve(fake_upstreams, "node-graph-405-everywhere")
    result = _check("openai-compatible", fake.base_url)
    assert {row["level"] for row in result["capabilities"]} == {"none"}, "a 405 like the made-up path is not a route"
    assert [f["code"] for f in result["findings"]] == ["not_openai_shape"]


def test_an_own_model_list_address_that_ends_in_v1_offers_to_remove_it(fake_upstreams):
    catalog = load_engine_catalog()
    definition = catalog.definition("own-model-list")

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        return engine_check.Answer(method=method, url=url, status=404, body=b'{"detail":"Not Found"}')

    result = asyncio.run(run_check(catalog, definition, {"address": "http://192.0.2.10:11434/v1"}, key=KeyInput(),
                                   fetch=fetch, baseline_token=TOKEN))
    finding = result["findings"][0]
    assert finding["code"] == "native_v1"
    assert finding["action"] == {"id": "fixpath", "label": "Remove /v1 and check again", "address_strip": "/v1"}


def test_an_openai_address_without_v1_and_nothing_found_offers_to_add_it():
    catalog = load_engine_catalog()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        return engine_check.Answer(method=method, url=url, status=404, body=b'{"detail":"Not Found"}')

    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.10:8080"},
                                   key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    assert result["outcome"] == "error"
    finding = result["findings"][0]
    assert finding["code"] == "path_v1_missing"
    assert finding["action"]["address_append"] == "/v1"
    assert finding["details"]["request"] == "curl -i http://192.0.2.10:8080/models"


def test_a_check_and_a_real_test_against_a_chat_server(fake_upstreams):
    reply = {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "length"}]}
    fake = _serve(fake_upstreams, "chat-answers-real-test", chat_status=200, chat_reply=reply)
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-compatible")
    result = asyncio.run(run_real_test(catalog, definition, {"address": f"{fake.base_url}/v1"}, "chat", key=KeyInput(),
                                       fetch=FETCH, model="qwen3-32b-instruct-q4_k_m"))
    assert result["ok"] is True and result["reply"] == "“OK”"
    assert result["sent_to"] == f"POST {fake.base_url}/v1/chat/completions"
    sent = json.loads(next(r for r in fake.requests if r.path == "/v1/chat/completions").body)
    assert sent["max_tokens"] == 1 and sent["model"] == "qwen3-32b-instruct-q4_k_m"


def test_a_failed_real_test_says_what_was_sent_and_what_to_do(fake_upstreams):
    fake = _serve(fake_upstreams, "chat-answers-real-test", chat_status=400, chat_reply={"error": {"message": "model not found"}})
    catalog = load_engine_catalog()
    result = asyncio.run(run_real_test(catalog, catalog.definition("openai-compatible"), {"address": f"{fake.base_url}/v1"},
                                       "chat", key=KeyInput(), fetch=FETCH, model="nope"))
    assert result["ok"] is False
    assert "model not found" in result["reply"]
    assert result["finding"]["what"].startswith("The server answered “400 Bad Request”")
    assert "OpenAI shape" in result["finding"]["why"]


def _table(rows):
    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        for (m, suffix), (status, body) in rows.items():
            if m == method and url.endswith(suffix):
                return engine_check.Answer(method=method, url=url, status=status, body=json.dumps(body).encode())
        return engine_check.Answer(method=method, url=url, status=404, body=b'{"detail":"Not Found"}')
    return fetch


def test_a_415_on_a_route_shows_it_exists_and_is_not_against():
    catalog = load_engine_catalog()
    fetch = _table({
        ("GET", "/v1/models"): (200, {"data": [{"id": "embed-model"}]}),
        ("POST", "/v1/embeddings"): (415, {"detail": "Unsupported Media Type"}),
    })
    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.10:8000/v1"},
                                   key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    assert _levels(result)["embeddings"] == "possible"


def test_a_failed_check_still_suggests_a_name_for_add_but_keep_off():
    catalog = load_engine_catalog()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        return engine_check.Answer(method=method, url=url, transport="refused", detail="refused")

    result = asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://garage-gpu.lan:8080/v1"},
                                   key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    assert result["outcome"] == "error" and result["suggested_name"] == "garage-gpu"
