"""The native lane against contract section 6, on the live gateway: query strings, reply headers, one
credential per class bound to its origin, the engine host checked for SSRF, ambiguous paths, and the
policy read from the engine's own record."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

import proxy
from gateway import engine_check, receipt_store, service_registry
from tests.support.access import plan_for
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_native_passthrough import (  # noqa: F401 - fixture and helpers
    ALLOW_CHAT, BODY, NATIVE_PATH, _engine, _new_receipt, _post, _receipts_for, _scenario, native_gateway,
)
from tests.test_runtime_route_policy import AUTH_HEADERS, authenticated_profile

pytestmark = pytest.mark.usefixtures("native_gateway")

WIDE = {"engines": "all", "operations": ["*"]}


def _denied(response, code: str, status: int = 403) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def _unload_scenario():
    return _scenario({
        "id": "unload", "match": {"method": "DELETE", "path": "/v1/models/demo-chat"},
        "response": {"status": 200, "json": {"unloaded": True}},
    })


def _models_scenario(**response):
    return _scenario({
        "id": "models", "match": {"method": "GET", "path": "/v1/models"},
        "response": {"status": 200, "json": {"object": "list", "data": []}, **response},
    })


def _get(path: str, **kwargs):
    return TestClient(proxy.app).get(path, headers=AUTH_HEADERS, **kwargs)


# --- query strings -----------------------------------------------------------------------------

def test_a_query_key_the_template_declares_reaches_the_engine_and_any_other_is_refused(fake_upstreams):
    receipt_store.init_db()
    engine = fake_upstreams.loopback(_models_scenario())
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate", "query": ["limit"]}]}
    path = "/native/garage/v1/models"

    with authenticated_profile(plan_for("native-query", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        allowed = _get(path, params={"limit": "5"})
        before = _receipts_for(path)
        refused = _get(path, params={"limit": "5", "debug": "1"})

    assert allowed.status_code == 200, allowed.text
    [sent] = engine.calls("models")
    assert dict(sent.query) == {"limit": "5"}
    _denied(refused, "native_query_not_allowed")
    assert len(engine.requests) == 1
    assert _new_receipt(before, path)["status"] == "denied"


def test_a_template_with_no_query_keys_refuses_any_query_string(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate"}]}

    with authenticated_profile(plan_for("native-no-query", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        response = _get("/native/garage/v1/models", params={"anything": "1"})

    _denied(response, "native_query_not_allowed")
    assert engine.requests == []


def test_a_generate_call_cannot_pass_a_file_path_in_a_declared_query_value(fake_upstreams):
    engine = fake_upstreams.loopback(_models_scenario())
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate", "query": ["model"]}]}

    with authenticated_profile(plan_for("native-query-path", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        encoded = _get("/native/garage/v1/models?model=%2Fetc%2Fpasswd")
        dotted = _get("/native/garage/v1/models", params={"model": "../../keys"})
        ordinary = _get("/native/garage/v1/models", params={"model": "demo-chat"})

    _denied(encoded, "native_query_value_refused")
    _denied(dotted, "native_query_value_refused")
    assert ordinary.status_code == 200
    assert len(engine.requests) == 1


def test_an_owner_control_call_may_pass_a_path_in_a_declared_query_value(fake_upstreams):
    engine = fake_upstreams.loopback(_unload_scenario())
    allow = {"allow": [{"method": "DELETE", "path": "/v1/models/{model}", "class": "control", "query": ["file"]}]}

    with authenticated_profile(plan_for("native-query-control", caps=(), **WIDE)), patch.object(
        proxy, "_native_caller_is_owner", return_value=True,
    ), patch.dict(proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)}):
        response = TestClient(proxy.app).delete("/native/garage/v1/models/demo-chat?file=/models/a.gguf",
                                                headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    assert dict(engine.calls("unload")[0].query) == {"file": "/models/a.gguf"}


# --- reply headers -----------------------------------------------------------------------------

def test_cookies_and_origin_headers_from_the_engine_are_not_relayed(fake_upstreams):
    engine = fake_upstreams.loopback(_models_scenario(headers={
        "set-cookie": "sid=engine", "strict-transport-security": "max-age=31536000",
        "access-control-allow-origin": "*", "access-control-allow-credentials": "true",
        "www-authenticate": "Basic realm=engine", "alt-svc": 'h3=":443"', "x-engine-note": "kept",
    }))
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate"}]}

    with authenticated_profile(plan_for("native-reply-headers", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        response = _get("/native/garage/v1/models")

    assert response.status_code == 200, response.text
    names = {name.lower() for name in response.headers}
    assert not names & {"set-cookie", "strict-transport-security", "access-control-allow-origin",
                        "access-control-allow-credentials", "www-authenticate", "alt-svc"}
    assert response.headers["x-engine-note"] == "kept"


# --- one credential per class, bound to its origin -----------------------------------------------

def _credentials(engine, **overrides) -> dict:
    origin = service_registry.origin_of(engine.base_url)
    return {"credentials": {
        "inference": {"secret_env": "NATIVE_INFER_KEY", "header": "x-api-key", "origin": origin},
        "control": {"secret_env": "NATIVE_CONTROL_KEY", "header": "Authorization", "scheme": "Bearer",
                    "origin": origin},
        **overrides,
    }}


CALLS = {"allow": [
    {"method": "POST", "path": "/v1/chat/completions", "class": "generate"},
    {"method": "DELETE", "path": "/v1/models/{model}", "class": "control"},
]}
UNLOAD = "/native/garage/v1/models/demo-chat"


def _two_call_engine(fake_upstreams):
    return fake_upstreams.loopback(_scenario(
        {"id": "chat", "match": {"method": "POST", "path": "/v1/chat/completions"},
         "response": {"status": 200, "json": {"choices": []}}},
        {"id": "unload", "match": {"method": "DELETE", "path": "/v1/models/demo-chat"},
         "response": {"status": 200, "json": {}}},
    ))


def _sent_headers(call) -> dict:
    return {name.lower(): value for name, value in call.headers.items()}


def test_each_class_goes_out_with_its_own_credential_and_never_the_others(fake_upstreams, monkeypatch):
    monkeypatch.setenv("NATIVE_INFER_KEY", "infer-secret")
    monkeypatch.setenv("NATIVE_CONTROL_KEY", "control-secret")
    engine = _two_call_engine(fake_upstreams)
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS, connection=_credentials(engine))
    client_headers = {**AUTH_HEADERS, "Cookie": "session=client", "X-Api-Key": "client-key"}

    with authenticated_profile(plan_for("native-class-credentials", caps=(), **WIDE)), patch.object(
        proxy, "_native_caller_is_owner", return_value=True,
    ), patch.dict(proxy.config["backends"], {"garage": record}):
        client = TestClient(proxy.app)
        assert client.post(NATIVE_PATH, json=BODY, headers=client_headers).status_code == 200
        assert client.delete(UNLOAD, headers=client_headers).status_code == 200

    chat, load = engine.calls("chat")[0], engine.calls("unload")[0]
    assert _sent_headers(chat)["x-api-key"] == "infer-secret"
    assert "authorization" not in _sent_headers(chat)
    assert _sent_headers(load)["authorization"] == "Bearer control-secret"
    assert _sent_headers(load).get("x-api-key") != "infer-secret"
    for sent in (chat, load):
        assert "client-key" not in _sent_headers(sent).values()
        assert "cookie" not in _sent_headers(sent)


def test_a_control_call_with_no_control_credential_sends_no_credential_at_all(fake_upstreams, monkeypatch):
    monkeypatch.setenv("NATIVE_INFER_KEY", "infer-secret")
    engine = fake_upstreams.loopback(_unload_scenario())
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS,
                     connection=_credentials(engine, control=None))

    with authenticated_profile(plan_for("native-no-control-cred", caps=(), **WIDE)), patch.object(
        proxy, "_native_caller_is_owner", return_value=True,
    ), patch.dict(proxy.config["backends"], {"garage": record}):
        response = TestClient(proxy.app).delete(UNLOAD, headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    sent = _sent_headers(engine.calls("unload")[0])
    assert "infer-secret" not in sent.values()
    assert not {"x-api-key", "authorization"} & set(sent)


def test_the_old_single_credential_of_the_service_record_is_not_a_native_credential(fake_upstreams, monkeypatch):
    monkeypatch.setenv("NATIVE_ENGINE_KEY", "engine-secret")
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="ok"))
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS,
                     auth_inject=[{"header": "x-api-key", "secret_env": "NATIVE_ENGINE_KEY"}],
                     api_key_env="NATIVE_ENGINE_KEY")

    with authenticated_profile(plan_for("native-no-fallback", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": record},
    ):
        assert TestClient(proxy.app).post(NATIVE_PATH, json=BODY, headers=AUTH_HEADERS).status_code == 200

    assert "engine-secret" not in _sent_headers(engine.calls("chat")[0]).values()


def test_a_credential_bound_to_another_origin_is_not_sent_and_the_call_is_refused(fake_upstreams, monkeypatch):
    receipt_store.init_db()
    monkeypatch.setenv("NATIVE_INFER_KEY", "infer-secret")
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    moved = {"inference": {"secret_env": "NATIVE_INFER_KEY", "header": "x-api-key", "origin": "http://elsewhere.test:9"}}
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS, connection={"credentials": moved})
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(plan_for("native-origin-moved", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": record},
    ):
        response = _post(NATIVE_PATH)

    _denied(response, "native_credential_origin_mismatch", status=502)
    assert engine.requests == []
    assert _new_receipt(before, NATIVE_PATH)["status"] == "failed"


def test_a_credential_that_names_no_origin_is_not_sent(fake_upstreams, monkeypatch):
    monkeypatch.setenv("NATIVE_INFER_KEY", "infer-secret")
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    unbound = {"inference": {"secret_env": "NATIVE_INFER_KEY", "header": "x-api-key"}}
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS, connection={"credentials": unbound})

    with authenticated_profile(plan_for("native-origin-unbound", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": record},
    ):
        response = _post(NATIVE_PATH)

    _denied(response, "native_credential_origin_mismatch", status=502)
    assert engine.requests == []


def test_a_credential_names_its_secret_by_stored_reference_too(fake_upstreams, monkeypatch):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="ok"))
    origin = service_registry.origin_of(engine.base_url)
    stored = {"inference": {"secret_ref": "sec_native_1", "header": "x-api-key", "origin": origin}}
    record = _engine(engine.base_url, native_passthrough=True, native=CALLS, connection={"credentials": stored})
    asked: list[tuple] = []

    def read(secret_id, **kwargs):
        asked.append((secret_id, kwargs.get("expected_origin")))
        return "stored-secret"

    with authenticated_profile(plan_for("native-stored-credential", caps=(), **WIDE)), patch.object(
        proxy.secret_store, "get_provider_secret", side_effect=read,
    ), patch.dict(proxy.config["backends"], {"garage": record}):
        response = _post(NATIVE_PATH)

    assert response.status_code == 200, response.text
    assert asked == [("sec_native_1", origin)]
    assert _sent_headers(engine.calls("chat")[0])["x-api-key"] == "stored-secret"


def test_binding_a_record_records_the_origin_of_each_class_credential():
    registry = {"services": {"garage": {
        "id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "10.0.0.5", "port": 8080, "native_passthrough": True, "connection": {"credentials": {
            "inference": {"secret_env": "A_KEY", "header": "x-api-key"},
            "control": {"secret_env": "B_KEY"},
        }}},
    }}}

    bound = service_registry.bind_credential_origins(registry, ["garage"])

    credentials = bound["services"]["garage"]["config"]["connection"]["credentials"]
    assert credentials["inference"]["origin"] == "http://10.0.0.5:8080"
    assert credentials["control"]["origin"] == "http://10.0.0.5:8080"


def test_a_credential_change_is_reported_for_the_owner_to_make():
    before = {"services": {"garage": {"id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
                                      "config": {"host": "h", "port": 1}}}}
    after = {"services": {"garage": {"id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
                                     "config": {"host": "h", "port": 1, "connection": {"credentials": {
                                         "control": {"secret_env": "B_KEY"}}}}}}}

    assert "garage" in service_registry.credential_binding_changes(before, after)


# --- the engine host is checked for SSRF ---------------------------------------------------------

@pytest.mark.parametrize("url", ["http://169.254.169.254", "http://[fd00:ec2::254]:80", "http://100.100.100.200"])
def test_a_native_call_to_a_metadata_address_is_refused_before_anything_is_sent(url):
    receipt_store.init_db()
    before = _receipts_for(NATIVE_PATH)

    with authenticated_profile(plan_for("native-ssrf", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(url, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH)

    _denied(response, "native_destination_blocked", status=502)
    assert _new_receipt(before, NATIVE_PATH)["status"] == "failed"


def test_a_native_call_to_the_gateway_itself_is_refused():
    own = f"http://127.0.0.1:{proxy.PROXY_PORT}"

    with authenticated_profile(plan_for("native-ssrf-self", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(own, native_passthrough=True)},
    ):
        response = _post(NATIVE_PATH)

    _denied(response, "native_destination_blocked", status=502)


def test_a_name_that_resolves_to_a_blocked_address_is_refused_when_the_call_is_made():
    async def resolve(host, port=None):
        return ["169.254.169.254"]

    with authenticated_profile(plan_for("native-ssrf-dns", caps=(), **WIDE)), patch.object(
        engine_check, "_default_resolve", resolve,
    ), patch.dict(proxy.config["backends"], {"garage": _engine("http://engine.example.test:8080", native_passthrough=True)}):
        response = _post(NATIVE_PATH)

    _denied(response, "native_destination_blocked", status=502)


def test_a_call_dials_the_checked_address_and_only_that(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="ok"))
    dialed: list[tuple] = []
    real = engine_check.check_destination

    async def spy(url, **kwargs):
        destination = await real(url, **kwargs)
        dialed.append((destination.transport, destination.addresses))
        return destination

    with authenticated_profile(plan_for("native-ssrf-pinned", caps=(), **WIDE)), patch.object(
        engine_check, "check_destination", spy,
    ), patch.dict(proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True)}):
        response = _post(NATIVE_PATH)

    assert response.status_code == 200, response.text
    assert dialed == [("ok", ("127.0.0.1",))]


@pytest.mark.parametrize("host,port", [("169.254.169.254", 80), ("fd00:ec2::254", 80), ("100.100.100.200", 80)])
def test_an_engine_record_for_a_metadata_address_cannot_be_saved_with_native_passthrough(host, port):
    record = {"id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
              "config": {"host": host, "port": port, "native_passthrough": True, "native": ALLOW_CHAT}}

    with pytest.raises(ValueError, match="native"):
        service_registry.normalize_service_registry({"services": {"garage": record}})

    # The same address with native passthrough off is an ordinary engine record: the lane is what is refused.
    quiet = {**record, "config": {**record["config"], "native_passthrough": False}}
    service_registry.normalize_service_registry({"services": {"garage": quiet}})


def test_an_engine_record_that_points_the_lane_at_the_gateway_port_cannot_be_saved():
    record = {"id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
              "config": {"host": "127.0.0.1", "port": 5000, "native_passthrough": True, "native": ALLOW_CHAT}}

    with pytest.raises(ValueError, match="gateway"):
        service_registry.normalize_service_registry({"services": {"garage": record}})


def test_saving_an_engine_whose_name_resolves_to_a_blocked_address_is_refused(tmp_path):
    from gateway import admin_store
    from gateway.admin_api import AdminContext
    from gateway.admin_runtime import AdminSQLiteStore

    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    store = AdminSQLiteStore(db)
    actor = AdminContext(subject="owner", email="o@example.test", roles=frozenset({"owner"}))
    registry = store.get_service_registry()
    registry["services"] = {"garage": {
        "id": "garage", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "engine.example.test", "port": 8080, "native_passthrough": True, "native": ALLOW_CHAT},
    }}

    async def resolve(host, port=None):
        return ["169.254.169.254"]

    with patch.object(engine_check, "_default_resolve", resolve), pytest.raises(ValueError, match="native"):
        store.update_service_registry(data=registry, actor=actor, expected_generation=0)


# --- ambiguous paths -----------------------------------------------------------------------------

def test_a_path_that_matches_a_control_entry_only_by_letter_case_is_refused(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"allow": [
        {"method": "POST", "path": "/v1/Embeddings", "class": "control"},
        {"method": "POST", "path": "/v1/{action}", "class": "generate"},
    ]}

    with authenticated_profile(plan_for("native-ambiguous", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        response = TestClient(proxy.app).post("/native/garage/v1/embeddings", headers=AUTH_HEADERS)

    _denied(response, "native_ambiguous_path")
    assert engine.requests == []


def test_an_engine_declared_case_insensitive_treats_the_folded_path_as_the_control_call(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"case_insensitive": True, "allow": [
        {"method": "POST", "path": "/v1/Embeddings", "class": "control"},
        {"method": "POST", "path": "/v1/{action}", "class": "generate"},
    ]}

    with authenticated_profile(plan_for("native-case-insensitive", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        response = TestClient(proxy.app).post("/native/garage/v1/embeddings", headers=AUTH_HEADERS)

    _denied(response, "native_owner_only")  # control wins: a non-owner never reaches the generate entry
    assert engine.requests == []


# --- HEAD and the engine's own record ------------------------------------------------------------

def test_a_head_request_to_the_native_lane_is_not_served(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate"}]}

    with authenticated_profile(plan_for("native-head", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, native=allow)},
    ):
        response = TestClient(proxy.app).head("/native/garage/v1/models", headers=AUTH_HEADERS)

    assert response.status_code in {404, 405}
    assert engine.requests == []


def test_the_policy_is_read_from_the_engines_own_record_whatever_the_case_of_its_key(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/chat-text", model="demo-chat", content="ok"))
    other = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))

    with authenticated_profile(plan_for("native-own-record", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"], {
            "Garage": _engine(engine.base_url, native_passthrough=True, downstream_service="shared"),
            "shared": _engine(other.base_url, native={"allow": [
                {"method": "POST", "path": "/v1/embeddings", "class": "generate"}]}),
        },
    ):
        allowed = _post(NATIVE_PATH)
        not_allowed = _post("/native/garage/v1/embeddings")

    assert allowed.status_code == 200, allowed.text
    _denied(not_allowed, "native_not_allowed")
    assert other.requests == []


def test_a_remote_provider_under_a_mixed_case_key_is_still_refused(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))

    with authenticated_profile(plan_for("native-remote-case", caps=(), **WIDE)), patch.dict(
        proxy.config["backends"],
        {"Hosted": _engine(engine.base_url, native_passthrough=True, remote_provider=True)},
    ):
        response = _post("/native/hosted/v1/chat/completions")

    _denied(response, "native_remote_forbidden")
    assert engine.requests == []
