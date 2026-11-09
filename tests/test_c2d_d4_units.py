"""Small units of the mesh train (builder D4): the adapter registry, the retry hint, the Prefer token and the form fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import job_retry_hint, multipart_adapter, row_routes, workflow_adapters


class _Runner:
    def __init__(self, key: str, adapter: str) -> None:
        self.binding_key = key
        self.adapter_id = adapter


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(workflow_adapters._RUNNERS)
    workflow_adapters.clear()
    yield
    workflow_adapters.clear()
    workflow_adapters._RUNNERS.update(saved)


def test_a_registered_runner_is_found_by_its_binding_key_and_its_adapter_id() -> None:
    runner = _Runner("key-a", "adapter-a")
    workflow_adapters.register(runner)
    assert workflow_adapters.for_binding("key-a") is runner
    assert workflow_adapters.for_adapter("adapter-a") is runner
    assert workflow_adapters.for_binding("other") is None
    assert workflow_adapters.for_adapter("other") is None


def test_a_second_runner_for_the_same_key_replaces_the_first() -> None:
    workflow_adapters.register(_Runner("key-a", "first"))
    second = _Runner("key-a", "second")
    workflow_adapters.register(second)
    assert workflow_adapters.for_binding("key-a") is second
    assert workflow_adapters.for_adapter("first") is None


def _event(kind: str, step=None):
    return SimpleNamespace(type=kind, payload={"step": step} if step is not None else {})


def test_the_retry_hint_is_the_newest_positive_integer_delay() -> None:
    events = [_event("retry_after", 5), _event("progress", 99), _event("retry_after", 30)]
    assert job_retry_hint.read(events) == 30


@pytest.mark.parametrize("step", [0, -3, True, "30", None])
def test_the_retry_hint_ignores_a_delay_that_is_not_a_positive_integer(step) -> None:
    assert job_retry_hint.read([_event("retry_after", step)]) is None


def test_the_retry_hint_is_none_without_events() -> None:
    assert job_retry_hint.read([]) is None


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("respond-async", True), ("RESPOND-ASYNC", True),
    ("wait=10, respond-async", True), ("respond-async; foo=bar", True), ("handling=lenient", False),
    ("respond-asynchronous", False),
])
def test_prefer_respond_async_is_read_as_an_rfc_7240_token(value, expected) -> None:
    assert row_routes.prefers_async(value) is expected


def test_form_fields_come_from_the_mapped_inputs_with_contract_defaults() -> None:
    inputs = {"seed": {"type": "integer", "default": 7}, "flag": {"type": "boolean", "default": False},
              "image": {"type": "asset"}, "note": {"type": "string"}}
    mapping = {"seed": "seed_field", "flag": "flag_field", "image": "image", "note": "note_field"}
    assert multipart_adapter.scalar_fields(inputs, mapping, {}) == {"seed_field": "7", "flag_field": "false"}
    assert multipart_adapter.scalar_fields(inputs, mapping, {"seed": 1, "flag": True, "note": "x", "image": b"skipped"}) == {
        "seed_field": "1", "flag_field": "true", "note_field": "x"}
