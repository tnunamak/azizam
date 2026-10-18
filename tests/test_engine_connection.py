"""The final hop uses owner connection data and origin-bound credentials."""
from pathlib import Path

import pytest

from gateway import admin_store, secret_store
from gateway.engine_connection import inference_connection


def connection(config, tmp_path, **kwargs):
    return inference_connection({"config": config}, "/chat/completions",
                                fallback_address="http://fallback.invalid/v1", fallback_timeout=99,
                                db_path=tmp_path / "admin.db", env=lambda name: "test-value", **kwargs)


@pytest.mark.parametrize("approved,expected", [
    ("http://server.invalid:80", "Bearer test-value"),
    ("http://other.invalid:80", None),
    ("https://server.invalid:443", None),
    ("", None),
])
def test_environment_credential_only_goes_to_its_approved_origin(tmp_path, approved, expected):
    result = connection({"address": "http://server.invalid/v1", "api_key_env": "ENGINE_TEST_KEY",
                         "credential_origin": approved}, tmp_path)
    assert result.headers.get("authorization") == expected


def test_stored_inference_key_uses_the_secret_store_origin_check(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    secret_store.put_provider_secret("engine:test:inference", "test-value", db_path=db,
                                     approved_origin="http://server.invalid:80")
    result = connection({"address": "http://server.invalid/v1", "api_key_secret_id": "engine:test:inference",
                         "credential_origin": "http://server.invalid:80"}, tmp_path)
    assert result.headers["authorization"] == "Bearer test-value"


def test_legacy_callers_keep_their_fallback_wire_request(tmp_path):
    result = inference_connection(None, "/chat/completions", fallback_address="http://fallback.invalid/v1",
                                  fallback_timeout=99, db_path=tmp_path / "absent.db", env=lambda name: None)
    assert result.url == "http://fallback.invalid/v1/chat/completions"
    assert result.headers == {"content-type": "application/json"}
    assert result.read_timeout == 99


def test_plain_headers_cannot_override_credentials_or_http_framing(tmp_path):
    result = connection({"address": "http://server.invalid/v1", "headers": [
        {"name": "Authorization", "value": "unbound"},
        {"name": "Host", "value": "other.invalid"},
        {"name": "X-Workspace", "value": "tenant"},
    ]}, tmp_path)
    assert result.headers == {"content-type": "application/json", "x-workspace": "tenant"}


def test_legacy_base_path_already_contains_the_saved_prefix(tmp_path):
    result = connection({"host": "server.invalid", "port": 80, "base_path": "/provider/v1", "path_prefix": "/v1"}, tmp_path)
    assert result.url == "http://server.invalid:80/provider/v1/chat/completions"


@pytest.mark.parametrize("timeout", ["invalid", True, False, -1, 0])
def test_invalid_raw_timeouts_keep_the_transport_default(tmp_path, timeout):
    assert connection({"address": "http://server.invalid/v1", "timeout": timeout}, tmp_path).read_timeout == 99


def test_missing_stored_key_is_a_connection_error_not_an_unauthenticated_request(tmp_path):
    from gateway.engine_connection import InferenceConnectionError
    admin_store.init_db(tmp_path / "admin.db")
    with pytest.raises(InferenceConnectionError, match="credential is unavailable"):
        connection({"address": "http://server.invalid/v1", "api_key_secret_id": "missing",
                    "credential_origin": "http://server.invalid:80"}, tmp_path)


@pytest.mark.parametrize("references,expected_name", [
    ({"config": {"api_key_ref": "NAMED_TEST_KEY"}}, "NAMED_TEST_KEY"),
    ({"secret_refs": {"api_key": "<external-secret>"}}, "AZIZAM_DOWNSTREAM_API_KEY"),
    ({"config": {"api_key_env": "NAMED_TEST_KEY", "api_key_secret_id": "unused"}}, "NAMED_TEST_KEY"),
])
def test_credential_references_have_the_same_precedence_as_direct_routing(tmp_path, references, expected_name):
    record = {"config": {"address": "http://server.invalid/v1", "credential_origin": "http://server.invalid:80",
                          **references.get("config", {})}, "secret_refs": references.get("secret_refs", {})}
    names = []
    def env(name):
        names.append(name)
        return "test-value"
    result = inference_connection(record, "/chat/completions", fallback_address="", fallback_timeout=99,
                                  db_path=tmp_path / "admin.db", env=env)
    assert result.headers["authorization"] == "Bearer test-value"
    assert names == [expected_name]


@pytest.mark.parametrize("references", [
    {"config": {"api_key_ref": "MISSING_TEST_KEY"}},
    {"secret_refs": {"api_key": "<external-secret>"}},
])
def test_missing_legacy_credentials_stop_the_request(tmp_path, references):
    from gateway.engine_connection import InferenceConnectionError
    record = {"config": {"address": "http://server.invalid/v1", "credential_origin": "http://server.invalid:80",
                          **references.get("config", {})}, "secret_refs": references.get("secret_refs", {})}
    with pytest.raises(InferenceConnectionError, match="credential is unavailable"):
        inference_connection(record, "/chat/completions", fallback_address="", fallback_timeout=99,
                             db_path=tmp_path / "admin.db", env=lambda name: None)
