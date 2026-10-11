from __future__ import annotations

import re

import pytest

from gateway import public_failure
from gateway.public_failure import LAYERS, PublicFailure, describe

# Words a member never reads (docs/design/VOICE.md); they belong in details.
_BANNED = re.compile(r"\b(intent|asset|backend|payload|JSON|upstream|profile|alias|cursor|idempotency|"
                     r"exception|manifest|http)\b|[a-z]+_[a-z_]+|\(\w+Error\)", re.IGNORECASE)


@pytest.mark.parametrize("key", sorted(public_failure._COPY))
def test_every_failure_names_a_layer_and_ends_with_an_action(key: str) -> None:
    values = {"maximum": 3, "max_chars": 32000, "max_images": 10, "input": "image", "model": "pixel"}
    layer, message = describe(key, **values)
    assert layer in LAYERS
    assert not _BANNED.search(message), message
    assert "{" not in message
    sentences = [part for part in re.split(r"(?<=\.) ", message) if part]
    assert len(sentences) >= 2, "a fact, then one next action"


def test_unlisted_failure_is_reported_as_unknown_not_invented() -> None:
    assert describe("never_seen") == describe(public_failure.UNKNOWN)
    assert describe("never_seen")[0] == "unknown"


def test_failure_body_keeps_code_and_moves_diagnostics_to_details() -> None:
    failure = PublicFailure("unsupported_field", 400, field="steps")
    assert failure.status_code == 400
    assert failure.body() == {"error": {
        "code": "unsupported_field",
        "message": "This request has a setting the gateway doesn't accept. Refresh the page and try again.",
        "details": {"layer": "request", "field": "steps"},
    }}


def test_copy_key_separates_causes_that_share_a_code() -> None:
    failure = PublicFailure("source_not_ready", 409, copy="source_missing", job_status="failed")
    assert failure.code == "source_not_ready"
    assert failure.message.startswith("That task didn't produce a picture.")
