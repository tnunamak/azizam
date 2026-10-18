"""OAuth 2.1 authorization server for the MCP endpoint.

MCP clients that cannot send a header (ChatGPT, Claude.ai) and clients that
prefer to sign in (Claude Code, Cursor, VS Code, Goose, Zed, Inspector, ...)
follow the MCP authorization spec: a 401 names the protected-resource metadata
(gateway/mcp_server.py), that names this server, and this server's RFC 8414
metadata names its endpoints. Nothing here knows any client by name.

What is implemented, and only what is advertised:

* Authorization code grant with PKCE, S256 only (OAuth 2.1). Refresh tokens
  rotate on every use; a reused refresh token or authorization code ends the
  whole connection (RFC 9700 4.14).
* Client identity: Client ID Metadata Documents (an ``https`` client_id whose
  document is fetched through the SSRF-guarded fetcher) and Dynamic Client
  Registration (RFC 7591, deprecated by MCP but still what many clients use).
* Resource indicators (RFC 8707): every code and token is bound to the
  canonical ``/mcp`` URL and is refused anywhere else.
* RFC 9207 ``iss`` on every authorization response, success or error.
* Token revocation (RFC 7009), and revocation by the person in Azizam's UI.

The person signs in with Azizam's own browser sign-in and approves the client on
one consent page (the web app's ``/authorize/`` page). A token then acts as that
person through their signed-in identity, so their *current* access plan applies
to every call and removing the person or changing the plan takes effect at
once. Approval never raises a plan. Tokens are opaque random strings stored
only as hashes (gateway/mcp_oauth_store.py).

Authlib provides the protocol machinery: grant validation, client
authentication, PKCE verification, RFC 7591 metadata validation and RFC 7009
revocation. Its authorization server is framework-neutral; this module adapts
it to FastAPI by building Authlib request objects from parsed parameters and
turning its ``(status, body, headers)`` answers into responses.
"""
from __future__ import annotations

import asyncio
import hmac
import html
import ipaddress
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from authlib.common.security import is_secure_transport
from authlib.common.urls import add_params_to_uri
from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import (
    AuthorizationCodeMixin,
    ClientMixin,
    InvalidGrantError,
    InvalidRequestError,
    JsonPayload,
    JsonRequest,
    OAuth2Error,
    OAuth2Payload,
    OAuth2Request,
    TokenMixin,
)
from authlib.oauth2.rfc6749 import grants
from authlib.oauth2.rfc6749.util import scope_to_list
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7591 import (
    ClientMetadataClaims,
    ClientRegistrationEndpoint,
    InvalidClientMetadataError,
    InvalidRedirectURIError,
)
from authlib.oauth2.rfc7636 import CodeChallenge
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from gateway import mcp_oauth_store
from gateway.mcp_oauth_store import ClientRecord, GrantRecord, Store, digest, new_secret

SCOPE = "mcp"
OFFLINE_ACCESS = "offline_access"
SCOPES_SUPPORTED = (SCOPE, OFFLINE_ACCESS)
ACCESS_TOKEN_PREFIX = "azm_at_"
REFRESH_TOKEN_PREFIX = "azm_rt_"
CLIENT_ID_PREFIX = "azm_client_"
BINDING_COOKIE = "azizam_mcp_authorize"
CONSENT_PAGE = "/authorize/"
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")
GRANT_TYPES = ("authorization_code", "refresh_token")
# A client metadata document is a few hundred bytes; refuse anything large.
CLIENT_DOCUMENT_MAX_BYTES = 64 * 1024
CLIENT_DOCUMENT_TIMEOUT_SECONDS = 10
CLIENT_DOCUMENT_DEFAULT_TTL = 3600
CLIENT_DOCUMENT_MAX_TTL = 24 * 3600
CLIENT_DOCUMENT_MIN_TTL = 300
REGISTRATION_MAX_BYTES = 16 * 1024
# Schemes a native app may not use for its redirect (RFC 8252 7.1 lets apps use
# their own private-use schemes; these would run code or read local data).
_FORBIDDEN_REDIRECT_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "about", "blob", "filesystem"})


@dataclass(frozen=True)
class Settings:
    access_token_seconds: int = 3600
    refresh_idle_seconds: int = 30 * 24 * 3600
    refresh_max_seconds: int = 90 * 24 * 3600
    code_seconds: int = 60
    request_seconds: int = 600
    registration_enabled: bool = True

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> "Settings":
        raw = raw or {}
        defaults = cls()
        def seconds(key: str) -> int:
            value = raw.get(key, getattr(defaults, key))
            return max(1, int(value))
        return cls(
            access_token_seconds=seconds("access_token_seconds"),
            refresh_idle_seconds=seconds("refresh_idle_seconds"),
            refresh_max_seconds=seconds("refresh_max_seconds"),
            code_seconds=seconds("code_seconds"),
            request_seconds=seconds("request_seconds"),
            registration_enabled=bool(raw.get("dynamic_client_registration", True)),
        )


@dataclass(frozen=True)
class Connection:
    """What a valid access token proves: this person approved this client."""
    grant_id: str
    person_id: str
    client_id: str
    client_name: str
    scope: str


