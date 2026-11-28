"""The media job submit body binds each intent to its own body shape once an engine declares an operation.

A shipped intent takes ``request``; a declared intent takes ``model`` and ``input``. The published schema must not
accept one family's intent with the other family's shape (review of train 52, member side, F4)."""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from gateway import media_job_intents
from gateway.workflow_openapi import JOBS_SUBMIT, augment_openapi

SHIPPED = media_job_intents.SUBMITTABLE_INTENTS[0]
DECLARED = "sample.inspect"


def _submit_schema(declared_intents=()):
    spec = augment_openapi({"paths": {JOBS_SUBMIT: {"post": {}}}}, [], declared_intents=declared_intents)
    return spec["paths"][JOBS_SUBMIT]["post"]["requestBody"]["content"]["application/json"]["schema"]


def _valid(schema, body) -> bool:
    return Draft202012Validator(schema).is_valid(body)


def test_each_intent_family_is_accepted_only_with_its_own_shape():
    schema = _submit_schema([DECLARED])
    assert _valid(schema, {"intent": DECLARED, "model": "sample-offering", "input": {}})
    assert _valid(schema, {"intent": SHIPPED, "request": {}})
    assert not _valid(schema, {"intent": DECLARED, "request": {}})
    assert not _valid(schema, {"intent": SHIPPED, "model": "sample-offering", "input": {}})
    assert not _valid(schema, {"intent": "never.declared", "model": "m", "input": {}})


def test_with_nothing_declared_the_body_is_the_shipped_shape_alone():
    schema = _submit_schema()
    assert schema == {
        "type": "object",
        "required": ["intent", "request"],
        "properties": copy.deepcopy(schema["properties"]),
    }
    assert schema["properties"]["intent"]["enum"] == list(media_job_intents.SUBMITTABLE_INTENTS)
    assert set(schema["properties"]) == {"intent", "request"}
