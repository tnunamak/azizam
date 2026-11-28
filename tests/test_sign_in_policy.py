"""The sign-in policy is one release data file, and every former code copy reads it (code-to-data move 7)."""

from __future__ import annotations

import asyncio
from http.cookies import CookieError
import json
import re
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
import yaml
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gateway import admin_oidc, oidc_browser_flow, oidc_jwks, sign_in_policy, signin_api

ROOT = Path(__file__).resolve().parent.parent


def _changed_policy(monkeypatch, tmp_path, **changes):
    """Point the reader at a copy of the release file with *changes* applied (dotted keys)."""
    document = yaml.safe_load(sign_in_policy.BUNDLED_PATH.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        node = document
        *parents, leaf = dotted.split(".")
        for name in parents:
            node = node[name]
        node[leaf] = value
    copy = tmp_path / "sign_in_policy.yml"
    copy.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(sign_in_policy, "BUNDLED_PATH", copy)
    sign_in_policy.policy.cache_clear()
    return sign_in_policy.policy()


@pytest.fixture(autouse=True)
def _reset_policy():
    sign_in_policy.policy.cache_clear()
    yield
    sign_in_policy.policy.cache_clear()


def test_the_release_policy_gives_the_values_the_code_gave():
    p = sign_in_policy.policy()
    assert p.provider_enabled is False
    assert p.scopes == ("openid", "email", "profile")
    assert (p.groups_claim, p.clock_skew_seconds, p.display_name_max_chars) == ("groups", 60, 80)
    assert (p.session_cookie_name, p.session_cookie_max_age_seconds) == ("opx_admin_session", 8 * 3600)
    assert (p.pending_login_seconds, p.max_pending_logins) == (600, 1024)
    assert p.auth_time_clock_skew_seconds == 60
    assert (p.jwks_ttl_seconds, p.jwks_refresh_interval_seconds) == (300, 30)
    assert p.connection_attempt_seconds == 600


def test_an_omitted_provider_key_takes_the_policy_and_an_explicit_empty_scope_list_stays_empty():
    cfg = admin_oidc.load_oidc_config({})
    assert cfg.enabled is False and cfg.scopes == ("openid", "email", "profile")
    assert (cfg.groups_claim, cfg.clock_skew_seconds) == ("groups", 60)
    assert (cfg.cookie.name, cfg.cookie.max_age_seconds) == ("opx_admin_session", 28800)
    assert admin_oidc.load_oidc_config({"scopes": []}).scopes == ()
    explicit = admin_oidc.load_oidc_config({"scopes": ["openid"], "groups_claim": "roles", "clock_skew_seconds": 5,
                                            "cookie": {"name": "mine", "max_age_seconds": 9}})
    assert (explicit.scopes, explicit.groups_claim, explicit.clock_skew_seconds) == (("openid",), "roles", 5)
    assert (explicit.cookie.name, explicit.cookie.max_age_seconds) == ("mine", 9)


def test_the_two_scope_defaults_are_one_declaration(monkeypatch, tmp_path):
    """admin_oidc's omitted-scope default and the Settings form's empty-box default were two literals; now one."""
    assert tuple(signin_api.ConnectionBody().scopes.split()) == sign_in_policy.policy().scopes
    assert signin_api.ConnectionBody().groups_claim == sign_in_policy.policy().groups_claim
    assert signin_api.ConnectionBody().clock_skew_seconds == sign_in_policy.policy().clock_skew_seconds
    _changed_policy(monkeypatch, tmp_path, **{"provider.scopes": ["openid", "email"]})
    assert admin_oidc.load_oidc_config({}).scopes == ("openid", "email")
    assert admin_oidc._coerce_scopes(None) == ("openid", "email")


def test_the_provider_and_cookie_defaults_follow_the_data(monkeypatch, tmp_path):
    _changed_policy(monkeypatch, tmp_path, **{
        "provider.groups_claim": "roles", "provider.clock_skew_seconds": 7, "provider.display_name_max_chars": 5,
        "session_cookie.name": "other_cookie", "session_cookie.max_age_seconds": 11})
    cfg = admin_oidc.load_oidc_config({})
    assert (cfg.groups_claim, cfg.clock_skew_seconds) == ("roles", 7)
    assert (cfg.cookie.name, cfg.cookie.max_age_seconds) == ("other_cookie", 11)
    assert admin_oidc.CookieConfig().name == "other_cookie"
    assert admin_oidc.access_names_from_oidc_claims({"roles": ["x"]}, client_id="c")["oidc_group"] == ("x",)
    with pytest.raises(admin_oidc.OIDCConfigError, match="at most 5 characters"):
        admin_oidc.validate_oidc_display_name("abcdef")


def test_the_pending_login_and_its_browser_cookie_share_one_lifetime(monkeypatch, tmp_path):
    _changed_policy(monkeypatch, tmp_path, **{"pending_login.seconds": 7, "pending_login.max_pending": 2})
    store: dict = {}
    first = admin_oidc.record_state(store, nonce="n", pkce_verifier="v", now=100.0)
    admin_oidc.record_state(store, nonce="n", pkce_verifier="v", now=100.0)
    with pytest.raises(admin_oidc.SessionError, match="too many pending"):
        admin_oidc.record_state(store, nonce="n", pkce_verifier="v", now=100.0)
    with pytest.raises(admin_oidc.SessionError, match="state expired"):
        admin_oidc.consume_state(store, first.state, now=108.0)

    cfg = admin_oidc.OIDCConfig(
        enabled=True, issuer="https://issuer.test", client_id="client", client_secret="fake-secret",
        redirect_uri="https://gateway.test/admin/oidc/callback", scopes=("openid",), admin_emails=frozenset(),
        signing_key=b"k" * 32, authorization_endpoint="https://issuer.test/authorize")
    store = {}
    response = oidc_browser_flow.begin_browser_login(cfg, store, return_to="/")
    assert "Max-Age=7" in response.headers["set-cookie"]
    (pending,) = store.values()
    store[pending.state] = replace(pending, created_at=pending.created_at - 8)
    with pytest.raises(admin_oidc.SessionError, match="state expired"):
        admin_oidc.consume_state(store, pending.state)


def test_the_auth_time_allowance_follows_the_data(monkeypatch, tmp_path):
    claims = {"auth_time": 1000 - 30, "iat": 1000}
    assert admin_oidc.provider_signed_in_again(claims, since=1000)
    _changed_policy(monkeypatch, tmp_path, auth_time_clock_skew_seconds=10)
    assert not admin_oidc.provider_signed_in_again(claims, since=1000)


def test_the_signing_key_cache_times_follow_the_data(monkeypatch, tmp_path):
    assert (oidc_jwks.JWKSCache().ttl_seconds, oidc_jwks.JWKSCache().refresh_interval) == (300, 30)
    _changed_policy(monkeypatch, tmp_path, **{"jwks.ttl_seconds": 9, "jwks.refresh_interval_seconds": 3})
    assert (oidc_jwks.JWKSCache().ttl_seconds, oidc_jwks.JWKSCache().refresh_interval) == (9, 3)
    assert oidc_jwks.JWKSCache(ttl_seconds=1, refresh_interval=2).ttl_seconds == 1


def test_a_plan_connection_attempt_lifetime_follows_the_data(monkeypatch, tmp_path):
    from tests.support.subscription_fakes import Upstream, make_service

    _changed_policy(monkeypatch, tmp_path, connection_attempt_seconds=42)
    service, store, _ = make_service(tmp_path, Upstream())
    started = asyncio.run(service.start("owner_1", "chatgpt", session=None))
    assert started["expires_in"] == 42
    attempt = store.take_attempt(attempt_id=started["attempt_id"], owner_id="owner_1", consume=False)
    assert attempt.expires_at == 1_000_042


def test_the_name_length_message_of_the_settings_api_follows_the_data(monkeypatch, tmp_path):
    _changed_policy(monkeypatch, tmp_path, **{"provider.display_name_max_chars": 5})
    app = FastAPI()
    router = APIRouter(route_class=signin_api._SignInRoute)

    @router.get("/x")
    async def boom():
        admin_oidc.validate_oidc_display_name("abcdef")

    app.include_router(router)
    body = TestClient(app).get("/x").json()
    assert body["error"]["message"] == "The name must be plain text of at most 5 characters."


def test_the_policy_file_is_checked_by_its_schema_and_requires_openid():
    schema = json.loads(sign_in_policy.SCHEMA_PATH.read_text(encoding="utf-8"))
    document = yaml.safe_load(sign_in_policy.BUNDLED_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(document)
    without_openid = {**document, "provider": {**document["provider"], "scopes": ["email"]}}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(without_openid)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**document, "issuer": "https://issuer.test"})