@dataclass
class McpOAuthDeps:
    public_base_url: Callable[[Request], str | None]
    # Browser sign-in: the signed-in session or None, and the person it is.
    session: Callable[[Request], Any]
    person: Callable[[Any], Mapping[str, Any] | None]
    # CSRF and same-site action check for a state-changing browser call.
    check_action: Callable[[Request, Any], Response | None]
    # True when the person may act through Azizam now (active, with a plan).
    person_can_connect: Callable[[str], bool]
    # {"name", "description"} of the person's current access plan, for consent.
    plan: Callable[[str], Mapping[str, Any]]
    client_ip: Callable[[Request], str | None]
    fetch_client_document: Callable[[str], Awaitable[tuple[bytes, Mapping[str, str]]]]
    store: Store
    settings: Callable[[], Settings] = Settings
    audit: Callable[..., None] = lambda *args, **kwargs: None
    mcp_path: str = "/mcp"


class ClientDocumentError(ValueError):
    """A Client ID Metadata Document could not be fetched or is not acceptable."""


# --------------------------------------------------------------- utilities


def canonical_resource(value: str) -> str:
    """RFC 8707 comparison form: lowercase scheme and host, no trailing slash."""
    parts = urlsplit(value.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redirect_uri_allowed(uri: str) -> bool:
    """Hosted clients use https; native clients use loopback http (RFC 8252
    7.3) or their own private-use scheme (7.1). No fragments, no userinfo."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if not parts.scheme or parts.fragment or "@" in parts.netloc or any(ord(c) < 33 for c in uri):
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return bool(parts.hostname)
    if scheme == "http":
        return _is_loopback_host(parts.hostname)
    return scheme not in _FORBIDDEN_REDIRECT_SCHEMES


def redirect_uri_matches(registered: str, presented: str) -> bool:
    """Exact match, except that a loopback http redirect may use any port
    (RFC 8252 7.3), because native apps pick a free port per sign-in."""
    if registered == presented:
        return True
    try:
        a, b = urlsplit(registered), urlsplit(presented)
    except ValueError:
        return False
    return (a.scheme == b.scheme == "http" and _is_loopback_host(a.hostname)
            and a.hostname == b.hostname and a.path == b.path and a.query == b.query and not b.fragment)


def is_client_document_url(client_id: str) -> bool:
    """CIMD client ids are https URLs with a path (draft-ietf-oauth-client-id-metadata-document)."""
    try:
        parts = urlsplit(client_id)
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and parts.path not in ("", "/")
            and not parts.fragment and parts.username is None and parts.password is None
            and not any(segment in (".", "..") for segment in parts.path.split("/")))


def _cache_seconds(headers: Mapping[str, str]) -> int:
    control = {k.lower(): v for k, v in headers.items()}.get("cache-control", "")
    directives = {part.strip().split("=", 1)[0].lower(): part.strip().split("=", 1)[-1]
                  for part in control.split(",") if part.strip()}
    if "no-store" in directives or "no-cache" in directives:
        return CLIENT_DOCUMENT_MIN_TTL
    try:
        ttl = int(directives.get("max-age", CLIENT_DOCUMENT_DEFAULT_TTL))
    except ValueError:
        ttl = CLIENT_DOCUMENT_DEFAULT_TTL
    return max(CLIENT_DOCUMENT_MIN_TTL, min(CLIENT_DOCUMENT_MAX_TTL, ttl))


def parse_client_document(client_id: str, raw: bytes) -> dict[str, Any]:
    """Validate a Client ID Metadata Document; returns the fields we keep."""
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ClientDocumentError("The app's metadata document is not JSON.") from None
    if not isinstance(document, Mapping):
        raise ClientDocumentError("The app's metadata document is not a JSON object.")
    if document.get("client_id") != client_id:
        raise ClientDocumentError("The app's metadata document names a different client_id.")
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        raise ClientDocumentError("The app's metadata document has no client_name.")
    redirects = document.get("redirect_uris")
    if (not isinstance(redirects, list) or not redirects or len(redirects) > 50
            or not all(isinstance(uri, str) and redirect_uri_allowed(uri) for uri in redirects)):
        raise ClientDocumentError("The app's metadata document has no usable redirect_uris.")
    methods = document.get("token_endpoint_auth_methods_supported")
    method = document.get("token_endpoint_auth_method", "none")
    # Azizam authenticates metadata-document clients as public clients. A
    # document that lists "none" among its methods (ChatGPT's does) is fine.
    if method != "none" and not (isinstance(methods, list) and "none" in methods):
        raise ClientDocumentError("The app requires private_key_jwt client authentication, which Azizam does not offer.")
    grant_types = document.get("grant_types") or ["authorization_code"]
    response_types = document.get("response_types") or ["code"]
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise ClientDocumentError("The app's metadata document does not allow the authorization code grant.")
    if not isinstance(response_types, list) or "code" not in response_types:
        raise ClientDocumentError("The app's metadata document does not allow response_type=code.")
    return {
        "name": name.strip()[:120],
        "redirect_uris": redirects,
        "grant_types": [g for g in grant_types if g in GRANT_TYPES],
        "metadata": {key: document[key] for key in ("client_uri", "software_id", "software_version")
                     if isinstance(document.get(key), str)},
    }


class _RateLimit:
    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str | None) -> bool:
        now = time.monotonic()
        hits = self.hits[key or "?"]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self.hits) > 10000:
            self.hits.clear()
        return True


# ------------------------------------------------------- Authlib models


@dataclass
class Client(ClientMixin):
    record: ClientRecord

    @property
    def client_id(self) -> str:
        return self.record.client_id

    def get_client_id(self) -> str:
        return self.record.client_id

    def get_default_redirect_uri(self) -> str | None:
        # OAuth 2.1 requires redirect_uri unless exactly one is registered.
        uris = self.record.redirect_uris
        return uris[0] if len(uris) == 1 else None

    def get_allowed_scope(self, scope: str | None) -> str:
        # Unknown scopes are dropped, not refused: a client asking for
        # "openid profile" still gets a working MCP grant (RFC 6749 3.3).
        asked = set(scope_to_list(scope) or [])
        return " ".join([SCOPE] + ([OFFLINE_ACCESS] if OFFLINE_ACCESS in asked else []))

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        return any(redirect_uri_matches(uri, redirect_uri) for uri in self.record.redirect_uris)

    def check_client_secret(self, client_secret: str) -> bool:
        stored = self.record.secret_hash
        return bool(stored) and hmac.compare_digest(stored, digest(client_secret))

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        return method == self.record.auth_method

    def check_response_type(self, response_type: str) -> bool:
        return response_type == "code"

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type in self.record.grant_types


@dataclass
class Person:
    id: str
    grant_id: str

    def get_user_id(self) -> str:
        return self.id


@dataclass
class Code(AuthorizationCodeMixin):
    record: mcp_oauth_store.CodeRecord
    person_id: str

    def get_redirect_uri(self) -> str:
        return self.record.redirect_uri

    def get_scope(self) -> str:
        return self.record.scope

    @property
    def code_challenge(self) -> str:
        return self.record.code_challenge

    @property
    def code_challenge_method(self) -> str:
        return self.record.code_challenge_method


@dataclass
class Token(TokenMixin):
    record: mcp_oauth_store.TokenRecord
    grant: GrantRecord

    def check_client(self, client: Client) -> bool:
        return self.record.client_id == client.get_client_id()

    def get_scope(self) -> str:
        return self.record.scope

    def get_expires_in(self) -> int:
        return self.record.expires_at - self.record.issued_at

    def is_expired(self) -> bool:
        return self.record.expires_at <= time.time()

    def is_revoked(self) -> bool:
        return self.grant.revoked_at is not None


class _Payload(OAuth2Payload):
    def __init__(self, items: Iterable[tuple[str, str]]) -> None:
        self._items = list(items)
        self._data: dict[str, str] = {}
        self._datalist: dict[str, list[str]] = defaultdict(list)
        for key, value in self._items:
            self._data.setdefault(key, value)
            self._datalist[key].append(value)

    @property
    def data(self) -> dict[str, str]:
        return self._data

    @property
    def datalist(self) -> dict[str, list[str]]:
        return self._datalist


class _Request(OAuth2Request):
    """An Authlib request built from parameters FastAPI already parsed."""

    def __init__(self, method: str, uri: str, headers: Mapping[str, str], items: Iterable[tuple[str, str]],
                 *, resource: str) -> None:
        super().__init__(method, uri, headers=headers)
        self.payload = _Payload(items)
        self.resource = resource  # the canonical MCP URL this server issues tokens for

    @property
    def args(self) -> dict[str, str]:
        return self.payload.data

    @property
    def form(self) -> dict[str, str]:
        return self.payload.data


class _JsonPayload(JsonPayload):
    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data


class _JsonRequest(JsonRequest):
    def __init__(self, method: str, uri: str, headers: Mapping[str, str], data: Mapping[str, Any]) -> None:
        super().__init__(method, uri, headers=headers)
        self.payload = _JsonPayload(data)


# --------------------------------------------------------- Authlib parts


class _S256Required(CodeChallenge):
    """PKCE on every authorization request, S256 only (OAuth 2.1, MCP)."""

    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    DEFAULT_CODE_CHALLENGE_METHOD = "S256"

    def validate_code_challenge(self, grant: Any, redirect_uri: str) -> None:
        data = grant.request.payload.data
        if not data.get("code_challenge"):
            raise InvalidRequestError("PKCE is required: send code_challenge with code_challenge_method=S256",
                                      redirect_uri=redirect_uri)
        if data.get("code_challenge_method") != "S256":
            raise InvalidRequestError("code_challenge_method must be S256", redirect_uri=redirect_uri)
        super().validate_code_challenge(grant, redirect_uri)


def _check_resource(request: _Request, bound: str) -> None:
    asked = request.payload.datalist.get("resource") or []
    if len(asked) > 1 or (asked and canonical_resource(asked[0]) != canonical_resource(bound)):
        error = InvalidRequestError("The resource does not match this grant")
        error.error = "invalid_target"
        raise error


class _CodeGrant(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = list(AUTH_METHODS)

    @property
    def oauth(self) -> "McpOAuth":
        return self.server.oauth

    def save_authorization_code(self, code: str, request: _Request) -> None:
        data = request.payload.data
        self.oauth.deps.store.save_code(
            code, grant_id=request.user.grant_id, client_id=request.client.get_client_id(),
            # Empty when the request named none: then the token request need
            # not either (RFC 6749 4.1.3).
            redirect_uri=data.get("redirect_uri") or "",
            scope=request.scope, resource=request.resource,
            code_challenge=data["code_challenge"], code_challenge_method=data["code_challenge_method"],
            ttl=self.oauth.settings().code_seconds,
        )

    def query_authorization_code(self, code: str, client: Client) -> Code | None:
        store = self.oauth.deps.store
        record = store.code(code)
        if record is None or record.client_id != client.get_client_id():
            return None
        if record.used_at is not None:
            # A code used twice was stolen or replayed: end the connection
            # (RFC 6749 4.1.2, RFC 9700 4.2.4).
            store.revoke_grant(record.grant_id, "authorization_code_reuse")
            return None
        grant = store.active_grant(record.grant_id)
        if record.expires_at <= store.now() or grant is None:
            return None
        return Code(record, grant.person_id)

    def delete_authorization_code(self, authorization_code: Code) -> None:
        pass  # spent atomically in create_token_response

    def authenticate_user(self, authorization_code: Code) -> Person:
        return Person(authorization_code.person_id, authorization_code.record.grant_id)

    def validate_token_request(self) -> None:
        super().validate_token_request()
        _check_resource(self.request, self.request.authorization_code.record.resource)

    def create_token_response(self):  # type: ignore[override]
        if not self.oauth.deps.store.spend_code(self.request.authorization_code.record.code_hash):
            raise InvalidGrantError("The authorization code was already used or has expired")
        return super().create_token_response()


class _RefreshGrant(grants.RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = list(AUTH_METHODS)
    INCLUDE_NEW_REFRESH_TOKEN = True

    @property
    def oauth(self) -> "McpOAuth":
        return self.server.oauth

    def authenticate_refresh_token(self, refresh_token: str) -> Token | None:
        store = self.oauth.deps.store
        record = store.token(refresh_token)
        if record is None or record.kind != "refresh":
            return None
        if record.used_at is not None:
            # Rotation means each refresh token works once. A second use is a
            # stolen copy or a replay: end the connection.
            store.revoke_grant(record.grant_id, "refresh_token_reuse")
            return None
        grant = store.active_grant(record.grant_id)
        if grant is None or record.expires_at <= store.now():
            return None
        return Token(record, grant)

    def authenticate_user(self, refresh_token: Token) -> Person:
        if not self.oauth.deps.person_can_connect(refresh_token.grant.person_id):
            raise InvalidGrantError("This account can no longer use Azizam")
        return Person(refresh_token.grant.person_id, refresh_token.grant.id)

    def revoke_old_credential(self, refresh_token: Token) -> None:
        pass  # spent atomically in create_token_response

    def validate_token_request(self) -> None:
        super().validate_token_request()
        _check_resource(self.request, self.request.refresh_token.grant.resource)

    def create_token_response(self):  # type: ignore[override]
        if not self.oauth.deps.store.spend_refresh_token(self.request.refresh_token.record.token_hash):
            self.oauth.deps.store.revoke_grant(self.request.refresh_token.grant.id, "refresh_token_reuse")
            raise InvalidGrantError("The refresh token was already used")
        return super().create_token_response()


class _Revocation(RevocationEndpoint):
    CLIENT_AUTH_METHODS = list(AUTH_METHODS)

    def query_token(self, token_string: str, token_type_hint: str | None) -> Token | None:
        store = self.server.oauth.deps.store
        record = store.token(token_string)
        grant = store.grant(record.grant_id) if record else None
        return Token(record, grant) if record and grant else None

    def revoke_token(self, token: Token, request: Any) -> None:
        # Revoking either token ends the connection it belongs to (RFC 7009 2.1).
        self.server.oauth.deps.store.revoke_grant(token.grant.id, "client_revoked")


class _ClientMetadata(ClientMetadataClaims):
    def validate_redirect_uris(self) -> None:
        # Authlib accepts only web URLs; native apps may use their own scheme
        # (RFC 8252 7.1). save_client applies redirect_uri_allowed instead.
        pass


class _Registration(ClientRegistrationEndpoint):
    def __init__(self, server: Any = None) -> None:
        super().__init__(server, claims_classes=[_ClientMetadata])

    def authenticate_token(self, request: Any) -> bool:
        return True  # open registration (RFC 7591 3); a registration grants no access

    def get_server_metadata(self) -> dict[str, Any]:
        return {
            "grant_types_supported": list(GRANT_TYPES),
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "scopes_supported": list(SCOPES_SUPPORTED),
        }

    def generate_client_id(self, request: Any) -> str:
        return new_secret(CLIENT_ID_PREFIX, 16)

    def generate_client_info(self, request: Any) -> dict[str, Any]:
        info = super().generate_client_info(request)
        if request.payload.data.get("token_endpoint_auth_method") == "none":
            info.pop("client_secret", None)
            info.pop("client_secret_expires_at", None)
        return info

    def save_client(self, client_info: Mapping[str, Any], client_metadata: Mapping[str, Any], request: Any) -> Client:
        redirects = client_metadata.get("redirect_uris") or []
        if not redirects or len(redirects) > 50 or not all(redirect_uri_allowed(uri) for uri in redirects):
            raise InvalidRedirectURIError("Each redirect URI must be https, loopback http, or an app's own scheme")
        grant_types = list(client_metadata.get("grant_types") or ["authorization_code"])
        if "authorization_code" not in grant_types:
            raise InvalidClientMetadataError("grant_types must include authorization_code")
        if any(rt != "code" for rt in client_metadata.get("response_types") or ["code"]):
            raise InvalidClientMetadataError("response_types must be [\"code\"]")
        secret = client_info.get("client_secret")
        name = str(client_metadata.get("client_name") or "").strip()[:120] or "Unnamed app"
        record = self.server.oauth.deps.store.put_client(
            client_id=client_info["client_id"], kind="registered", name=name, redirect_uris=list(redirects),
            grant_types=[g for g in grant_types if g in GRANT_TYPES],
            auth_method=client_metadata.get("token_endpoint_auth_method", "client_secret_basic"),
            secret_hash=digest(secret) if secret else None,
            metadata={key: client_metadata[key] for key in ("client_uri", "software_id", "software_version")
                      if isinstance(client_metadata.get(key), str)},
        )
        return Client(record)


class _Server(AuthorizationServer):
    def __init__(self, oauth: "McpOAuth") -> None:
        super().__init__(scopes_supported=list(SCOPES_SUPPORTED))
        self.oauth = oauth
        self.register_token_generator("default", self._generate_token)
        self.register_grant(_CodeGrant, [_S256Required(required=True)])
        self.register_grant(_RefreshGrant)
        self.register_endpoint(_Revocation)
        self.register_endpoint(_Registration)

    def query_client(self, client_id: str) -> Client | None:
        record = self.oauth.deps.store.client(client_id) if client_id else None
        return Client(record) if record else None

    def save_token(self, token: Mapping[str, Any], request: _Request) -> None:
        store, settings = self.oauth.deps.store, self.oauth.settings()
        grant = store.grant(request.user.grant_id)
        assert grant is not None
        client_id = request.client.get_client_id()
        store.save_token(token["access_token"], kind="access", grant_id=grant.id, client_id=client_id,
                         scope=token["scope"], ttl=settings.access_token_seconds)
        if token.get("refresh_token"):
            # Idle expiry, capped by the connection's absolute lifetime.
            ttl = min(settings.refresh_idle_seconds, grant.created_at + settings.refresh_max_seconds - store.now())
            store.save_token(token["refresh_token"], kind="refresh", grant_id=grant.id, client_id=client_id,
                             scope=token["scope"], ttl=max(1, ttl))

    def _generate_token(self, grant_type: str, client: Client, user: Any = None, scope: str | None = None,
                        expires_in: int | None = None, include_refresh_token: bool = True) -> dict[str, Any]:
        token: dict[str, Any] = {
            "token_type": "Bearer",
            "access_token": new_secret(ACCESS_TOKEN_PREFIX),
            "expires_in": self.oauth.settings().access_token_seconds,
            "scope": scope or SCOPE,
        }
        if include_refresh_token:
            token["refresh_token"] = new_secret(REFRESH_TOKEN_PREFIX, 48)
        return token

    def validate_requested_scope(self, scope: str | None) -> None:
        pass  # Client.get_allowed_scope narrows instead of refusing

    def create_oauth2_request(self, request: Any) -> Any:
        return request

    def create_json_request(self, request: Any) -> Any:
        return request

    def handle_response(self, status: int, body: Any, headers: Any) -> tuple[int, Any, list[tuple[str, str]]]:
        return status, body, list(headers or [])

    def send_signal(self, name: str, *args: Any, **kwargs: Any) -> None:
        pass


# ---------------------------------------------------------- the server


class McpOAuth:
    def __init__(self, deps: McpOAuthDeps) -> None:
        self.deps = deps
        self.server = _Server(self)
        self.authorize_limit = _RateLimit(60, 60)
        self.token_limit = _RateLimit(120, 60)
        self.register_limit = _RateLimit(20, 3600)
        self._pruned_at = 0.0

    def settings(self) -> Settings:
        return self.deps.settings()

    # ---- addresses

    def issuer(self, request: Request) -> str:
        return (self.deps.public_base_url(request) or "").rstrip("/")

    def resource(self, request: Request) -> str:
        return self.issuer(request) + self.deps.mcp_path

    def available(self, request: Request) -> bool:
        """OAuth 2.1 needs TLS, except on loopback. Without a usable public
        address the gateway does not advertise OAuth; API keys still work."""
        issuer = self.issuer(request)
        return bool(issuer) and is_secure_transport(issuer + "/")

    def metadata(self, request: Request) -> dict[str, Any]:
        issuer = self.issuer(request)
        document: dict[str, Any] = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": list(GRANT_TYPES),
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "scopes_supported": list(SCOPES_SUPPORTED),
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "service_documentation": f"{issuer}/llms.txt",
        }
        if self.settings().registration_enabled:
            document["registration_endpoint"] = f"{issuer}/oauth/register"
        return document

    # ---- resource server side

    def verify_access_token(self, token: str, resource: str | None) -> Connection | None:
        """The connection behind a valid, unexpired access token for
        ``resource`` whose grant is still in force; otherwise None. ``None``
        skips the resource comparison for a call the MCP endpoint, which
        already compared it, makes in process."""
        store = self.deps.store
        record = store.token(token)
        if record is None or record.kind != "access" or record.expires_at <= store.now():
            return None
        grant = store.active_grant(record.grant_id)
        if grant is None or (resource is not None
                             and canonical_resource(grant.resource) != canonical_resource(resource)):
            return None
        if SCOPE not in scope_to_list(record.scope):
            return None
        store.touch_grant(grant)
        return Connection(grant.id, grant.person_id, grant.client_id, grant.client_name, record.scope)

    def connection_active(self, grant_id: str, person_id: str) -> bool:
        """Whether a connection's grant is still in force for ``person_id``.

        Work a connection started (a queued media job) holds no access token,
        so it checks the grant itself: the same grant and scope test
        ``verify_access_token`` applies. Revoking the connection stops it.
        """
        grant = self.deps.store.active_grant(grant_id)
        return grant is not None and grant.person_id == person_id and SCOPE in scope_to_list(grant.scope)

    # ---- helpers

    def _maybe_prune(self) -> None:
        if time.monotonic() - self._pruned_at > 600:
            self._pruned_at = time.monotonic()
            try:
                self.deps.store.prune()
            except Exception as exc:  # noqa: BLE001 - housekeeping must not fail a sign-in
                print(f"[mcp-oauth] prune failed: {type(exc).__name__}", flush=True)

    async def ensure_client(self, client_id: str | None) -> None:
        """Load a metadata-document client into the store, fetching its
        document when the cached copy is stale."""
        if not client_id or not is_client_document_url(client_id):
            return
        store = self.deps.store
        cached = await asyncio.to_thread(store.client, client_id)
        if cached is not None and cached.kind == "cimd" and (cached.fresh_until or 0) > store.now():
            return
        try:
            raw, headers = await self.deps.fetch_client_document(client_id)
            parsed = parse_client_document(client_id, raw)
        except ClientDocumentError:
            raise
        except Exception as exc:  # noqa: BLE001 - any fetch failure is the same answer
            if cached is not None and cached.kind == "cimd" and store.now() - cached.refreshed_at < CLIENT_DOCUMENT_MAX_TTL:
                return  # a recent copy carries a brief outage
            raise ClientDocumentError("Azizam could not fetch the app's metadata document.") from exc
        if cached is not None and cached.kind != "cimd":
            raise ClientDocumentError("This client_id is already registered differently.")
        await asyncio.to_thread(
            store.put_client, client_id=client_id, kind="cimd", name=parsed["name"],
            redirect_uris=parsed["redirect_uris"], grant_types=parsed["grant_types"], auth_method="none",
            secret_hash=None, metadata=parsed["metadata"], fresh_until=store.now() + _cache_seconds(headers),
        )

    def _request(self, request: Request, path: str, items: Iterable[tuple[str, str]]) -> _Request:
        return _Request(request.method, self.issuer(request) + path, request.headers, items,
                        resource=self.resource(request))

    def _with_iss(self, location: str, issuer: str) -> str:
        return add_params_to_uri(location, [("iss", issuer)])

    def _error_redirect(self, error: OAuth2Error, state: str | None, issuer: str) -> str:
        params = [("error", error.error)]
        if error.description:
            params.append(("error_description", error.description))
        if state:
            params.append(("state", state))
        params.append(("iss", issuer))
        return add_params_to_uri(error.redirect_uri, params)

    # ---- authorization endpoint (sync parts run in a thread)

    def _start(self, oauth_request: _Request, issuer: str) -> tuple[str, str, str]:
        """Validate a fresh authorization request. Returns ("consent", request
        id, binding), ("redirect", url, "") or ("error", message, "")."""
        state = oauth_request.payload.state
        try:
            grant = self.server.get_consent_grant(oauth_request)
        except OAuth2Error as error:
            if error.redirect_uri:
                return "redirect", self._error_redirect(error, state, issuer), ""
            return "error", error.description or error.error, ""
        try:
            _check_resource(oauth_request, oauth_request.resource)
        except OAuth2Error as error:
            error.redirect_uri = grant.redirect_uri
            return "redirect", self._error_redirect(error, state, issuer), ""
        request_id, binding = new_secret(), new_secret()
        self.deps.store.save_request(request_id, binding, oauth_request.payload.client_id,
                                     list(oauth_request.payload._items), self.settings().request_seconds)
        return "consent", request_id, binding

    def _decide(self, oauth_request: _Request, issuer: str, person_id: str | None) -> tuple[int, dict[str, Any]]:
        """Finish a consented request: a code (person approved) or access_denied."""
        state = oauth_request.payload.state
        try:
            grant = self.server.get_consent_grant(oauth_request)
        except OAuth2Error as error:
            if error.redirect_uri:
                return 200, {"redirect": self._error_redirect(error, state, issuer)}
            return 400, {"error": {"code": "invalid_request", "message": error.description or error.error}}
        user = None
        if person_id is not None:
            client = oauth_request.client
            record = self.deps.store.create_grant(
                person_id=person_id, client=client.record, redirect_uri=grant.redirect_uri,
                resource=oauth_request.resource, scope=oauth_request.scope,
            )
            user = Person(person_id, record.id)
        status, _body, headers = self.server.create_authorization_response(oauth_request, grant_user=user, grant=grant)
        location = dict(headers).get("Location")
        if not location:
            return 400, {"error": {"code": "invalid_request", "message": "The request could not be completed."}}
        return 200, {"redirect": self._with_iss(location, issuer)}

    # ---- routes

    def register(self, app: FastAPI) -> None:
        cors = {"access-control-allow-origin": "*"}
        no_store = {"cache-control": "no-store", "pragma": "no-cache"}

        def preflight(_request: Request) -> Response:
            return Response(status_code=204, headers={
                **cors, "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "authorization, content-type, mcp-protocol-version",
                "access-control-max-age": "600",
            })

        def oauth_json(status: int, body: Any, headers: Iterable[tuple[str, str]] = ()) -> JSONResponse:
            merged = {name.lower(): value for name, value in headers if name.lower() != "content-type"}
            return JSONResponse(body if body != "" else {}, status_code=status, headers={**merged, **cors, **no_store})

        def error_page(message: str, status: int = 400) -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>Can't connect this app</title>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<main style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
                "<h1>Can't connect this app</h1>"
                f"<p>{html.escape(message)}</p>"
                "<p>Go back to the app and start connecting again.</p></main>",
                status_code=status,
                headers={**no_store, "x-frame-options": "DENY", "referrer-policy": "no-referrer",
                         "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"},
            )

        def unavailable() -> JSONResponse:
            return oauth_json(404, {"error": "invalid_request",
                                    "error_description": "Sign-in needs this gateway's public https address"})

        async def as_metadata(request: Request) -> JSONResponse:
            if not self.available(request):
                return unavailable()
            return JSONResponse(self.metadata(request), headers={**cors, "cache-control": "public, max-age=300"})

        app.add_api_route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"], include_in_schema=False)
        for path in ("/.well-known/oauth-authorization-server", "/oauth/token", "/oauth/register", "/oauth/revoke"):
            app.add_api_route(path, preflight, methods=["OPTIONS"], include_in_schema=False)

        @app.get("/oauth/authorize", include_in_schema=False)
        async def authorize(request: Request) -> Response:
            if not self.available(request):
                return error_page("Sign-in needs this gateway's public https address. Ask the owner to set it.", 404)
            if not self.authorize_limit.allow(self.deps.client_ip(request)):
                return error_page("Too many sign-in attempts. Wait a minute and try again.", 429)
            await asyncio.to_thread(self._maybe_prune)
            items = list(request.query_params.multi_items())
            client_id = dict(items).get("client_id")
            try:
                await self.ensure_client(client_id)
            except ClientDocumentError as exc:
                return error_page(str(exc))
            issuer = self.issuer(request)
            kind, value, binding = await asyncio.to_thread(
                self._start, self._request(request, "/oauth/authorize", items), issuer)
            if kind == "error":
                return error_page(value)
            if kind == "redirect":
                return RedirectResponse(value, status_code=302, headers=no_store)
            response = RedirectResponse(f"{CONSENT_PAGE}?{urlencode({'request': value})}", status_code=302,
                                        headers=no_store)
            response.set_cookie(BINDING_COOKIE, binding, max_age=self.settings().request_seconds, httponly=True,
                                samesite="lax", secure=issuer.startswith("https://"), path="/")
            return response

        @app.post("/oauth/token", include_in_schema=False)
        async def token(request: Request) -> Response:
            if not self.available(request):
                return unavailable()
            if not self.token_limit.allow(self.deps.client_ip(request)):
                return oauth_json(429, {"error": "slow_down", "error_description": "Too many token requests"})
            form = await request.form()
            items = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
            try:
                await self.ensure_client(dict(items).get("client_id"))
            except ClientDocumentError as exc:
                return oauth_json(400, {"error": "invalid_client", "error_description": str(exc)})
            status, body, headers = await asyncio.to_thread(
                self.server.create_token_response, self._request(request, "/oauth/token", items))
            return oauth_json(status, body, headers)

        @app.post("/oauth/revoke", include_in_schema=False)
        async def revoke(request: Request) -> Response:
            if not self.available(request):
                return unavailable()
            if not self.token_limit.allow(self.deps.client_ip(request)):
                return oauth_json(429, {"error": "slow_down"})
            form = await request.form()
            items = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
            try:
                await self.ensure_client(dict(items).get("client_id"))
            except ClientDocumentError as exc:
                return oauth_json(400, {"error": "invalid_client", "error_description": str(exc)})
            status, body, headers = await asyncio.to_thread(
                self.server.create_endpoint_response, "revocation", self._request(request, "/oauth/revoke", items))
            return oauth_json(status, body, headers)

        @app.post("/oauth/register", include_in_schema=False)
        async def register_client(request: Request) -> Response:
            if not self.available(request) or not self.settings().registration_enabled:
                return oauth_json(404, {"error": "invalid_request", "error_description": "Registration is off"})
            if not self.register_limit.allow(self.deps.client_ip(request)):
                return oauth_json(429, {"error": "slow_down", "error_description": "Too many registrations"})
            raw = await request.body()
            if len(raw) > REGISTRATION_MAX_BYTES:
                return oauth_json(400, {"error": "invalid_client_metadata", "error_description": "Too large"})
            try:
                data = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                return oauth_json(400, {"error": "invalid_client_metadata", "error_description": "Body must be JSON"})
            if not isinstance(data, Mapping):
                return oauth_json(400, {"error": "invalid_client_metadata", "error_description": "Body must be an object"})
            if not data.get("redirect_uris"):
                return oauth_json(400, {"error": "invalid_redirect_uri", "error_description": "redirect_uris is required"})
            await asyncio.to_thread(self._maybe_prune)
            jreq = _JsonRequest("POST", self.issuer(request) + "/oauth/register", request.headers, data)
            status, body, headers = await asyncio.to_thread(
                self.server.create_endpoint_response, "client_registration", jreq)
            return oauth_json(status, body, headers)

        # ---- consent and connections (Azizam's browser session)

        def session_person(request: Request) -> tuple[Any, Mapping[str, Any]] | None:
            session = self.deps.session(request)
            person = self.deps.person(session) if session is not None else None
            return (session, person) if person else None

        def login_required() -> JSONResponse:
            return JSONResponse({"error": {"code": "login_required", "message": "Sign in first."}}, status_code=401,
                                headers=no_store)

        def gone() -> JSONResponse:
            return JSONResponse({"error": {"code": "request_expired",
                                           "message": "This sign-in request expired, was already answered or was "
                                                      "started in another browser. Start again from the app."}},
                                status_code=410, headers=no_store)

        @app.get("/me/api/mcp/requests/{request_id}", include_in_schema=False)
        async def consent_details(request_id: str, request: Request) -> Response:
            found = await asyncio.to_thread(session_person, request)
            if found is None:
                return login_required()
            _session, person = found
            params = await asyncio.to_thread(self.deps.store.peek_request, request_id,
                                             request.cookies.get(BINDING_COOKIE))
            if params is None:
                return gone()
            data = dict(params)
            client = await asyncio.to_thread(self.deps.store.client, data.get("client_id", ""))
            if client is None:
                return gone()
            redirect = data.get("redirect_uri") or (client.redirect_uris[0] if len(client.redirect_uris) == 1 else "")
            plan = await asyncio.to_thread(self.deps.plan, str(person["id"]))
            can_connect = await asyncio.to_thread(self.deps.person_can_connect, str(person["id"]))
            return JSONResponse({
                "client": {
                    "name": client.name,
                    "client_id": client.client_id,
                    # Who vouches for the name: the domain serving the metadata
                    # document, or nobody (self-registered).
                    "verified_domain": urlsplit(client.client_id).hostname if client.kind == "cimd" else None,
                    "registration": "metadata_document" if client.kind == "cimd" else "self_registered",
                    "redirect_host": urlsplit(redirect).hostname or urlsplit(redirect).scheme + ":",
                },
                "person": {"name": person.get("display_name") or person.get("email") or "you",
                           "email": person.get("email")},
                "plan": dict(plan),
                "can_connect": can_connect,
                "access": [
                    "See which media workflows your plan includes",
                    "Make images, video, music, sound and speech within your plan",
                    "Check on and cancel work it started",
                ],
                "lasts": "Until you disconnect it on the Connect page",
            }, headers=no_store)

        @app.post("/me/api/mcp/requests/{request_id}", include_in_schema=False)
        async def consent_decision(request_id: str, request: Request) -> Response:
            found = await asyncio.to_thread(session_person, request)
            if found is None:
                return login_required()
            session, person = found
            refusal = self.deps.check_action(request, session)
            if refusal is not None:
                return refusal
            try:
                body = await request.json()
            except ValueError:
                body = None
            decision = body.get("decision") if isinstance(body, Mapping) else None
            if decision not in ("allow", "deny"):
                return JSONResponse({"error": {"code": "invalid_decision", "message": "decision must be allow or deny"}},
                                    status_code=400, headers=no_store)
            person_id = str(person["id"])
            if decision == "allow" and not await asyncio.to_thread(self.deps.person_can_connect, person_id):
                return JSONResponse({"error": {"code": "no_access",
                                               "message": "Your account can't use AI here yet. Ask the owner for access."}},
                                    status_code=403, headers=no_store)
            params = await asyncio.to_thread(self.deps.store.take_request, request_id,
                                             request.cookies.get(BINDING_COOKIE))
            if params is None:
                return gone()
            try:
                await self.ensure_client(dict(params).get("client_id"))
            except ClientDocumentError as exc:
                return JSONResponse({"error": {"code": "invalid_client", "message": str(exc)}}, status_code=400,
                                    headers=no_store)
            status, payload = await asyncio.to_thread(
                self._decide, self._request(request, "/oauth/authorize", params), self.issuer(request),
                person_id if decision == "allow" else None)
            if status == 200:
                await asyncio.to_thread(self.deps.audit, "mcp.connection." + ("approved" if decision == "allow" else "denied"),
                                        person_id=person_id, client_id=dict(params).get("client_id"))
            response = JSONResponse(payload, status_code=status, headers=no_store)
            response.delete_cookie(BINDING_COOKIE, path="/")
            return response

        @app.get("/me/api/mcp/connections", include_in_schema=False)
        async def connections(request: Request) -> Response:
            found = await asyncio.to_thread(session_person, request)
            if found is None:
                return login_required()
            grants = await asyncio.to_thread(self.deps.store.grants_for_person, str(found[1]["id"]))
            return JSONResponse({"connections": [{
                "id": grant.id,
                "client_name": grant.client_name,
                "client_id": grant.client_id,
                "redirect_host": urlsplit(grant.redirect_uri).hostname or urlsplit(grant.redirect_uri).scheme + ":",
                "created_at": grant.created_at,
                "last_used_at": grant.last_used_at,
            } for grant in grants]}, headers=no_store)

        @app.post("/me/api/mcp/connections/{grant_id}/revoke", include_in_schema=False)
        async def revoke_connection(grant_id: str, request: Request) -> Response:
            found = await asyncio.to_thread(session_person, request)
            if found is None:
                return login_required()
            session, person = found
            refusal = self.deps.check_action(request, session)
            if refusal is not None:
                return refusal
            revoked = await asyncio.to_thread(self.deps.store.revoke_grant, grant_id, "person_revoked",
                                              person_id=str(person["id"]))
            if not revoked:
                return JSONResponse({"error": {"code": "not_found", "message": "No such connection."}},
                                    status_code=404, headers=no_store)
            await asyncio.to_thread(self.deps.audit, "mcp.connection.revoked", person_id=str(person["id"]),
                                    grant_id=grant_id)
            return JSONResponse({"revoked": grant_id}, headers=no_store)
