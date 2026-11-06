"""A history record links to the receipt of the request it kept, on the OpenAI mount too (REQUEST_RETENTION.md 13.3).

The receipt names the canonical route (``/v1/chat/completions``); a request that came through the OpenAI mount is
``/openai/v1/chat/completions``. The same route, so the receipt links. Another route, or an inner hop, does not.
"""
from __future__ import annotations

from gateway import request_history_slot


def _observed(slot_path: str, receipt_path: str | None, parent: str | None = None) -> str | None:
    token = request_history_slot.open_slot(slot_path)
    try:
        request_history_slot.observe("rcpt_1", receipt_path, parent)
        return request_history_slot.current().receipt_id
    finally:
        request_history_slot.close_slot(token)


def test_a_receipt_on_the_v1_route_links_to_the_same_route_on_the_openai_mount():
    assert _observed("/openai/v1/chat/completions", "/v1/chat/completions") == "rcpt_1"


def test_a_receipt_on_the_same_path_links():
    assert _observed("/v1/embeddings", "/v1/embeddings") == "rcpt_1"


def test_a_receipt_on_another_route_does_not_link():
    assert _observed("/openai/v1/chat/completions", "/v1/embeddings") is None


def test_an_inner_hop_with_a_parent_does_not_link():
    assert _observed("/openai/v1/chat/completions", "/v1/chat/completions", parent="rcpt_outer") is None