def test_the_sign_in_form_defaults_in_the_web_app_match_the_policy():
    """The web form keeps its own copy (static export); this pins it to the data until the snapshot carries it."""
    source = (ROOT / "web" / "src" / "app" / "settings" / "sign-in" / "copy.ts").read_text(encoding="utf-8")
    block = re.search(r"export const DEFAULTS = \{(.*?)\};", source, re.S).group(1)
    p = sign_in_policy.policy()
    assert re.search(r'scopes:\s*"([^"]*)"', block).group(1) == " ".join(p.scopes)
    assert re.search(r'groupsClaim:\s*"([^"]*)"', block).group(1) == p.groups_claim
    assert int(re.search(r"clockSkewSeconds:\s*(\d+)", block).group(1)) == p.clock_skew_seconds


@pytest.mark.parametrize("mutation", ["bound", "duplicate", "cookie", "float", "refresh"])
def test_reader_rejects_invalid_release_data(monkeypatch, tmp_path, mutation):
    source = sign_in_policy.BUNDLED_PATH.read_text(encoding="utf-8")
    replacements = {
        "bound": ("auth_time_clock_skew_seconds: 60", "auth_time_clock_skew_seconds: 86400"),
        "duplicate": ("  seconds: 600", "  seconds: 600\n  seconds: 20"),
        "cookie": ("name: opx_admin_session", "name: secure"),
        "float": ("  seconds: 600", "  seconds: 600.0"),
        "refresh": ("refresh_interval_seconds: 30", "refresh_interval_seconds: 301"),
    }
    old, new = replacements[mutation]
    assert old in source
    path = tmp_path / "policy.yml"
    path.write_text(source.replace(old, new), encoding="utf-8")
    monkeypatch.setattr(sign_in_policy, "BUNDLED_PATH", path)
    with pytest.raises((ValueError, CookieError, jsonschema.ValidationError, yaml.YAMLError)):
        sign_in_policy.policy()


