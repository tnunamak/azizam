"""Pure OIDC / admin-session helpers for the control plane.

This module is **only** concerned with the human admin dashboard login path —
it never authenticates data-plane API requests, never reads files, never
touches ``os.environ`` directly, and never makes network calls. It is meant
to be wired into ``proxy.py`` via a thin FastAPI callback route living in
that module; the route handler stays small because all of the policy lives
here.

What lives here:

* :class:`OIDCConfig` and :class:`CookieConfig` — plain dataclasses loaded
  from a dict / env-shaped mapping.
* Authorization-URL builder with ``state``, ``nonce``, and PKCE
  (``S256``) parameters.
* Signed admin session token (HMAC-SHA256) helpers. **No raw OIDC id_token
  or access_token ever lands in the cookie payload** — only the claims the
  control plane actually needs (subject + email + role + expiry).
* ``state`` / ``nonce`` storage + validation primitives that are pure (a
  caller-supplied dict-like store).
* An ID-token *claim-shape* validator (``validate_id_token_claims``) that
  checks issuer, audience, expiry, nonce, and email/domain allow-list given
  an already-decoded claims dict. Cryptographic JWT signature verification
  is delegated to an adapter boundary (:class:`IDTokenVerifier`) so this
  module is fully testable without network or a JWKS dependency.
* Role bootstrap helper that seeds a configured, verified owner under the
  trusted issuer only when no grant exists.

Security defaults chosen for r/selfhosted operators:

* Cookies default to ``Secure``, ``HttpOnly``, ``SameSite=Lax``.
* PKCE always uses ``S256`` (plain is never offered).
* ``state`` and ``nonce`` are 256-bit URL-safe tokens.
* HMAC verification uses ``hmac.compare_digest``.
* Signing-secret must be at least 32 bytes; shorter secrets raise on
  config load so a misconfigured deployment fails fast at startup, not at
  first login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, MutableMapping, Protocol
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from gateway import ui_routes


__all__ = [
    "AdminRole",
    "AdminSession",
    "CookieConfig",
    "IDTokenClaimsError",
    "IDTokenVerifier",
    "OIDCConfig",
    "OIDCConfigError",
    "PKCEPair",
    "SessionError",
    "SignInRefused",
    "StateRecord",
    "authorization_url",
    "admin_role_from_oidc_claims",
    "access_names_from_oidc_claims",
    "bootstrap_admin_role",
    "consume_state",
    "canonical_oidc_group",
    "csrf_token_for_session",
    "encode_session_cookie",
    "decode_session_cookie",
    "generate_pkce_pair",
    "generate_nonce",
    "generate_state",
    "load_oidc_config",
    "record_state",
    "session_from_claims",
    "validate_oidc_stored_override",
    "validate_id_token_claims",
    "safe_browser_return_to",
    "oidc_config_for_public_base",
]


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class OIDCConfigError(ValueError):
    """Raised when the OIDC config dict is malformed or insecure."""


class IDTokenClaimsError(ValueError):
    """Raised when an ID-token claims dict fails validation."""


class SignInRefused(IDTokenClaimsError):
    """A signed, current token for a person the instance does not admit.

    Only the checks on who the person is raise this (unverified email, email
    not allowed). The caller may end that person's identity-provider access,
    which a malformed or replayed token must never be able to do.
    """


class SessionError(ValueError):
    """Raised when a session cookie is malformed, tampered with, or expired."""


def safe_browser_return_to(value: str | None, *, default: str = "/") -> str:
    """Keep return destinations on a web app page or /status/raw.

    The query and fragment are kept. Everything else, including API paths,
    other hosts and paths with dot segments, falls back to ``default``.
    tests/fixtures/return_paths.json is shared with the web app's mirror,
    safeContinuePath.
    """
    if not value or not value.startswith("/"):
        return default
    decoded = unquote(value)
    if decoded.startswith("//") or "\\" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return default
    try:
        target = urlsplit(value)
    except ValueError:
        return default
    if target.netloc or target.scheme:
        return default
    path = unquote(target.path)
    if ui_routes.is_page_path(path) or path in {"/status/raw", "/status/raw/"}:
        return value
    return default


def oidc_config_for_public_base(config: OIDCConfig, public_base_url: str | None) -> OIDCConfig:
    """Use the configured public origin while preserving the registered callback path."""
    if not public_base_url:
        return config
    origin = urlsplit(public_base_url)
    callback = urlsplit(config.redirect_uri)
    return replace(config, redirect_uri=urlunsplit((origin.scheme, origin.netloc, callback.path, callback.query, "")))


# ---------------------------------------------------------------------------
# Config dataclasses.
# ---------------------------------------------------------------------------


# Roles are intentionally a small closed set — the control plane only needs
# to know "is this caller allowed to mutate admin state, and are they the
# bootstrap owner."
class AdminRole:
    OWNER = "owner"
    ADMIN = "admin"
    NONE = "none"

    ALL = frozenset({OWNER, ADMIN, NONE})


@dataclass(frozen=True)
class CookieConfig:
    """Settings for the signed admin-session cookie."""

    name: str = "opx_admin_session"
    max_age_seconds: int = 60 * 60 * 8  # 8h default — admins re-auth daily-ish
    secure: bool = True
    http_only: bool = True
    same_site: str = "lax"  # "lax" | "strict" | "none"
    path: str = "/"
    domain: str | None = None

    def __post_init__(self) -> None:
        if self.same_site not in {"lax", "strict", "none"}:
            raise OIDCConfigError(
                f"cookie same_site must be lax|strict|none, got {self.same_site!r}"
            )
        if self.same_site == "none" and not self.secure:
            # Browsers reject SameSite=None without Secure; fail loudly.
            raise OIDCConfigError(
                "cookie same_site=none requires secure=true"
            )
        if self.max_age_seconds <= 0:
            raise OIDCConfigError("cookie max_age_seconds must be > 0")


_MIN_SIGNING_KEY_BYTES = 32


@dataclass(frozen=True)
class OIDCConfig:
    """All inputs required to do admin OIDC.

    ``client_secret`` is provided pre-resolved by the caller — this module
    never reads env vars itself. Callers should resolve ``client_secret_env``
    against their env mapping before constructing the config.
    """

    enabled: bool
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    allowed_email_domains: frozenset[str]
    admin_emails: frozenset[str]
    signing_key: bytes
    cookie: CookieConfig = field(default_factory=CookieConfig)
    # Authorization / token endpoints are usually discovered from the
    # issuer's ``.well-known/openid-configuration``. We accept them as
    # plain values so the discovery side can stay in proxy.py (or a small
    # adapter) without this module taking on network responsibility.
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""
    # Allow clock skew when checking ``exp`` / ``iat``.
    clock_skew_seconds: int = 60
    # Optional IdP-driven admin grants. Keycloak commonly emits groups as
    # "/trusted-users" and roles under realm_access/resource_access.
    admin_groups: frozenset[str] = field(default_factory=frozenset)
    admin_roles: frozenset[str] = field(default_factory=frozenset)
    # Name of the ID-token claim that carries the user's group memberships.
    # Defaults to "groups" (the de-facto standard for Keycloak, Okta, etc.).
    # Change this if your IdP uses a different claim name (e.g. "roles",
    # "cognito:groups", "https://example.com/groups").
    groups_claim: str = "groups"
    display_name: str = ""

    def __post_init__(self) -> None:
        validate_oidc_display_name(self.display_name)
        if not self.enabled:
            return  # Disabled config doesn't need to be fully valid.
        for field_name in ("issuer", "client_id", "client_secret", "redirect_uri"):
            if not getattr(self, field_name):
                raise OIDCConfigError(f"{field_name} is required when enabled=true")
        if "openid" not in self.scopes:
            raise OIDCConfigError("scopes must include 'openid'")
        if len(self.signing_key) < _MIN_SIGNING_KEY_BYTES:
            raise OIDCConfigError(
                f"signing_key must be at least {_MIN_SIGNING_KEY_BYTES} bytes"
            )
        if self.clock_skew_seconds < 0:
            raise OIDCConfigError("clock_skew_seconds must be >= 0")


def _coerce_str_set(values: Any) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        items: Iterable[str] = [values]
    else:
        try:
            items = list(values)
        except TypeError as exc:
            raise OIDCConfigError(f"expected iterable, got {type(values).__name__}") from exc
    return frozenset(str(v).strip().lower() for v in items if str(v).strip())


def canonical_oidc_group(value: str) -> str:
    """Interpret a short group name as a root path, preserving its exact name."""
    return value if value.startswith("/") else "/" + value


def _coerce_access_name_set(values: Any) -> frozenset[str]:
    """Load exact authorization names; separators are only config list syntax."""
    if values is None:
        return frozenset()
    if isinstance(values, str):
        items: Iterable[Any] = [
            part.strip() for chunk in values.splitlines() for part in chunk.split(",")
        ]
    else:
        try:
            items = list(values)
        except TypeError as exc:
            raise OIDCConfigError(f"expected iterable, got {type(values).__name__}") from exc
    return frozenset(str(item) for item in items if str(item))


def _coerce_scopes(values: Any) -> tuple[str, ...]:
    if values is None:
        return ("openid", "email", "profile")
    if isinstance(values, str):
        parts = values.split()
    else:
        try:
            parts = [str(v) for v in values]
        except TypeError as exc:
            raise OIDCConfigError("scopes must be a string or iterable") from exc
    cleaned = tuple(p.strip() for p in parts if p.strip())
    # ``None`` means the field was omitted and receives the runtime default.
    # An explicit empty list is a persisted operator clear and must remain
    # empty through parsing and response projection.
    return cleaned


def validate_oidc_display_name(value: Any) -> str:
    """Accept only bounded, single-line public text; never echo rejected input."""
    if not isinstance(value, str):
        raise OIDCConfigError("display_name must be a string")
    if len(value) > 80:
        raise OIDCConfigError("display_name must be at most 80 characters")
    if any(
        char in "<>" or unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        raise OIDCConfigError("display_name must be plain text without markup or control characters")
    return value.strip()


_STORED_OIDC_STRING_FIELDS = frozenset(
    {
        "issuer",
        "client_id",
        "client_secret",
        "redirect_uri",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "groups_claim",
        "display_name",
    }
)
_STORED_OIDC_LIST_FIELDS = frozenset(
    {
        "scopes",
        "allowed_email_domains",
        "admin_emails",
        "admin_groups",
        "admin_roles",
    }
)


def validate_oidc_stored_override(data: Mapping[str, Any]) -> None:
    """Reject malformed persisted OIDC values before they influence runtime.

    Bootstrap configuration supports a few convenient environment-shaped
    forms. The database is a typed control-plane boundary: its scalar values
    must be strings and its list values must be lists of strings. This avoids
    silently turning corrupt persisted data into an apparently valid provider
    configuration.
    """
    for key in _STORED_OIDC_STRING_FIELDS & data.keys():
        if not isinstance(data[key], str):
            raise OIDCConfigError(f"stored {key} must be a string")
    if "display_name" in data:
        validate_oidc_display_name(data["display_name"])
    for key in _STORED_OIDC_LIST_FIELDS & data.keys():
        value = data[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise OIDCConfigError(f"stored {key} must be a list of strings")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise OIDCConfigError("stored enabled must be a boolean")
    if "clock_skew_seconds" in data and (
        isinstance(data["clock_skew_seconds"], bool)
        or not isinstance(data["clock_skew_seconds"], int)
    ):
        raise OIDCConfigError("stored clock_skew_seconds must be an integer")


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise OIDCConfigError(f"expected boolean value, got {value!r}")


def _coerce_signing_key(raw: Any) -> bytes:
    if raw is None:
        raise OIDCConfigError("signing_key is required")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        # Treat as utf-8 — operators commonly paste a hex/base64/random
        # string. We don't try to base64-decode magically; the byte length
        # check in __post_init__ catches obviously too-short values.
        return raw.encode("utf-8")
    raise OIDCConfigError(f"signing_key must be str or bytes, got {type(raw).__name__}")


def load_oidc_config(
    data: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> OIDCConfig:
    """Build an :class:`OIDCConfig` from a plain config mapping.

    Recognized keys (all optional unless ``enabled`` is true):

    * ``display_name`` — optional public plain text, at most 80 characters
    * ``enabled`` — bool
    * ``issuer`` — string (no trailing slash assumed; we don't normalize)
    * ``client_id`` — string
    * ``client_secret`` — string (literal)
    * ``client_secret_env`` — env var name; resolved against ``env``. If
      both are present, ``client_secret`` wins.
    * ``redirect_uri`` — absolute URL
    * ``scopes`` — string ("openid email profile") or list
    * ``allowed_email_domains`` — list of domains (case-insensitive)
    * ``admin_emails`` — list of emails (case-insensitive)
    * ``admin_groups`` — OIDC group names/paths that should grant admin
    * ``admin_roles`` — OIDC realm/client role names that should grant admin
    * ``admin_groups_env`` / ``admin_roles_env`` — env var names with comma-
      separated OIDC group/role names
    * ``signing_key`` — literal
    * ``signing_key_env`` — env var name
    * ``cookie`` — nested mapping for :class:`CookieConfig`
    * ``authorization_endpoint`` / ``token_endpoint`` / ``jwks_uri``
    * ``clock_skew_seconds`` — int
    """
    env_map: Mapping[str, str] = env or {}

    enabled = _coerce_bool(data.get("enabled"), default=False)

    client_secret = data.get("client_secret") or ""
    if not client_secret:
        secret_env = data.get("client_secret_env")
        if secret_env:
            client_secret = env_map.get(str(secret_env), "")

    signing_raw = data.get("signing_key")
    if not signing_raw:
        sk_env = data.get("signing_key_env")
        if sk_env:
            signing_raw = env_map.get(str(sk_env))

    # When disabled, allow missing signing key — but if present, still
    # require sane bytes. This lets ops stage the config block before
    # flipping enabled=true.
    if signing_raw is None and not enabled:
        signing_key = b"\x00" * _MIN_SIGNING_KEY_BYTES
    else:
        signing_key = _coerce_signing_key(signing_raw)

    admin_groups = data.get("admin_groups")
    if not admin_groups:
        groups_env = data.get("admin_groups_env")
        if groups_env:
            admin_groups = env_map.get(str(groups_env), "")
    admin_roles = data.get("admin_roles")
    if not admin_roles:
        roles_env = data.get("admin_roles_env")
        if roles_env:
            admin_roles = env_map.get(str(roles_env), "")

    cookie_data = data.get("cookie") or {}
    if not isinstance(cookie_data, Mapping):
        raise OIDCConfigError("cookie config must be a mapping")
    cookie = CookieConfig(
        name=str(cookie_data.get("name", CookieConfig.name)),
        max_age_seconds=int(cookie_data.get("max_age_seconds", CookieConfig.max_age_seconds)),
        secure=_coerce_bool(cookie_data.get("secure"), default=CookieConfig.secure),
        http_only=_coerce_bool(cookie_data.get("http_only"), default=CookieConfig.http_only),
        same_site=str(cookie_data.get("same_site", CookieConfig.same_site)).lower(),
        path=str(cookie_data.get("path", CookieConfig.path)),
        domain=(str(cookie_data["domain"]) if cookie_data.get("domain") else None),
    )

    return OIDCConfig(
        enabled=enabled,
        display_name=validate_oidc_display_name(data.get("display_name", "")),
        issuer=str(data.get("issuer", "")),
        client_id=str(data.get("client_id", "")),
        client_secret=str(client_secret),
        redirect_uri=str(data.get("redirect_uri", "")),
        scopes=_coerce_scopes(data.get("scopes")),
        allowed_email_domains=_coerce_str_set(data.get("allowed_email_domains")),
        admin_emails=_coerce_str_set(data.get("admin_emails")),
        signing_key=signing_key,
        cookie=cookie,
        authorization_endpoint=str(data.get("authorization_endpoint", "")),
        token_endpoint=str(data.get("token_endpoint", "")),
        jwks_uri=str(data.get("jwks_uri", "")),
        clock_skew_seconds=int(data.get("clock_skew_seconds", 60)),
        admin_groups=_coerce_access_name_set(admin_groups),
        admin_roles=_coerce_access_name_set(admin_roles),
        groups_claim=str(data.get("groups_claim") or "groups"),
    )


# ---------------------------------------------------------------------------
# State / nonce / PKCE primitives.
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def generate_state() -> str:
    """Return a 256-bit URL-safe ``state`` parameter."""
    return _b64url(secrets.token_bytes(32))


def generate_nonce() -> str:
    """Return a 256-bit URL-safe ``nonce`` parameter."""
    return _b64url(secrets.token_bytes(32))


@dataclass(frozen=True)
class PKCEPair:
    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce_pair() -> PKCEPair:
    """Return a fresh ``S256`` PKCE verifier/challenge pair.

    Verifier length is 64 url-safe chars (~384 bits of entropy), well above
    the RFC 7636 minimum of 43.
    """
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCEPair(verifier=verifier, challenge=challenge, method="S256")


@dataclass(frozen=True)
class StateRecord:
    """What gets stashed server-side while the user is at the IdP.

    The store can be anything mapping-like (in-memory dict for single-proc,
    Redis-backed shim for multi-proc). The values here are never sent to
    the browser.
    """

    state: str
    nonce: str
    pkce_verifier: str
    created_at: float
    return_to: str = "/"
    redirect_uri: str = ""
    browser_cookie_name: str = ""
    browser_binding_hash: str = ""


def record_state(
    store: MutableMapping[str, StateRecord],
    *,
    nonce: str,
    pkce_verifier: str,
    return_to: str = "/",
    redirect_uri: str = "",
    browser_cookie_name: str = "",
    browser_binding_hash: str = "",
    state: str | None = None,
    now: float | None = None,
    max_age_seconds: int = 600,
    max_pending: int = 1024,
) -> StateRecord:
    """Prune expired flows, then mint a bounded pending authorization record."""
    current = now if now is not None else time.time()
    for key, pending in tuple(store.items()):
        if current - pending.created_at > max_age_seconds:
            del store[key]
    if max_pending <= 0 or len(store) >= max_pending:
        raise SessionError("too many pending login flows")
    record = StateRecord(
        state=state or generate_state(),
        nonce=nonce,
        pkce_verifier=pkce_verifier,
        created_at=current,
        return_to=return_to,
        redirect_uri=redirect_uri,
        browser_cookie_name=browser_cookie_name,
        browser_binding_hash=browser_binding_hash,
    )
    store[record.state] = record
    return record


def consume_state(
    store: MutableMapping[str, StateRecord],
    state: str,
    *,
    max_age_seconds: int = 600,
    now: float | None = None,
) -> StateRecord:
    """Pop and validate a ``state`` from the store.

    Raises :class:`SessionError` if the state is unknown or expired. The
    record is removed from the store regardless to prevent replay.
    """
    record = store.pop(state, None)
    if record is None:
        raise SessionError("unknown or already-used state")
    current = now if now is not None else time.time()
    if current - record.created_at > max_age_seconds:
        raise SessionError("state expired")
    return record


# ---------------------------------------------------------------------------
# Authorization URL.
# ---------------------------------------------------------------------------


def authorization_url(
    config: OIDCConfig,
    *,
    state: str,
    nonce: str,
    pkce: PKCEPair,
    extra_params: Mapping[str, str] | None = None,
) -> str:
    """Build the IdP authorization URL.

    The caller supplies ``state`` / ``nonce`` / ``pkce`` so they can also
    persist them server-side via :func:`record_state`. ``extra_params`` is
    a small escape hatch for things like ``prompt=login`` or
    ``login_hint=...``.
    """
    if not config.enabled:
        raise OIDCConfigError("OIDC is disabled; cannot build authorization URL")
    if not config.authorization_endpoint:
        raise OIDCConfigError("authorization_endpoint is required")
    if pkce.method != "S256":
        raise OIDCConfigError("only S256 PKCE is supported")

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if extra_params:
        for k, v in extra_params.items():
            # Don't let extras silently override security-relevant params.
            if k in {"state", "nonce", "code_challenge", "code_challenge_method",
                     "response_type", "client_id", "redirect_uri"}:
                continue
            params[k] = str(v)

    separator = "&" if "?" in config.authorization_endpoint else "?"
    return f"{config.authorization_endpoint}{separator}{urlencode(params)}"


# ---------------------------------------------------------------------------
# ID-token claims validation.
# ---------------------------------------------------------------------------


class IDTokenVerifier(Protocol):
    """Adapter boundary for full cryptographic JWT verification.

    Implementations (e.g. an Authlib + JWKS-backed verifier) are wired in
    by ``proxy.py`` and must:

    * fetch the issuer's JWKS,
    * verify the JWT signature against the appropriate key,
    * verify ``iss`` / ``aud`` / signature algorithm,
    * return the decoded claims dict on success,
    * raise on any verification failure.

    Keeping this as a Protocol lets ``admin_oidc`` stay free of any JWT
    library dependency and lets tests pin validation behavior with a
    trivial fake.
    """

    def verify(self, id_token: str) -> Mapping[str, Any]:
        ...


def validate_id_token_claims(
    claims: Mapping[str, Any],
    *,
    config: OIDCConfig,
    expected_nonce: str,
    now: float | None = None,
) -> Mapping[str, Any]:
    """Validate the *shape* of an already-decoded ID-token claims dict.

    Checks:

    * ``iss`` matches ``config.issuer`` exactly,
    * ``aud`` contains ``config.client_id`` (aud may be string or list),
    * ``exp`` is in the future (with ``clock_skew_seconds`` tolerance),
    * ``iat``, if present, is not absurdly in the future,
    * ``nonce`` matches ``expected_nonce``,
    * ``email`` is present and (if domain/admin allow-lists are set)
      is permitted by them.

    Returns the same claims dict on success; raises
    :class:`IDTokenClaimsError` otherwise. Cryptographic signature
    verification is the verifier adapter's job and is *not* duplicated
    here — this function trusts that ``claims`` was produced by a verifier
    that already checked the signature.
    """
    current = now if now is not None else time.time()
    skew = max(config.clock_skew_seconds, 0)

    iss = claims.get("iss")
    if iss != config.issuer:
        raise IDTokenClaimsError(f"issuer mismatch: {iss!r} != {config.issuer!r}")

    aud = claims.get("aud")
    if isinstance(aud, str):
        aud_list = [aud]
    elif isinstance(aud, (list, tuple)):
        aud_list = [str(a) for a in aud]
    else:
        raise IDTokenClaimsError("aud claim missing or wrong type")
    if config.client_id not in aud_list:
        raise IDTokenClaimsError(f"audience does not include client_id {config.client_id!r}")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise IDTokenClaimsError("exp claim missing or not numeric")
    if current - skew > float(exp):
        raise IDTokenClaimsError("id_token expired")

    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and float(iat) - skew > current:
        raise IDTokenClaimsError("id_token iat in the future")

    nonce = claims.get("nonce")
    if not nonce or not hmac.compare_digest(str(nonce), expected_nonce):
        raise IDTokenClaimsError("nonce mismatch")

    email_raw = claims.get("email")
    if not email_raw:
        raise IDTokenClaimsError("email claim required")
    email = str(email_raw).strip().lower()
    if "@" not in email:
        raise IDTokenClaimsError("email claim malformed")

    email_verified = claims.get("email_verified")
    if email_verified is False:
        raise SignInRefused("email not verified by IdP")

    if (
        not _email_allowed(email, config)
        and admin_role_from_oidc_claims(claims, config=config) != AdminRole.ADMIN
    ):
        raise SignInRefused("email not in allow-list")

    return claims


def _email_allowed(email: str, config: OIDCConfig) -> bool:
    """Return True if ``email`` passes the configured allow-lists.

    Rules:

    * If both ``admin_emails`` and ``allowed_email_domains`` are empty,
      *anyone* who passes IdP auth is allowed. This is the "single tenant,
      IdP-restricted to my org" case.
    * Otherwise, the email must either be in ``admin_emails`` or its
      domain must be in ``allowed_email_domains``.
    """
    if not config.admin_emails and not config.allowed_email_domains:
        return True
    if email in config.admin_emails:
        return True
    _, _, domain = email.partition("@")
    return domain in config.allowed_email_domains


def _iter_claim_strings(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value if item is not None)
    return ()


def _claim_values_match(
    value: Any, allowed: frozenset[str], *, groups: bool = False,
) -> bool:
    expected = allowed | {canonical_oidc_group(item) for item in allowed} if groups else allowed
    # Claims are issuer assertions, not operator shorthand. No suffix,
    # case, whitespace or punctuation aliases belong in authorization.
    return any(item in expected for item in _iter_claim_strings(value))


def admin_role_from_oidc_claims(
    claims: Mapping[str, Any],
    *,
    config: OIDCConfig,
) -> str:
    """Return an admin role granted by configured OIDC groups/roles.

    This deliberately grants only ``admin``. ``owner`` remains an explicit
    bootstrap/manual concept, while "trusted users" can be managed in the
    IdP as the normal admin set for this gateway.
    """
    if _claim_values_match(claims.get(config.groups_claim), config.admin_groups, groups=True):
        return AdminRole.ADMIN

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, Mapping) and _claim_values_match(
        realm_access.get("roles"),
        config.admin_roles,
    ):
        return AdminRole.ADMIN

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, Mapping):
        client_access = resource_access.get(config.client_id)
        if isinstance(client_access, Mapping) and _claim_values_match(
            client_access.get("roles"),
            config.admin_roles,
        ):
            return AdminRole.ADMIN

    return AdminRole.NONE


def access_names_from_oidc_claims(
    claims: Mapping[str, Any],
    *,
    client_id: str,
    groups_claim: str = "groups",
) -> dict[str, tuple[str, ...]]:
    """Return exact issuer assertions for runtime Profile authorization mapping."""
    groups: set[str] = set()
    roles: set[str] = set()

    for item in _iter_claim_strings(claims.get(groups_claim)):
        groups.add(item)

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, Mapping):
        for item in _iter_claim_strings(realm_access.get("roles")):
            roles.add(item)

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, Mapping):
        client_access = resource_access.get(client_id)
        if isinstance(client_access, Mapping):
            for item in _iter_claim_strings(client_access.get("roles")):
                roles.add(item)

    return {
        "oidc_group": tuple(sorted(groups)),
        "oidc_role": tuple(sorted(roles)),
    }


# ---------------------------------------------------------------------------
# Signed admin session cookie.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminSession:
    """The minimal payload we trust into a signed cookie.

    Deliberately **does not** include the raw ``id_token`` or
    ``access_token`` — those stay server-side (or are dropped entirely
    after one-shot use). The dashboard only needs to know "who you are"
    and "what you're allowed to do."
    """

    subject: str
    email: str
    role: str
    issued_at: float
    expires_at: float
    session_id: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "email": self.email,
            "role": self.role,
            "iat": int(self.issued_at),
            "exp": int(self.expires_at),
            "sid": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdminSession":
        try:
            session_id = str(data.get("sid") or "")
            if not session_id:
                # Old signed cookies predate sid. Their payload is a stable,
                # session-specific revocation key without invalidating them.
                payload = json.dumps(dict(data), sort_keys=True, separators=(",", ":"))
                session_id = "legacy_" + hashlib.sha256(payload.encode()).hexdigest()
            return cls(
                subject=str(data["sub"]),
                email=str(data["email"]),
                role=str(data["role"]),
                issued_at=float(data["iat"]),
                expires_at=float(data["exp"]),
                session_id=session_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"malformed session payload: {exc}") from exc


# A short version tag so we can rotate the cookie format without silently
# accepting old shapes.
_SESSION_VERSION = "v1"


def session_from_claims(
    claims: Mapping[str, Any],
    *,
    role: str,
    cookie: CookieConfig,
    now: float | None = None,
) -> AdminSession:
    """Build an :class:`AdminSession` from validated ID-token claims.

    Only the subject + email are pulled from the claims. The role is
    decided by the caller (typically via :func:`bootstrap_admin_role`).
    """
    if role not in AdminRole.ALL:
        raise SessionError(f"invalid role {role!r}")
    issued = now if now is not None else time.time()
    return AdminSession(
        subject=str(claims["sub"]),
        email=str(claims["email"]).strip().lower(),
        role=role,
        issued_at=issued,
        expires_at=issued + cookie.max_age_seconds,
        session_id=_b64url(secrets.token_bytes(16)),
    )


def encode_session_cookie(session: AdminSession, *, signing_key: bytes) -> str:
    """Return the signed cookie string for an :class:`AdminSession`.

    Layout: ``<version>.<b64url(payload)>.<b64url(hmac-sha256)>``. The
    payload is the JSON of :meth:`AdminSession.to_dict`. The HMAC covers
    ``<version>.<b64url(payload)>`` so we can change the version field
    without resigning issues.
    """
    if len(signing_key) < _MIN_SIGNING_KEY_BYTES:
        raise SessionError("signing_key too short")
    payload = json.dumps(session.to_dict(), separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64url(payload.encode("utf-8"))
    signing_input = f"{_SESSION_VERSION}.{payload_b64}".encode("ascii")
    mac = hmac.new(signing_key, signing_input, hashlib.sha256).digest()
    return f"{_SESSION_VERSION}.{payload_b64}.{_b64url(mac)}"


def csrf_token_for_session(session: AdminSession, *, signing_key: bytes) -> str:
    """Return the same-origin mutation token for a signed browser session."""
    if len(signing_key) < _MIN_SIGNING_KEY_BYTES:
        raise SessionError("signing_key too short")
    session_nonce = session.session_id or (
        f"{session.subject}:{int(session.issued_at)}:{int(session.expires_at)}"
    )
    mac = hmac.new(
        signing_key,
        f"csrf:{session_nonce}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url(mac)


def decode_session_cookie(
    token: str,
    *,
    signing_key: bytes,
    now: float | None = None,
) -> AdminSession:
    """Verify HMAC, expiry, and version; return the :class:`AdminSession`.

    Raises :class:`SessionError` for any tampering, wrong version, bad
    encoding, or expiry.
    """
    if not token or token.count(".") != 2:
        raise SessionError("malformed cookie")
    version, payload_b64, mac_b64 = token.split(".", 2)
    if version != _SESSION_VERSION:
        raise SessionError(f"unsupported cookie version {version!r}")

    signing_input = f"{version}.{payload_b64}".encode("ascii")
    expected_mac = hmac.new(signing_key, signing_input, hashlib.sha256).digest()
    try:
        provided_mac = _b64url_decode(mac_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise SessionError("bad signature encoding") from exc
    if not hmac.compare_digest(expected_mac, provided_mac):
        raise SessionError("signature mismatch")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionError("payload decode failed") from exc
    if not isinstance(payload, Mapping):
        raise SessionError("payload not an object")

    session = AdminSession.from_dict(payload)
    if session.is_expired(now=now):
        raise SessionError("session expired")
    return session


# ---------------------------------------------------------------------------
# Role bootstrap.
# ---------------------------------------------------------------------------


def bootstrap_admin_role(
    email: str,
    *,
    config: OIDCConfig,
    existing_roles: Mapping[str, str],
    issuer: str = "",
    email_verified: bool = False,
) -> str:
    """Seed only the first configured, verified owner under the trusted issuer.

    ``existing_roles`` maps grant ids to role names. Persisted grants are
    presence-only here. Returning users recover their grants by immutable
    (issuer, subject) at the caller, never by email. Once an owner grant exists
    (even a revoked one), an email cannot create or transfer ownership. An
    administrator grant does not count: a group administrator who signs in
    first must not lock the listed owner out.
    """
    if AdminRole.OWNER in set(existing_roles.values()) or issuer != config.issuer or email_verified is not True:
        return AdminRole.NONE
    if email.strip().lower() in config.admin_emails:
        return AdminRole.OWNER
    return AdminRole.NONE
