"""Rerank request and response shapes.

One route family serves the common rerank clients: Cohere (``/v1/rerank``,
``/v2/rerank``), Jina (``/v1/rerank``), Voyage (``top_k``, ``data``) and
Hugging Face TEI (``/rerank`` with ``texts``). Open WebUI, LlamaIndex,
langchain-cohere, Dify and Continue all send the Cohere/Jina body::

    {"model": "...", "query": "...", "documents": ["...", {"text": "..."}], "top_n": 3}

and read ``results[].index`` and ``results[].relevance_score``.

This module is pure: no FastAPI, no network, no config. The caller sends
``upstream_body`` to a rerank engine (any server that answers
``POST /v1/rerank`` with ``results[].index`` and ``relevance_score``) and passes
the answer to ``build_response``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_MODEL = "rerank"
DEFAULT_MAX_DOCUMENTS = 1000
DEFAULT_MAX_DOCUMENT_CHARS = 32000

# Options that change which documents or text are scored. The engine cannot honor them,
# so a request that sets one is refused instead of answered with a different result.
_UNSUPPORTED = ("max_tokens_per_doc", "max_chunks_per_doc", "rank_fields")


class RerankInputError(ValueError):
    """The request body is not a valid rerank request."""

    def __init__(self, message: str, *, code: str = "invalid_request", param: str | None = None):
        super().__init__(message)
        self.code = code
        self.param = param


@dataclass(frozen=True)
class RerankRequest:
    model: str
    query: str
    documents: tuple[str, ...]
    top_n: int | None
    return_documents: bool | None
    tei: bool  # the body used TEI's ``texts``: answer with TEI's bare list


def _text_of(item: Any, index: int, field: str) -> str:
    if isinstance(item, Mapping):
        item = item.get("text")
    if not isinstance(item, str) or not item.strip():
        raise RerankInputError(
            f"{field}[{index}] must be a non-empty string or an object with a non-empty text field.",
            param=f"{field}[{index}]",
        )
    return item


def parse_request(
    body: Any,
    *,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> RerankRequest:
    if not isinstance(body, Mapping):
        raise RerankInputError("Request body must be a JSON object.")
    for name in _UNSUPPORTED:
        if body.get(name) is not None:
            raise RerankInputError(
                f"{name} is not supported by this rerank engine.", code="unsupported_parameter", param=name
            )
    model = body.get("model")
    if model is None or model == "":
        model = DEFAULT_MODEL
    if not isinstance(model, str):
        raise RerankInputError("model must be a string.", param="model")

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RerankInputError("query must be a non-empty string.", param="query")

    tei = "documents" not in body and "texts" in body
    field = "texts" if tei else "documents"
    raw_documents = body.get(field)
    if not isinstance(raw_documents, list) or not raw_documents:
        raise RerankInputError(f"{field} must be a non-empty list.", param=field)
    if len(raw_documents) > max_documents:
        raise RerankInputError(
            f"{field} has {len(raw_documents)} items; the limit is {max_documents}.", param=field
        )
    documents = tuple(_text_of(item, index, field) for index, item in enumerate(raw_documents))
    for index, text in enumerate(documents):
        if len(text) > max_document_chars:
            raise RerankInputError(
                f"{field}[{index}] has {len(text)} characters; the limit is {max_document_chars}.",
                param=f"{field}[{index}]",
            )

    top_n = body.get("top_n", body.get("top_k"))
    if top_n is not None:
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            raise RerankInputError("top_n must be an integer of 1 or more.", param="top_n")

    return_documents = body.get("return_text") if tei else body.get("return_documents")
    if return_documents is not None and not isinstance(return_documents, bool):
        raise RerankInputError(
            "return_text must be a boolean." if tei else "return_documents must be a boolean.",
            param="return_text" if tei else "return_documents",
        )
    return RerankRequest(model, query, documents, top_n, return_documents, tei)


def upstream_body(req: RerankRequest, upstream_model: str | None = None) -> dict[str, Any]:
    """Body for the engine. All documents are scored; sorting and ``top_n`` happen here."""
    body: dict[str, Any] = {"query": req.query, "documents": list(req.documents)}
    if upstream_model:
        body["model"] = upstream_model
    return body


class RerankUpstreamError(ValueError):
    """The engine answered with something that is not a rerank result."""


def _scores(upstream: Any, count: int) -> list[float]:
    results = upstream.get("results") if isinstance(upstream, Mapping) else None
    if not isinstance(results, list):
        raise RerankUpstreamError("The rerank engine returned no results list.")
    scores: dict[int, float] = {}
    for item in results:
        if not isinstance(item, Mapping):
            raise RerankUpstreamError("The rerank engine returned a malformed result.")
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
            raise RerankUpstreamError("The rerank engine returned an out-of-range document index.")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankUpstreamError("The rerank engine returned a result without a score.")
        scores[index] = float(score)
    if len(scores) != count:
        raise RerankUpstreamError(f"The rerank engine scored {len(scores)} of {count} documents.")
    return [scores[i] for i in range(count)]


def build_response(
    req: RerankRequest,
    upstream: Any,
    *,
    model: str,
    default_return_documents: bool,
) -> Any:
    """Sorted results in the shape the request used.

    Cohere/Jina/Voyage body -> ``{"id", "model", "results", "data", "usage", "meta"}``
    (``results`` is Cohere and Jina, ``data`` is Voyage; both hold the same items).
    TEI body (``texts``) -> a bare list of ``{"index", "score", "text"?}``.
    """
    scores = _scores(upstream, len(req.documents))
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    if req.top_n is not None:
        order = order[: req.top_n]
    include = default_return_documents if req.return_documents is None else req.return_documents

    if req.tei:
        out: list[dict[str, Any]] = []
        for i in order:
            row: dict[str, Any] = {"index": i, "score": scores[i]}
            if req.return_documents:
                row["text"] = req.documents[i]
            out.append(row)
        return out

    results = []
    for i in order:
        row = {"index": i, "relevance_score": scores[i]}
        if include:
            row["document"] = {"text": req.documents[i]}
        results.append(row)
    usage = upstream.get("usage") if isinstance(upstream, Mapping) else None
    total_tokens = int(usage.get("total_tokens", 0)) if isinstance(usage, Mapping) else 0
    return {
        "id": str(uuid.uuid4()),
        "object": "list",
        "model": model,
        "results": results,
        "data": results,
        "usage": {"total_tokens": total_tokens},
        "meta": {"api_version": {"version": "2"}, "billed_units": {"search_units": 1}},
    }


def error_body(
    message: str, *, code: str, param: str | None = None, tei: bool = False, kind: str = "invalid_request_error"
) -> dict[str, Any]:
    """One error body that the common rerank clients can read.

    ``message`` is Cohere, ``detail`` is Jina and Voyage, ``error`` as a string with
    ``error_type`` is TEI, and ``error`` as an object is the gateway's OpenAI shape.
    """
    if tei:
        return {"error": message, "error_type": code}
    error: dict[str, Any] = {"message": message, "type": kind, "code": code}
    if param:
        error["param"] = param
    return {"message": message, "detail": message, "error": error}