def test_policy_changes_require_restart(monkeypatch, tmp_path):
    first = _changed_policy(monkeypatch, tmp_path, connection_attempt_seconds=42)
    path = sign_in_policy.BUNDLED_PATH
    path.write_text(path.read_text().replace('connection_attempt_seconds: 42', 'connection_attempt_seconds: 43'))
    assert sign_in_policy.policy() is first
    sign_in_policy.policy.cache_clear()
    assert sign_in_policy.policy().connection_attempt_seconds == 43


def test_listener_timeout_follows_policy(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from gateway.connections import oauth_pkce

    _changed_policy(monkeypatch, tmp_path, connection_attempt_seconds=42)
    async def fake_open(self):
        return True
    monkeypatch.setattr(oauth_pkce.CallbackListener, "open", fake_open)
    mechanism = oauth_pkce.OAuthPkceMechanism(None)
    ctx = SimpleNamespace(kind=SimpleNamespace(oauth={"redirect_uri": "http://localhost:1234/callback"}))
    listener = asyncio.run(mechanism.listen_for_callback(ctx, None))
    assert listener._timeout == 42


@pytest.mark.parametrize("field,value", [
    ("provider.clock_skew_seconds", 301),
    ("provider.display_name_max_chars", 1001),
    ("session_cookie.max_age_seconds", 86401),
    ("pending_login.seconds", 3601),
    ("pending_login.max_pending", 100001),
    ("auth_time_clock_skew_seconds", 301),
    ("jwks.ttl_seconds", 86401),
    ("jwks.refresh_interval_seconds", 86401),
    ("connection_attempt_seconds", 3601),
])
def test_reader_rejects_each_numeric_limit(monkeypatch, tmp_path, field, value):
    with pytest.raises((jsonschema.ValidationError, ValueError)):
        _changed_policy(monkeypatch, tmp_path, **{field: value})


def test_startup_and_the_loader_agree_on_a_provider_that_omits_enabled(monkeypatch, tmp_path):
    """A release policy that turns providers on by default gets the session signing key at startup too."""
    import proxy

    _changed_policy(monkeypatch, tmp_path, **{"provider.enabled": True})
    monkeypatch.setattr(proxy, "admin_cfg", {"oidc": {"issuer": "https://id.example"}, "session_signing_key": "k" * 40})
    bootstrap = proxy._bootstrap_admin_oidc_cfg()
    assert admin_oidc.provider_enabled(bootstrap) is True
    assert bootstrap["signing_key"] == "k" * 40
    monkeypatch.setattr(proxy, "admin_cfg", {"oidc": {"issuer": "https://id.example", "enabled": "off"},
                                             "session_signing_key": "k" * 40})
    assert "signing_key" not in proxy._bootstrap_admin_oidc_cfg()


def test_the_transactional_role_helper_takes_the_policy_role_window(monkeypatch, tmp_path):
    """effective_user_roles_in (used inside the caller's transaction) reads the same window as effective_admin_roles."""
    import sqlite3

    from gateway import admin_store

    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    now = 1_000_000
    admin_store.reconcile_oidc_admin_roles("person-1", ["admin"], now=now - 20, path=db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row  # the store reads rows by column name
        assert "admin" in admin_store.effective_user_roles_in(conn, "person-1", now=now)
        _changed_policy(monkeypatch, tmp_path, **{"session_cookie.max_age_seconds": 10})
        assert "admin" not in admin_store.effective_user_roles_in(conn, "person-1", now=now)
        assert "admin" in admin_store.effective_user_roles_in(conn, "person-1", now=now, oidc_max_age=30)
