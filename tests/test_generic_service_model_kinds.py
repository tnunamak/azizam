"""A model saved through the generic service API is checked against its kind, as the engine page checks it.

Add and edit both reach ``service_registry.upsert_service_in_registry``, which runs
``refuse_unoffered_kinds``. A refusal names the same field and the same 400 the engine page gives.
"""

from __future__ import annotations

from gateway import admin_store
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SPEECH_ROW = {"public": "voice-1", "upstream": "voice-1", "kind": "speech"}
CHAT_ROW = {"public": "chat-1", "upstream": "chat-1", "kind": "chat"}


def _write_headers(client):
    # A fresh database has no registry row yet: its generation is 0.
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _add(client, **fields):
    body = {"id": "box", "name": "Box", "host": "10.0.0.9", "port": 8080, **fields}
    return client.post("/admin/api/operator/services", headers=_write_headers(client), json=body)


def _edit(client, **fields):
    return client.patch("/admin/api/operator/services/box", headers=_write_headers(client), json=fields)


def _stored(gateway):
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    return (record["value"]["services"] if record else {}).get("box")


def _stored_model_kind(gateway):
    stored = _stored(gateway)
    assert stored is not None
    return stored["config"]["models"][0]["kind"]


def test_adding_a_kind_the_record_does_not_offer_is_refused_at_its_row(gateway):
    owner, *_ = owner_and_admin(gateway)

    refused = _add(owner, capabilities=["chat"], models=[SPEECH_ROW])

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "models.0.kind")
    assert error["message"] == "This engine doesn’t offer Voice."
    assert _stored(gateway) is None


def test_adding_a_kind_the_record_offers_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)

    created = _add(owner, capabilities=["speech"], models=[SPEECH_ROW])

    assert created.status_code == 201, created.text
    assert _stored_model_kind(gateway) == "speech"


def test_adding_a_model_without_a_kind_is_saved_on_any_record(gateway):
    owner, *_ = owner_and_admin(gateway)

    created = _add(owner, capabilities=["chat"], models=[{"public": "plain-1", "upstream": "plain-1"}])

    assert created.status_code == 201, created.text


def test_adding_a_kind_outside_the_vocabulary_is_refused_at_its_row(gateway):
    owner, *_ = owner_and_admin(gateway)

    refused = _add(owner, capabilities=["chat"], models=[{"public": "odd-1", "upstream": "odd-1", "kind": "video"}])

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "models.0.kind")
    assert error["message"] == "The capability of “odd-1” is not one of the choices."


def test_editing_a_kind_the_record_does_not_offer_is_refused_and_nothing_changes(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    before = _stored(gateway)

    refused = _edit(owner, models=[SPEECH_ROW])

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "models.0.kind"
    assert _stored(gateway) == before


def test_editing_a_record_to_offer_the_kind_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201

    edited = _edit(owner, capabilities=["speech"], models=[SPEECH_ROW])

    assert edited.status_code == 200, edited.text
    assert _stored_model_kind(gateway) == "speech"
