"""The mesh wire is byte-identical to what it was before the mesh validator was deleted (CODE_TO_DATA_S3, S3.9).

``tests/fixtures/mesh_wire_baseline.json`` was recorded from the route that read the old validator's manifest, with the corpus
of ``tests/mesh_wire_corpus.py``. Here the same corpus runs against the route that reads the registry's record through the
mesh frontend profile (``proxy.current_mesh_workflow``): every status, header, response body (the GLB included) and every
byte the engine received must match. The multipart boundary the sender picks at random is replaced by a fixed word.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tests import mesh_wire_corpus as corpus
from tests.test_mesh_route import GLB, Harness

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "mesh_wire_baseline.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recorded() -> dict:
    return corpus.run(Harness)


def test_the_baseline_and_the_corpus_name_the_same_requests(recorded: dict) -> None:
    assert set(BASELINE) == set(recorded)
    assert len(BASELINE) >= 37


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_the_answer_and_the_engine_request_are_byte_identical(recorded: dict, name: str) -> None:
    assert recorded[name] == BASELINE[name]


def test_a_success_returns_the_raw_glb_and_sends_the_engine_one_form() -> None:
    answer = BASELINE["png-seed"]
    assert base64.b64decode(answer["body"]) == GLB
    assert answer["content_type"] == "model/gltf-binary" and answer["request_id"] == "req-test"
    [sent] = answer["engine_requests"]
    assert (sent["method"], sent["url"], sent["content_type"]) == ("POST", "http://127.0.0.1:8890/generate", "multipart/form-data; boundary=BOUNDARY")
    body = base64.b64decode(sent["body"])
    assert body.index(b'name="seed"') < body.index(b'name="resolution"') < body.index(b'name="image"')
    assert b'name="bg_removal"' not in body and b'name="extensions"' not in body
    assert b'filename="input.png"' in body and corpus.PNG in body


def test_a_misleading_name_and_type_never_reach_the_engine() -> None:
    body = base64.b64decode(BASELINE["jpeg-claimed-png-nothing-else"]["engine_requests"][0]["body"])
    assert b'filename="input.jpg"' in body and b"Content-Type: image/jpeg" in body
    assert b"Holiday" not in body and b'name="seed"' not in body and b'name="resolution"' in body


def test_an_error_has_one_exact_body() -> None:
    answer = BASELINE["seed-negative"]
    assert base64.b64decode(answer["body"]) == (
        b'{"error":{"message":"seed is outside its range","type":"invalid_request_error","code":"invalid_input","param":"seed"}}')
    assert answer["status"] == 400 and answer["request_id"] == "req-test" and answer["engine_requests"] == []
