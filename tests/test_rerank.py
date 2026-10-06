"""Rerank routes: Cohere, Jina, Voyage and TEI shapes over an on-demand engine."""

from __future__ import annotations

import json
import unittest

import httpx
from unittest.mock import patch

import proxy

from gateway import rerank as rerank_api
from gateway import stable_aliases
from tests.speech_tier_helpers import RouteCase

DOCS = ["paris is in france", "bananas are yellow", "the eiffel tower is in paris"]


def _engine_answer(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    # Score by shared words with the query so order depends on the input.
    words = set(body["query"].split())
    results = [
        {"index": i, "relevance_score": float(len(words & set(doc.split())))}
        for i, doc in enumerate(body["documents"])
    ]
    return httpx.Response(200, json={"model": "m", "object": "list", "usage": {"total_tokens": 7}, "results": results})


class ParseRequestTests(unittest.TestCase):
    def test_accepts_strings_and_text_objects(self) -> None:
        req = rerank_api.parse_request({"query": "q", "documents": ["a", {"text": "b"}], "top_n": 1})
        self.assertEqual(req.documents, ("a", "b"))
        self.assertEqual(req.top_n, 1)
        self.assertFalse(req.tei)
        self.assertEqual(req.model, "rerank")

    def test_tei_texts_and_voyage_top_k(self) -> None:
        req = rerank_api.parse_request({"query": "q", "texts": ["a"], "top_k": 2, "return_text": True})
        self.assertTrue(req.tei)
        self.assertEqual((req.top_n, req.return_documents), (2, True))

    def test_bad_input(self) -> None:
        cases = [
            [],
            {"documents": ["a"]},
            {"query": "", "documents": ["a"]},
            {"query": "q"},
            {"query": "q", "documents": []},
            {"query": "q", "documents": "a"},
            {"query": "q", "documents": [1]},
            {"query": "q", "documents": [{"body": "x"}]},
            {"query": "q", "documents": ["  "]},
            {"query": "q", "documents": ["a"], "top_n": 0},
            {"query": "q", "documents": ["a"], "top_n": "2"},
            {"query": "q", "documents": ["a"], "top_n": True},
            {"query": "q", "documents": ["a"], "return_documents": "yes"},
            {"query": "q", "documents": ["a"], "model": 3},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(rerank_api.RerankInputError):
                rerank_api.parse_request(body)

    def test_limits_and_unsupported_options(self) -> None:
        with self.assertRaises(rerank_api.RerankInputError):
            rerank_api.parse_request({"query": "q", "documents": ["a", "b"]}, max_documents=1)
        with self.assertRaises(rerank_api.RerankInputError):
            rerank_api.parse_request({"query": "q", "documents": ["abcd"]}, max_document_chars=3)
        for name in ("max_tokens_per_doc", "max_chunks_per_doc", "rank_fields"):
            with self.subTest(name=name), self.assertRaises(rerank_api.RerankInputError) as ctx:
                rerank_api.parse_request({"query": "q", "documents": ["a"], name: 5})
            self.assertEqual(ctx.exception.code, "unsupported_parameter")
        # null means unset
        rerank_api.parse_request({"query": "q", "documents": ["a"], "max_chunks_per_doc": None})


class BuildResponseTests(unittest.TestCase):
    def test_sorted_cut_and_ties_keep_input_order(self) -> None:
        req = rerank_api.parse_request({"query": "q", "documents": ["a", "b", "c"], "top_n": 2})
        upstream = {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.9}, {"index": 2, "relevance_score": 0.5}]}
        out = rerank_api.build_response(req, upstream, model="rerank", default_return_documents=False)
        self.assertEqual([r["index"] for r in out["results"]], [1, 0])
        self.assertEqual(out["data"], out["results"])
        self.assertNotIn("document", out["results"][0])

    def test_upstream_must_score_every_document(self) -> None:
        req = rerank_api.parse_request({"query": "q", "documents": ["a", "b"]})
        for upstream in ({}, {"results": [{"index": 0, "relevance_score": 1}]}, {"results": [{"index": 5, "relevance_score": 1}, {"index": 0, "relevance_score": 1}]}):
            with self.subTest(upstream=upstream), self.assertRaises(rerank_api.RerankUpstreamError):
                rerank_api.build_response(req, upstream, model="m", default_return_documents=False)


