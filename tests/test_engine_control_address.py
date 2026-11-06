"""``connection.control_address``: a record's ``class: control`` operations go to a separate management address.

Inference, streams and documented HTTP observations stay on ``connection.address``. A credential goes only to the
origin it is approved for, so a control key bound to the control address never reaches the engine address.
"""
from __future__ import annotations

import asyncio

import pytest

from gateway import engine_egress, native_lane, service_registry
from gateway.engine_contract_view import address_for_class
from gateway.engine_credentials import CredentialOriginMismatch, credential_headers
from gateway.engine_protocol import description, dispatch
from gateway.engine_protocol.errors import ProtocolError

ENGINE = "http://127.0.0.1:7001"
CONTROL = "http://127.0.0.1:7002"
CONTROL_KEY = "control-fixture-key"


def run(coro):
    return asyncio.run(coro)


def protocol() -> description.ProtocolDescription:
    return description.parse_description({
        "id": "control-address-fixture", "protocol_version": 1, "executor": "http-v1",
        "auth": {"inference": {"kind": "bearer"}, "control": {"kind": "bearer"}},
        "operations": {
            "generate": {"class": "generate", "request": {"method": "POST", "path": "/v1/generate"},
                         "response": {"framing": "json"}},
            "engine.release": {"class": "control", "request": {"method": "POST", "path": "/release"},
                               "response": {"framing": "json", "select": {"outcome": "/outcome"}}},
        }}, source="control-address-fixture")


def record(*, control: str | None = CONTROL, control_origin: str | None = CONTROL, observations=None) -> dict:
    connection: dict = {"dispatch": "protocol", "protocol": "control-address-fixture", "transport": "http",
                        "address": ENGINE}
    if control is not None:
        connection["control_address"] = control
    if control_origin is not None:
        connection["credentials"] = {"control": {"secret_ref": "os.environ/CONTROL_ADDRESS_TEST_KEY",
                                                 "scheme": "Bearer", "origin": control_origin}}
    return {"id": "fixture", "connection": connection, "observations": observations or {},
            "offerings": [{"id": "fixture"}]}


def runtime(service: dict) -> dispatch.Runtime:
    return dispatch.Runtime(dispatch.Target("fixture", service, {"id": "fixture"}, protocol()))


def sent(rt: dispatch.Runtime, monkeypatch) -> list:
    seen: list = []

    async def invoke(connection, op, request):
        seen.append((op, connection))
        return None

    monkeypatch.setattr(rt.adapter, "invoke", invoke)
    return seen


@pytest.fixture(autouse=True)
def control_key(monkeypatch):
    monkeypatch.setenv("CONTROL_ADDRESS_TEST_KEY", CONTROL_KEY)


def test_only_control_operations_use_the_control_address():
    connection = record()["connection"]
    assert address_for_class(connection, "control") == CONTROL
    assert address_for_class(connection, "inference") == ENGINE
    assert address_for_class(connection, "inventory") == ENGINE
    assert address_for_class({"address": ENGINE}, "control") == ENGINE, "no control address: control stays on the engine"


def test_a_control_operation_is_sent_to_the_control_address_with_its_key(monkeypatch):
    rt = runtime(record())
    seen = sent(rt, monkeypatch)
    run(rt.invoke("engine.release", {}))
    run(rt.invoke("generate", {}))
    (_, control), (_, inference) = seen
    assert control.address == CONTROL and control.credentials["control"].secret == CONTROL_KEY
    assert inference.address == ENGINE and not inference.credentials


def test_a_control_key_bound_to_the_engine_address_is_not_sent_to_the_control_address(monkeypatch):
    rt = runtime(record(control_origin=ENGINE))
    seen = sent(rt, monkeypatch)
    with pytest.raises(ProtocolError) as refused:
        run(rt.invoke("engine.release", {}))
    assert refused.value.code == "engine_credential_origin_mismatch"
    assert seen == [], "nothing is sent when the key is approved for another origin"


def test_a_documented_observation_probes_the_engine_address_without_the_control_key():
    rt = runtime(record())
    observation = description.parse_description({
        "id": "observation", "protocol_version": 1, "executor": "http-v1",
        "auth": {"control": {"kind": "bearer"}},
        "operations": {"observe": {"class": "control", "request": {"method": "GET", "path": "/health"},
                                   "response": {"framing": "json", "select": {"value": "/ok"}}}}},
        source="observation")
    connection = rt.connection_for("observe", operation=observation.operations["observe"], probe=True,
                                   engine_address=True)
    assert connection.address == ENGINE and not connection.credentials


def test_credential_headers_check_the_origin_the_request_goes_to():
    service = record()
    assert credential_headers(service, "control", address=CONTROL) == {"Authorization": f"Bearer {CONTROL_KEY}"}
    with pytest.raises(CredentialOriginMismatch):
        credential_headers(service, "control", address=ENGINE)
    with pytest.raises(CredentialOriginMismatch):  # the default destination is still the engine address
        credential_headers(service, "control")


def test_the_egress_map_reaches_the_control_address():
    keys = engine_egress.destinations_of({"connection": record()["connection"]})
    assert engine_egress.destination_key("127.0.0.1", 7002) in keys
    assert engine_egress.destination_key("127.0.0.1", 7001) in keys


@pytest.mark.parametrize("bad", ["ftp://127.0.0.1:7002", "http://user:pw@127.0.0.1:7002", "http://127.0.0.1:7002/?x=1",
                                 "http://127.0.0.1:7002/#f", "127.0.0.1:7002", 7002])
def test_a_malformed_control_address_is_refused(bad):
    with pytest.raises(ValueError, match="control_address"):
        native_lane.validate_connection({"address": ENGINE, "control_address": bad})


def test_protocol_dispatch_refuses_a_control_address_that_is_not_local():
    service_registry.validate_protocol_dispatch_local_only(record())
    with pytest.raises(ValueError):
        service_registry.validate_protocol_dispatch_local_only(record(control="https://control.example.com"))


def test_moving_the_control_address_counts_as_moving_a_key():
    before = {"services": {"fixture": {"config": {"connection": record()["connection"]}}}}
    moved = {"services": {"fixture": {"config": {"connection": record(control="http://127.0.0.1:7003")["connection"]}}}}
    assert service_registry.credential_binding_changes(before, moved)
