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