class RerankRouteTests(RouteCase):
    def _post(self, body, path="/v1/rerank"):
        return self.client.post(path, json=body)

    def test_cohere_jina_shape_on_every_path(self) -> None:
        seen = self.use_upstream(_engine_answer)
        for path in ("/v1/rerank", "/openai/v1/rerank", "/rerank", "/v2/rerank"):
            with self.subTest(path=path):
                resp = self._post({"model": "rerank-v3.5", "query": "paris france", "documents": DOCS, "top_n": 2}, path)
                self.assertEqual(resp.status_code, 200, resp.text)
                data = resp.json()
                self.assertEqual([r["index"] for r in data["results"]], [0, 2])
                self.assertEqual(data["results"], data["data"])
                self.assertTrue(data["id"])
                self.assertEqual(data["model"], "rerank-v3.5")
                self.assertEqual(data["usage"], {"total_tokens": 7})
                self.assertEqual(data["meta"]["billed_units"], {"search_units": 1})
                self.assertEqual(resp.headers["x-azizam-engine"], "qwen3-reranker-0.6b")
        self.assertEqual({r.url.port for r in seen}, {8018})
        self.assertEqual({r.url.path for r in seen}, {"/v1/rerank"})
        # The engine scores everything; the gateway cuts to top_n.
        self.assertNotIn("top_n", json.loads(seen[0].content))

    def test_document_echo_defaults_follow_the_vendor(self) -> None:
        self.use_upstream(_engine_answer)
        body = {"query": "paris", "documents": DOCS}
        self.assertEqual(self._post(body, "/v1/rerank").json()["results"][0]["document"], {"text": "paris is in france"})
        self.assertNotIn("document", self._post(body, "/v2/rerank").json()["results"][0])
        self.assertNotIn("document", self._post({**body, "return_documents": False}, "/v1/rerank").json()["results"][0])
        self.assertIn("document", self._post({**body, "return_documents": True}, "/v2/rerank").json()["results"][0])

    def test_text_object_documents(self) -> None:
        self.use_upstream(_engine_answer)
        resp = self._post({"query": "paris", "documents": [{"text": d} for d in DOCS]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["results"][0]["index"], 0)

    def test_tei_body_gets_tei_answer(self) -> None:
        self.use_upstream(_engine_answer)
        resp = self._post({"query": "paris", "texts": DOCS, "return_text": True}, "/rerank")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()
        self.assertIsInstance(rows, list)
        self.assertEqual(set(rows[0]), {"index", "score", "text"})
        self.assertEqual(rows[0]["index"], 0)

    def test_model_names(self) -> None:
        self.use_upstream(_engine_answer)
        body = {"query": "paris", "documents": DOCS}
        for model in ("rerank", "rerank-english-v3.0", "rerank-2", "jina-reranker-v2-base-multilingual", "bge-reranker-v2-m3", "qwen3-reranker-0.6b"):
            with self.subTest(model=model):
                self.assertEqual(self._post({**body, "model": model}).status_code, 200)
        self.assertEqual(self._post(body).status_code, 200)  # no model: the default
        resp = self._post({**body, "model": "gpt-4o"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "model_not_found")
        self.assertEqual(self._post({**body, "model": 5}).status_code, 400)

    def test_bad_input_is_400_in_every_vendor_shape(self) -> None:
        self.use_upstream(_engine_answer)
        resp = self._post({"query": "q", "documents": []})
        self.assertEqual(resp.status_code, 400)
        err = resp.json()
        self.assertEqual(err["message"], err["detail"])
        self.assertEqual(err["error"]["type"], "invalid_request_error")
        self.assertEqual(err["error"]["param"], "documents")
        tei = self._post({"query": "q", "texts": []}, "/rerank")
        self.assertEqual(tei.status_code, 400)
        self.assertEqual(set(tei.json()), {"error", "error_type"})
        self.assertEqual(self.client.post("/v1/rerank", content=b"{nope").status_code, 400)
        self.assertEqual(self.client.post("/v1/rerank", content=b"[]").status_code, 400)

    def test_engine_start_failure_is_503_and_not_a_hang(self) -> None:
        self.use_upstream(_engine_answer)
        self.health.return_value = False
        self.ensure.return_value = False
        resp = self._post({"query": "q", "documents": ["a"]})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers["retry-after"], "5")
        self.assertEqual(resp.json()["error"]["code"], "backend_unavailable")

    def test_engine_is_started_on_demand_only_when_down(self) -> None:
        self.use_upstream(_engine_answer)
        self.health.return_value = False
        self.assertEqual(self._post({"query": "q", "documents": ["a"]}).status_code, 200)
        self.ensure.assert_awaited_once()
        self.assertEqual(self.ensure.await_args.args[0], "rerank")
        self.ensure.reset_mock()
        self.health.return_value = True
        self.assertEqual(self._post({"query": "q", "documents": ["a"]}).status_code, 200)
        self.ensure.assert_not_awaited()

    def test_engine_400_is_a_vendor_400(self) -> None:
        self.use_upstream(lambda r: httpx.Response(400, json={"error": {"message": "input is too large"}}))
        resp = self._post({"query": "q", "documents": ["a"]})
        self.assertEqual((resp.status_code, resp.json()["message"]), (400, "input is too large"))

    def test_engine_5xx_is_502(self) -> None:
        self.use_upstream(lambda r: httpx.Response(500, text="boom"))
        self.assertEqual(self._post({"query": "q", "documents": ["a"]}).status_code, 502)

    def test_engine_answer_without_scores_is_502(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, json={"results": []}))
        self.assertEqual(self._post({"query": "q", "documents": ["a"]}).status_code, 502)

    def test_policy_applies_even_when_the_db_route_list_lacks_rerank(self) -> None:
        self.use_upstream(_engine_answer)
        with patch.dict(proxy._RUNTIME_ROUTE_POLICY, {}, clear=True):
            self.assertEqual(self._post({"query": "q", "documents": ["a"]}).status_code, 200)
        policy = proxy._enforce_runtime_route_policy.call_args.kwargs["policy"]
        self.assertEqual((policy["protocol"], policy["intent"]), ("openai.rerank", "text.embeddings"))

    def test_rerank_is_an_engine_alias_with_a_stable_name(self) -> None:
        res = stable_aliases.resolve_alias(self.cfg["model_aliases"] and {"rerank": self.cfg["model_aliases"]["rerank"]}, "rerank", "rerank")
        self.assertEqual((res.layer, res.engine, res.service), ("stable", "qwen3-reranker-0.6b", "rerank"))
        self.assertEqual(stable_aliases.validate_aliases(self.cfg["model_aliases"]), [])


if __name__ == "__main__":
    unittest.main()
