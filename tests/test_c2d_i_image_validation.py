"""Image wire validation (S7 parity, builder I): what the old routes refused, the row shapes refuse in the same words.

Group 1: top-level fields outside the route's standard set, the extensions envelope, and the not-an-object texts."""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.responses import Response

from tests.test_frontend_shapes import (  # noqa: F401 - the autouse fixture
    ROWS, FakeSpool, multipart_request, json_request, no_deployment_default, run_request, snapshot)

NAMESPACE = "com.azizam.workflow"
UNKNOWN = "unknown top-level field; move gateway-native controls under extensions." + NAMESPACE
IMG = ("image", "a.png", "image/png", b"x")


def refusal(response) -> tuple[int, dict]:
    assert isinstance(response, Response), "the request was accepted"
    return response.status_code, json.loads(response.body)["error"]


def test_a_top_level_field_outside_the_routes_standard_set_is_refused() -> None:
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit")
    status, error = refusal(run_request("images-generations", json_request({"prompt": "p", "not_a_field": 1}), snap))
    assert (status, error) == (400, {"message": UNKNOWN, "type": "invalid_request_error", "param": "not_a_field", "code": "unknown_parameter"})
    # A workflow's own input is not a top-level field of the wire: it travels under the extensions envelope.
    assert refusal(run_request("images-generations", json_request({"prompt": "p", "seed": 3}), snap))[1]["param"] == "seed"
    # The job route takes the field at submit and leaves the refusal to the run, which fails the job with it.
    jobs = run_request("image-jobs", json_request({"prompt": "p", "seed": 3}), snap)
    assert not isinstance(jobs, Response) and "seed" not in jobs.inputs
    assert {k: jobs.run_refusal[k] for k in ("status", "code", "category")} == {"status": 400, "code": "unknown_parameter", "category": "invalid_input"}
    assert jobs.run_refusal["message"] == UNKNOWN
    # An edit refuses a file part the wire does not name; variations take no prompt at all.
    edit = run_request("images-edits", multipart_request({"prompt": "p"}, [("other", "a.png", "image/png", b"x")]), snap)
    assert refusal(edit)[1]["param"] == "other"
    variation = run_request("images-variations", multipart_request({"prompt": "p"}, [IMG]), snap)
    assert refusal(variation)[1] == {"message": UNKNOWN, "type": "invalid_request_error", "param": "prompt", "code": "unknown_parameter"}


def test_the_extensions_envelope_carries_workflow_inputs() -> None:
    snap = snapshot("flux2-klein")
    run = run_request("images-generations", json_request({"prompt": "p", "extensions": {NAMESPACE: {"seed": 3}}}), snap)
    assert not isinstance(run, Response) and run.inputs == {"prompt": "p", "seed": 3, "width": 1024, "height": 1024}
    form = run_request("images-edits", multipart_request({"prompt": "p", "extensions": json.dumps({NAMESPACE: {"steps": 8}})}, [IMG]),
                       snapshot("qwen-image-2.1-edit"))
    assert not isinstance(form, Response) and form.inputs["steps"] == 8


def test_extensions_that_are_not_a_json_object_string_are_refused() -> None:
    form = multipart_request({"prompt": "p", "extensions": "{nope"}, [IMG])
    status, error = refusal(run_request("images-edits", form, snapshot("qwen-image-2.1-edit")))
    assert (status, error) == (400, {"message": "extensions must be a JSON object string", "type": "invalid_request_error",
                                     "param": "extensions", "code": "invalid_json"})


@pytest.mark.parametrize("row,text", [("images-generations", "Request body must be a JSON object"),
                                      ("image-jobs", "JSON body must be an object")])
def test_a_body_that_is_not_an_object_is_refused_in_the_routes_words(row, text) -> None:
    status, error = refusal(run_request(row, json_request(b"[1]"), snapshot("flux2-klein")))
    assert (status, error) == (400, {"message": text, "type": "invalid_request_error"})
