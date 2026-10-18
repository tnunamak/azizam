"""The review's unsettled candidate: an upstream error that repeats a stored credential, against a record that holds entries.

The management probes (check, real test, a saved edit) answer with text the engine wrote, and the review asked whether
an engine could make them repeat a stored URL credential or opaque header secret. The management plane never decrypts
a secret (decision D26), so it cannot scrub one by value; the property that closes the candidate is that a probe sends
the engine none of them. These tests hold a record with a short URL password, a query token and an opaque header
secret, run every probe, and require that (1) the engine received none of the stored values and (2) nothing a probe,
a saved edit or a view returns holds one. If a probe is ever changed to send a stored entry, (1) fails and the
route needs a value scrub with the resolved secret first.
"""

from __future__ import annotations

import json

import pytest

from gateway import engine_check, record_carriers, secret_refs, service_registry
from tests.support import canary
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.test_engine_admin_routes import TOKEN, _put_registry, _registry
from tests.test_engine_admin_routes import fake_engine_dns, fixed_baseline  # noqa: F401 - fixtures
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

M = {**canary.MARKERS, **canary.REVIEW_MARKERS}
USER, PASSWORD, QUERY = "demo-r3", "pw", "xy"
PROVENANCE = {"source": "hand-written", "reference": "azizam-review-w1-r3-projection-6c5235d-r0 unsettled candidates"}
ECHOES = (M["review-opaque"], f"{USER}:{PASSWORD}@", f"signature={QUERY}")


def _reply() -> str:
    return "rejected: no key was sent"


def _exchange(ident: str, method: str, path: str, status: int, body=None) -> dict:
    return {"id": ident, "match": {"method": method, "path": path},
            "response": {"status": status, "json": body if body is not None else {"detail": "Not Found"}}, "repeat": True, "required": False}


def _baselines() -> list[dict]:
    return [{"id": f"baseline-{m.lower()}", "match": {"method": m, "path": f"/v1/zz-not-a-route-{TOKEN}"},
             "response": {"status": 404, "json": {"detail": "Not Found"}}, "repeat": True, "required": False} for m in ("GET", "POST")]


def _engine(fakes, gateway, exchanges):
    fake = fakes.asgi(scenario_from_data({"protocol": "engine-probe", "name": "echo", "provenance": PROVENANCE,
                                          "exchanges": _baselines() + exchanges}), host="echo.fk.test")
    record = service_registry.service_from_backend_config("echo", {
        "name": "Echo engine", "host": "echo.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "definition": "openai-compatible", "definition_version": 1,
        "address": f"http://{USER}:{PASSWORD}@echo.fk.test/v1?signature={QUERY}",
        "capabilities": ["chat", "completions"], "headers": [{"name": "X-Vendor", "value": M["review-opaque"]}],
        "models": [{"model_group": "echo-chat", "upstream_model": "echo-chat"}]}, source="ui")
    record, moved = record_carriers.externalize_service(record, secret_refs.putter(gateway.path))
    assert moved, "the stored record holds entries: the positive control for the echo below"
    registry = _registry(gateway)
    registry.setdefault("services", {})["echo"] = record
    _put_registry(gateway, registry)
    return fake


def _hidden(text: str) -> None:
    for echo in ECHOES:
        assert echo not in text, f"{echo!r} came back"


def _nothing_stored_was_sent(fake) -> None:
    assert fake.requests, "the engine was probed"
    for request in fake.requests:
        wire = f"{request.method} {request.path} {dict(request.headers)}"
        for stored in ECHOES + (M["review-opaque"],):
            assert stored not in wire, f"a probe sent the stored {stored!r} to the engine"
        assert "x-vendor" not in {name.lower() for name in request.headers}


def _body(fake) -> dict:
    return {"definition": "openai-compatible", "service_id": "echo", "values": {"address": f"{fake.base_url}/v1"}}


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_a_probe_of_an_engine_with_stored_entries_sends_none_of_them_and_returns_none_of_them(gateway, fake_upstreams, who) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _engine(fake_upstreams, gateway, [_exchange("models", "GET", "/v1/models", 401, {"error": {"message": _reply()}}),
                                             _exchange("chat", "POST", "/v1/chat/completions", 400, {"error": {"message": _reply()}})])
    client = owner if who == "owner" else admin

    checked = client.post("/admin/api/engines/check", headers=csrf_for(client), json=_body(fake))
    tested = client.post("/admin/api/engines/test", headers=csrf_for(client), json={**_body(fake), "capability": "chat", "model": "echo-chat"})

    assert checked.status_code == 200 and tested.status_code == 200, (checked.text[:300], tested.text[:300])
    for answer in (checked, tested):
        _hidden(answer.text + str(dict(answer.headers)))
    _nothing_stored_was_sent(fake)


def test_a_saved_edit_and_the_engine_view_hold_no_stored_credential_and_the_edit_probe_sends_none(gateway, fake_upstreams) -> None:
    owner, _, _, admin = owner_and_admin(gateway)
    fake = _engine(fake_upstreams, gateway, [
        _exchange("models", "GET", "/v1/models", 200, {"data": [{"id": "echo-chat"}, {"id": "model-listed"}]}),
        _exchange("chat", "POST", "/v1/chat/completions", 400, {"error": {"message": _reply()}}),
        _exchange("embeddings", "POST", "/v1/embeddings", 404), _exchange("openapi", "GET", "/openapi.json", 404),
        _exchange("capabilities", "GET", "/v1/models/capabilities", 404)])
    etag = owner.get("/admin/api/operator/service-registry").headers["etag"]

    saved = owner.patch("/admin/api/operator/services/echo", headers={**csrf_for(owner), "If-Match": etag}, json={
        "definition": "openai-compatible", "recheck": True, "acknowledge_warnings": True, "values": {}})

    assert saved.status_code == 200, saved.text[:300]
    _hidden(saved.text)
    _nothing_stored_was_sent(fake)
    stored = json.dumps(_registry(gateway))
    assert M["review-opaque"] not in stored and f"{USER}:{PASSWORD}@" not in stored
    for client in (owner, admin):
        view = client.get("/admin/api/engines/echo")
        assert view.status_code == 200
        _hidden(view.text)
        assert not canary.leaks_in(view.text)
