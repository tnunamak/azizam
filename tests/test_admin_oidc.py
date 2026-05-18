"""Tests for the pure admin OIDC / session helpers."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_oidc import (  # noqa: E402
    AdminRole,
    AdminSession,
    CookieConfig,
    IDTokenClaimsError,
    OIDCConfig,
    OIDCConfigError,
    SessionError,
    StateRecord,
    authorization_url,
    bootstrap_admin_role,
    consume_state,
    csrf_token_for_session,
    decode_session_cookie,
    encode_session_cookie,
    generate_nonce,
    generate_pkce_pair,
    generate_state,
    load_oidc_config,
    record_state,
    session_from_claims,
    validate_id_token_claims,
)


SIGNING_KEY = b"a" * 32


def make_config(**overrides) -> OIDCConfig:
    defaults: dict = {
        "enabled": True,
        "issuer": "https://idp.example.com",
        "client_id": "client-123",
        "client_secret": "shh",
        "redirect_uri": "https://proxy.example.com/admin/callback",
        "scopes": ("openid", "email", "profile"),
        "allowed_email_domains": frozenset(),
        "admin_emails": frozenset(),
        "signing_key": SIGNING_KEY,
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/jwks",
        "clock_skew_seconds": 60,
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


class TestConfigLoad(unittest.TestCase):
    def test_disabled_config_skips_strict_checks(self) -> None:
        cfg = load_oidc_config({"enabled": False})
        self.assertFalse(cfg.enabled)
        # Disabled configs synthesize a placeholder signing key so they
        # construct without error.
        self.assertEqual(len(cfg.signing_key), 32)

    def test_enabled_requires_issuer(self) -> None:
        with self.assertRaises(OIDCConfigError):
            load_oidc_config({
                "enabled": True,
                "client_id": "c",
                "client_secret": "s",
                "redirect_uri": "https://x/cb",
                "signing_key": "a" * 32,
            })

    def test_signing_key_min_length(self) -> None:
        with self.assertRaises(OIDCConfigError):
            load_oidc_config({
                "enabled": True,
                "issuer": "https://i",
                "client_id": "c",
                "client_secret": "s",
                "redirect_uri": "https://x/cb",
                "signing_key": "short",
            })

    def test_resolves_secrets_from_env(self) -> None:
        cfg = load_oidc_config(
            {
                "enabled": True,
                "issuer": "https://i",
                "client_id": "c",
                "client_secret_env": "OIDC_CS",
                "redirect_uri": "https://x/cb",
                "signing_key_env": "OIDC_SIG",
                "authorization_endpoint": "https://i/authorize",
            },
            env={"OIDC_CS": "from-env", "OIDC_SIG": "k" * 40},
        )
        self.assertEqual(cfg.client_secret, "from-env")
        self.assertEqual(cfg.signing_key, b"k" * 40)

    def test_scopes_must_include_openid(self) -> None:
        with self.assertRaises(OIDCConfigError):
            load_oidc_config({
                "enabled": True,
                "issuer": "https://i",
                "client_id": "c",
                "client_secret": "s",
                "redirect_uri": "https://x/cb",
                "scopes": ["email"],
                "signing_key": "a" * 32,
            })

    def test_cookie_samesite_none_requires_secure(self) -> None:
        with self.assertRaises(OIDCConfigError):
            load_oidc_config({
                "enabled": True,
                "issuer": "https://i",
                "client_id": "c",
                "client_secret": "s",
                "redirect_uri": "https://x/cb",
                "signing_key": "a" * 32,
                "cookie": {"same_site": "none", "secure": False},
            })

    def test_cookie_defaults_are_secure(self) -> None:
        cookie = CookieConfig()
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "lax")

    def test_string_booleans_are_parsed_explicitly(self) -> None:
        cfg = load_oidc_config({
            "enabled": "false",
            "cookie": {
                "secure": "false",
                "http_only": "false",
            },
        })
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.cookie.secure)
        self.assertFalse(cfg.cookie.http_only)

    def test_unknown_boolean_string_is_rejected(self) -> None:
        with self.assertRaises(OIDCConfigError):
            load_oidc_config({"enabled": "sometimes"})


class TestAuthorizationURL(unittest.TestCase):
    def test_includes_state_nonce_pkce_scopes(self) -> None:
        cfg = make_config()
        pkce = generate_pkce_pair()
        state = generate_state()
        nonce = generate_nonce()
        url = authorization_url(cfg, state=state, nonce=nonce, pkce=pkce)

        parsed = urlparse(url)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path,
                         "https://idp.example.com/authorize")
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(params["state"], state)
        self.assertEqual(params["nonce"], nonce)
        self.assertEqual(params["code_challenge"], pkce.challenge)
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["client_id"], "client-123")
        self.assertEqual(params["redirect_uri"], cfg.redirect_uri)
        self.assertEqual(params["scope"], "openid email profile")

    def test_disabled_config_refuses_to_build_url(self) -> None:
        cfg = make_config(enabled=False)
        with self.assertRaises(OIDCConfigError):
            authorization_url(
                cfg,
                state="s",
                nonce="n",
                pkce=generate_pkce_pair(),
            )

    def test_extra_params_cannot_override_security_fields(self) -> None:
        cfg = make_config()
        pkce = generate_pkce_pair()
        url = authorization_url(
            cfg,
            state="STATE",
            nonce="NONCE",
            pkce=pkce,
            extra_params={"state": "EVIL", "prompt": "login"},
        )
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual(params["state"], "STATE")
        self.assertEqual(params["prompt"], "login")

    def test_pkce_challenge_is_sha256_of_verifier(self) -> None:
        import base64
        import hashlib

        pkce = generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(pkce.verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(pkce.challenge, expected)


class TestStateStore(unittest.TestCase):
    def test_round_trip(self) -> None:
        store: dict[str, StateRecord] = {}
        rec = record_state(
            store,
            nonce="N",
            pkce_verifier="V",
            return_to="/dashboard",
            now=1000.0,
        )
        popped = consume_state(store, rec.state, now=1010.0)
        self.assertEqual(popped.nonce, "N")
        self.assertEqual(popped.pkce_verifier, "V")
        self.assertEqual(popped.return_to, "/dashboard")
        self.assertNotIn(rec.state, store)

    def test_unknown_state_rejected(self) -> None:
        with self.assertRaises(SessionError):
            consume_state({}, "missing")

    def test_replay_rejected(self) -> None:
        store: dict[str, StateRecord] = {}
        rec = record_state(store, nonce="N", pkce_verifier="V", now=1000.0)
        consume_state(store, rec.state, now=1000.0)
        with self.assertRaises(SessionError):
            consume_state(store, rec.state, now=1000.0)

    def test_expired_state_rejected(self) -> None:
        store: dict[str, StateRecord] = {}
        rec = record_state(store, nonce="N", pkce_verifier="V", now=0.0)
        with self.assertRaises(SessionError):
            consume_state(store, rec.state, max_age_seconds=10, now=1000.0)


class TestIDTokenClaims(unittest.TestCase):
    def _claims(self, **overrides) -> dict:
        base = {
            "iss": "https://idp.example.com",
            "aud": "client-123",
            "sub": "user-42",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "NONCE",
            "exp": time.time() + 3600,
            "iat": time.time(),
        }
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        cfg = make_config()
        out = validate_id_token_claims(self._claims(), config=cfg, expected_nonce="NONCE")
        self.assertEqual(out["sub"], "user-42")

    def test_bad_issuer(self) -> None:
        cfg = make_config()
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(iss="https://evil"), config=cfg, expected_nonce="NONCE"
            )

    def test_bad_audience(self) -> None:
        cfg = make_config()
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(aud="other-client"), config=cfg, expected_nonce="NONCE"
            )

    def test_audience_as_list_ok(self) -> None:
        cfg = make_config()
        validate_id_token_claims(
            self._claims(aud=["other", "client-123"]),
            config=cfg,
            expected_nonce="NONCE",
        )

    def test_expired(self) -> None:
        cfg = make_config(clock_skew_seconds=0)
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(exp=time.time() - 10),
                config=cfg,
                expected_nonce="NONCE",
            )

    def test_expired_within_skew_ok(self) -> None:
        cfg = make_config(clock_skew_seconds=60)
        validate_id_token_claims(
            self._claims(exp=time.time() - 5),
            config=cfg,
            expected_nonce="NONCE",
        )

    def test_nonce_mismatch(self) -> None:
        cfg = make_config()
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(self._claims(), config=cfg, expected_nonce="OTHER")

    def test_missing_email(self) -> None:
        cfg = make_config()
        bad = self._claims()
        del bad["email"]
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(bad, config=cfg, expected_nonce="NONCE")

    def test_email_not_verified_rejected(self) -> None:
        cfg = make_config()
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(email_verified=False),
                config=cfg,
                expected_nonce="NONCE",
            )

    def test_domain_allow_list(self) -> None:
        cfg = make_config(allowed_email_domains=frozenset({"example.com"}))
        validate_id_token_claims(self._claims(), config=cfg, expected_nonce="NONCE")
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(email="bob@other.com"),
                config=cfg,
                expected_nonce="NONCE",
            )

    def test_admin_email_allow_list(self) -> None:
        cfg = make_config(admin_emails=frozenset({"alice@example.com"}))
        validate_id_token_claims(self._claims(), config=cfg, expected_nonce="NONCE")
        with self.assertRaises(IDTokenClaimsError):
            validate_id_token_claims(
                self._claims(email="bob@example.com"),
                config=cfg,
                expected_nonce="NONCE",
            )

    def test_open_allow_list_permits_anyone(self) -> None:
        cfg = make_config()
        validate_id_token_claims(
            self._claims(email="random@anywhere.io"),
            config=cfg,
            expected_nonce="NONCE",
        )


class TestSessionCookie(unittest.TestCase):
    def _session(self, **overrides) -> AdminSession:
        now = 1_700_000_000.0
        base: dict = {
            "subject": "user-42",
            "email": "alice@example.com",
            "role": AdminRole.ADMIN,
            "issued_at": now,
            "expires_at": now + 3600,
            "session_id": "sid-1",
        }
        base.update(overrides)
        return AdminSession(**base)

    def test_round_trip(self) -> None:
        sess = self._session()
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        decoded = decode_session_cookie(
            token, signing_key=SIGNING_KEY, now=1_700_000_100.0
        )
        self.assertEqual(decoded.subject, sess.subject)
        self.assertEqual(decoded.email, sess.email)
        self.assertEqual(decoded.role, sess.role)
        self.assertEqual(decoded.session_id, sess.session_id)

    def test_tampered_payload_rejected(self) -> None:
        sess = self._session(role=AdminRole.NONE)
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        version, payload_b64, mac = token.split(".")
        # Try to swap role to admin by re-encoding payload but keeping mac.
        import base64
        import json
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode("utf-8"))
        payload["role"] = AdminRole.OWNER
        new_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        bad = f"{version}.{new_payload_b64}.{mac}"
        with self.assertRaises(SessionError):
            decode_session_cookie(bad, signing_key=SIGNING_KEY, now=1_700_000_100.0)

    def test_wrong_signing_key_rejected(self) -> None:
        sess = self._session()
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        with self.assertRaises(SessionError):
            decode_session_cookie(
                token, signing_key=b"b" * 32, now=1_700_000_100.0
            )

    def test_expired_rejected(self) -> None:
        sess = self._session()
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        with self.assertRaises(SessionError):
            decode_session_cookie(
                token, signing_key=SIGNING_KEY, now=1_700_999_999.0
            )

    def test_unsupported_version_rejected(self) -> None:
        sess = self._session()
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        _, payload, mac = token.split(".")
        with self.assertRaises(SessionError):
            decode_session_cookie(
                f"v9.{payload}.{mac}",
                signing_key=SIGNING_KEY,
                now=1_700_000_100.0,
            )

    def test_payload_contains_no_raw_tokens(self) -> None:
        # Build a session from realistic ID-token claims and verify the
        # encoded cookie does not embed id_token / access_token / refresh.
        claims = {
            "sub": "user-42",
            "email": "alice@example.com",
            "id_token": "RAW.JWT.SHOULD_NEVER_LEAK",
            "access_token": "AT_SHOULD_NEVER_LEAK",
            "refresh_token": "RT_SHOULD_NEVER_LEAK",
        }
        cookie_cfg = CookieConfig(max_age_seconds=600)
        sess = session_from_claims(
            claims, role=AdminRole.ADMIN, cookie=cookie_cfg, now=1_700_000_000.0
        )
        token = encode_session_cookie(sess, signing_key=SIGNING_KEY)
        # Decode the payload chunk and assert no leakage.
        import base64
        import json
        payload_b64 = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "==").decode("utf-8")
        )
        self.assertNotIn("id_token", payload)
        self.assertNotIn("access_token", payload)
        self.assertNotIn("refresh_token", payload)
        # And the literal secret bytes are not anywhere in the cookie blob.
        for leaked in ("RAW.JWT.SHOULD_NEVER_LEAK", "AT_SHOULD_NEVER_LEAK", "RT_SHOULD_NEVER_LEAK"):
            self.assertNotIn(leaked, token)

    def test_csrf_token_is_stable_for_session_and_key_bound(self) -> None:
        sess = self._session()
        token = csrf_token_for_session(sess, signing_key=SIGNING_KEY)
        self.assertEqual(
            token,
            csrf_token_for_session(sess, signing_key=SIGNING_KEY),
        )
        self.assertNotEqual(
            token,
            csrf_token_for_session(sess, signing_key=b"b" * 32),
        )


class TestBootstrapRole(unittest.TestCase):
    def test_first_login_becomes_owner(self) -> None:
        cfg = make_config()
        role = bootstrap_admin_role("alice@example.com", config=cfg, existing_roles={})
        self.assertEqual(role, AdminRole.OWNER)

    def test_existing_owner_blocks_bootstrap(self) -> None:
        cfg = make_config()
        role = bootstrap_admin_role(
            "bob@example.com",
            config=cfg,
            existing_roles={"alice@example.com": AdminRole.OWNER},
        )
        self.assertEqual(role, AdminRole.NONE)

    def test_existing_admin_blocks_bootstrap(self) -> None:
        cfg = make_config()
        role = bootstrap_admin_role(
            "carol@example.com",
            config=cfg,
            existing_roles={"alice@example.com": AdminRole.ADMIN},
        )
        self.assertEqual(role, AdminRole.NONE)

    def test_configured_admin_email_gets_admin(self) -> None:
        cfg = make_config(admin_emails=frozenset({"bob@example.com"}))
        role = bootstrap_admin_role(
            "bob@example.com",
            config=cfg,
            existing_roles={"alice@example.com": AdminRole.OWNER},
        )
        self.assertEqual(role, AdminRole.ADMIN)

    def test_existing_role_preserved(self) -> None:
        cfg = make_config(admin_emails=frozenset({"bob@example.com"}))
        role = bootstrap_admin_role(
            "bob@example.com",
            config=cfg,
            existing_roles={"bob@example.com": AdminRole.OWNER},
        )
        # bob was promoted to owner manually; bootstrap must not demote.
        self.assertEqual(role, AdminRole.OWNER)

    def test_email_normalization(self) -> None:
        cfg = make_config(admin_emails=frozenset({"bob@example.com"}))
        role = bootstrap_admin_role(
            "  BOB@Example.COM ",
            config=cfg,
            existing_roles={"alice@example.com": AdminRole.OWNER},
        )
        self.assertEqual(role, AdminRole.ADMIN)


if __name__ == "__main__":
    unittest.main()
