"""Dynamic registration, PKCE, identity verification and refresh for one connection mechanism."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from gateway import sign_in_policy, subscription_store
from gateway.session_guard import SessionEnded
from gateway.oidc_jwks import JWKSCache
from gateway.subscription_kinds import SubscriptionKind
from gateway.subscription_store import Attempt
from gateway.provider_connections import (CONNECT_MECHANISMS, ConnectionContext, ConnectionFacts, ConnectStart, Credential, SubscriptionError)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _safe_error_field(body: Any) -> str | None:
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            return error["code"]
    return None


class OAuthPkceMechanism:
    def __init__(self, proof_verifier=None):
        self.proof_verifier = proof_verifier or JWKSCache()

    def configured(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> bool:
        return connection["status"] == subscription_store.CONNECTED

    async def _post_form(self, ctx, url: str, fields: Mapping[str, str]) -> tuple[int, Any]:
        try:
            async with ctx.client_context() as client:
                response = await client.post(url, data=dict(fields), headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise SubscriptionError(502, "provider_unreachable", "The sign-in service could not be reached. Try again.") from None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    async def _discover(self, ctx, kind: SubscriptionKind) -> Mapping[str, Any]:
        cached = ctx.runtime.get("discovery")
        if cached is not None:
            return cached
        try:
            async with ctx.client_context() as client:
                response = await client.get(str(kind.oauth["discovery_url"]))
            document = response.json()
            if response.status_code != 200 or not isinstance(document, Mapping):
                raise ValueError
        except (httpx.HTTPError, ValueError):
            raise SubscriptionError(502, "provider_unreachable", "The sign-in service could not be reached. Try again.") from None
        ctx.runtime["discovery"] = document
        return document

    async def start(self, ctx: ConnectionContext, attempt: Attempt) -> ConnectStart:
        kind = ctx.kind
        oauth = kind.oauth
        saved = await asyncio.to_thread(ctx.store.connection, kind.id)
        # The issued client id belongs to this instance's registration: reuse it. Only a first sign-in registers.
        client_id = saved["client_id"] if saved else str(oauth["dynamic_client_id"])
        host_id = await asyncio.to_thread(ctx.store.host_id, kind.id, str(oauth["host_id_scheme"]))
        attempt_seconds = sign_in_policy.policy().connection_attempt_seconds
        verifier = _b64url(secrets.token_bytes(48))
        state, nonce = _b64url(secrets.token_bytes(24)), _b64url(secrets.token_bytes(24))
        params = {
            "client_id": client_id, "ext_agent_host_id": host_id, "response_type": "code",
            "redirect_uri": oauth["redirect_uri"], "scope": oauth["scope"], "resource": oauth["resource"],
            "state": state, "nonce": nonce, "code_challenge_method": "S256",
            "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        }
        if saved is None:
            params["agent_name_hint"] = oauth["agent_name_hint"]
        return ConnectStart(
            owner_action={"authorize_url": f"{oauth['authorize_url']}?{urlencode(params)}",
                          "redirect_uri": oauth["redirect_uri"], "expires_in": attempt_seconds},
            state=state, mechanism_state={"nonce": nonce, "verifier": verifier, "client_id": client_id,
                                          "host_id": host_id, "redirect_uri": str(oauth["redirect_uri"])},
            ttl=attempt_seconds)

    async def finish(self, ctx: ConnectionContext, attempt: Attempt, values: Any) -> ConnectionFacts:
        if attempt.operation == "import":
            return await self._import_record(ctx, attempt.owner_id, values)
        kind = ctx.kind
        oauth = kind.oauth
        if not hmac.compare_digest(values.get("state", ""), attempt.state):
            raise SubscriptionError(400, "state_mismatch", "That sign-in result does not belong to this attempt. Start again.")
        if "error" in values:
            raise SubscriptionError(400, "sign_in_refused", "The provider refused the sign-in. Start again to retry.")
        code = values.get("code")
        if not code:
            raise SubscriptionError(400, "callback_invalid", "That address has no sign-in code in it. Paste the whole address.")
        saved = await asyncio.to_thread(ctx.store.connection, kind.id)
        issued = values.get("client_id") or attempt.mechanism_state["client_id"]
        if saved is not None and issued != saved["client_id"]:
            raise SubscriptionError(409, "client_id_changed", "The provider issued a different registration than the saved one. Disconnect and connect again.")
        status, body = await self._post_form(ctx, str(oauth["token_url"]), {
            "grant_type": "authorization_code", "client_id": issued, "code": code, "code_verifier": attempt.mechanism_state["verifier"],
            "redirect_uri": attempt.mechanism_state["redirect_uri"], "resource": str(oauth["resource"])})
        if status != 200 or not isinstance(body, Mapping) or not body.get("access_token"):
            raise SubscriptionError(502, "token_exchange_failed", "The provider did not accept the sign-in. Start again.")
        scopes = sorted(str(body.get("scope") or "").split())
        if str(oauth["required_scope"]) not in scopes:
            raise SubscriptionError(403, "plan_permission_missing", "The sign-in did not grant permission to use your plan.")
        if not body.get("refresh_token"):
            raise SubscriptionError(502, "token_exchange_failed", "The provider issued no refresh token. Start again.")
        claims = await self._validate_id_token(ctx, kind, str(body.get("id_token") or ""), client_id=issued, nonce=attempt.mechanism_state["nonce"])
        if saved is not None and saved.get("account_subject") and saved["account_subject"] != claims.get("sub"):
            raise SubscriptionError(409, "account_mismatch", "That is a different account than the one connected before. Disconnect first.")
        tokens = self._token_set(ctx, body, now=ctx.clock())
        return ConnectionFacts(
            metadata={"client_id": issued, "host_id": attempt.mechanism_state["host_id"],
                      "scopes": scopes, "email": claims.get("email"), "subject": claims.get("sub")},
            credential=Credential("bearer", tokens["access_token"]), secret_state=tokens, origin=kind.upstream_origin)

    async def _validate_id_token(self, ctx, kind: SubscriptionKind, id_token: str, *, client_id: str, nonce: str) -> dict[str, Any]:
        """Signature (JWKS), issuer, audience, expiry and nonce. A token that fails any of them signs nobody in."""
        refusal = SubscriptionError(502, "id_token_invalid", "The provider's identity proof could not be verified. Start again.")
        if not id_token:
            raise refusal
        discovery = await self._discover(ctx, kind)
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise refusal
        try:
            claims = await self.proof_verifier.verify(id_token, uri=jwks_uri, leeway=60)
        except (ValueError, httpx.HTTPError):
            raise refusal from None
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        expires = claims.get("exp")
        if (claims.get("iss") != kind.oauth["issuer"] or client_id not in audiences
                or not isinstance(expires, (int, float)) or expires < ctx.clock() - 60
                or not hmac.compare_digest(str(claims.get("nonce") or ""), nonce)):
            raise refusal
        return claims

    def _token_set(self, ctx, body: Mapping[str, Any], *, now: float, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
        refresh = body.get("refresh_token") or (previous or {}).get("refresh_token")
        lifetime = body.get("expires_in")
        earliest = body.get("earliest_refresh_at")
        return {
            "access_token": str(body["access_token"]), "refresh_token": refresh,
            "token_type": body.get("token_type") or "Bearer",
            "expires_at": int(now + (lifetime if isinstance(lifetime, (int, float)) else 3600)),
            "earliest_refresh_at": int(earliest) if isinstance(earliest, (int, float)) else None,
        }

    def _usable(self, ctx, kind: SubscriptionKind, tokens: Mapping[str, Any]) -> bool:
        now = ctx.clock()
        expires = tokens.get("expires_at") or 0
        if now < expires - kind.refresh_margin:
            return True
        earliest = tokens.get("earliest_refresh_at")
        # Not yet allowed to refresh, and the access token still works: keep using it.
        return bool(earliest and now < earliest and now < expires)

    async def _tokens_or_refuse(self, ctx, kind: SubscriptionKind) -> dict[str, Any]:
        connection = await asyncio.to_thread(ctx.store.connection, kind.id)
        if connection is None or connection["status"] == subscription_store.DISCONNECTED:
            raise SubscriptionError(503, "plan_not_connected", f"No {kind.display_name} plan is connected to this gateway. The owner can connect one.")
        if connection["status"] == subscription_store.NEEDS_RECONNECT:
            raise SubscriptionError(503, "plan_connection_invalid", f"The gateway's {kind.display_name} connection ended. The owner needs to connect again.")
        tokens = await asyncio.to_thread(ctx.store.load_tokens, kind.id, origin=kind.upstream_origin)
        if not tokens:
            raise SubscriptionError(503, "plan_connection_invalid", f"The gateway's {kind.display_name} connection ended. The owner needs to connect again.")
        return tokens

    async def _refresh(self, ctx, kind: SubscriptionKind, tokens: Mapping[str, Any]) -> str:
        connection = await asyncio.to_thread(ctx.store.connection, kind.id)
        status, body = await self._post_form(ctx, str(kind.oauth["token_url"]), {
            "grant_type": "refresh_token", "client_id": connection["client_id"],
            "refresh_token": str(tokens["refresh_token"]), "resource": str(kind.oauth["resource"])})
        if status == 200 and isinstance(body, Mapping) and body.get("access_token"):
            fresh = self._token_set(ctx, body, now=ctx.clock(), previous=tokens)
            # A refresh is started by a request, not by the owner's browser: no sign-in to check.
            await asyncio.to_thread(ctx.store.save_tokens, kind.id, fresh, origin=kind.upstream_origin, session=None,
                                    expected_generation=connection["generation"])
            return fresh["access_token"]
        error = _safe_error_field(body)
        if status in (400, 401) and error in kind.terminal_refresh_errors:
            invalid = await asyncio.to_thread(ctx.store.invalidate, kind.id, expected_generation=connection["generation"],
                                               status=subscription_store.NEEDS_RECONNECT, reason=subscription_store.REFRESH_REFUSED)
            if invalid is not None:
                await asyncio.to_thread(ctx.store.drop_tokens, kind.id, session=None, expected_generation=invalid["generation"])
            raise SubscriptionError(503, "plan_connection_invalid", f"The gateway's {kind.display_name} connection ended. The owner needs to connect again.")
        raise SubscriptionError(503, "plan_refresh_unavailable", f"The {kind.display_name} connection could not be renewed just now. Try again shortly.")

    async def _import_record(self, ctx: ConnectionContext, owner_id: str, record: Mapping[str, Any]) -> ConnectionFacts:
        """Take a registration an earlier sign-in already made (client id, host id and tokens) as this gateway's
        connection, so the owner signs in once. Refresh tokens rotate: after this the gateway alone refreshes
        the set, and the file the record came from goes stale."""
        kind = ctx.kind
        invalid = SubscriptionError(400, "import_invalid", "That record is not a usable sign-in record for this kind.")
        def text(key: str) -> str:
            value = record.get(key)
            if not isinstance(value, str) or not value:
                raise invalid
            return value
        client_id, host_id, access, refresh = text("client_id"), text("ext_agent_host_id"), text("access_token"), text("refresh_token")
        if record.get("issuer") != kind.oauth["issuer"]:
            raise invalid
        scopes = record.get("scopes")
        if not isinstance(scopes, list) or str(kind.oauth["required_scope"]) not in scopes:
            raise SubscriptionError(403, "plan_permission_missing", "That sign-in did not grant permission to use the plan.")
        saved_at, lifetime = record.get("saved_at"), record.get("expires_in")
        if not isinstance(saved_at, (int, float)):
            raise invalid
        earliest = record.get("earliest_refresh_at")
        tokens = {"access_token": access, "refresh_token": refresh, "token_type": record.get("token_type") or "Bearer",
                  "expires_at": int(saved_at + (lifetime if isinstance(lifetime, (int, float)) else 3600)),
                  "earliest_refresh_at": int(earliest) if isinstance(earliest, (int, float)) else None}
        existing = await asyncio.to_thread(ctx.store.connection, kind.id)
        if existing is not None and existing["status"] == subscription_store.CONNECTED:
            raise SubscriptionError(409, "already_connected", "A plan is already connected. Disconnect it before importing another.")
        await asyncio.to_thread(ctx.store.set_host_id, kind.id, host_id)
        return ConnectionFacts(
            metadata={"client_id": client_id, "host_id": host_id, "scopes": sorted(str(item) for item in scopes),
                      "email": record.get("email") if isinstance(record.get("email"), str) else None,
                      "subject": record.get("subject") if isinstance(record.get("subject"), str) else None},
            credential=Credential("bearer", access), secret_state=tokens, origin=kind.upstream_origin)

    async def disconnect(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> dict[str, Any]:
        """End the connection: revoke the refresh token (best effort), delete the tokens, keep the registration."""
        kind = ctx.kind
        row = connection
        tokens = await asyncio.to_thread(ctx.store.load_tokens, kind.id, origin=kind.upstream_origin,
                                       expected_generation=connection["generation"])
        revoked = False
        if tokens and tokens.get("refresh_token"):
            try:
                endpoint = (await self._discover(ctx, kind)).get("revocation_endpoint")
                if isinstance(endpoint, str):
                    status, _ = await self._post_form(ctx, endpoint, {
                        "token": str(tokens["refresh_token"]), "token_type_hint": "refresh_token",
                        "client_id": row["client_id"]})
                    revoked = status == 200
            except SubscriptionError:
                revoked = False
        return {"revoked_upstream": revoked}

    def validate(self, row: Mapping[str, Any], *, source: Any) -> None:
        validate(row, source=source)

    async def acquire(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Credential:
        kind = ctx.kind
        if ctx.rejected is None:
            tokens = await self._tokens_or_refuse(ctx, kind)
            if self._usable(ctx, kind, tokens):
                return Credential("bearer", tokens["access_token"])
        async with ctx.lock:
            tokens = await self._tokens_or_refuse(ctx, kind)
            if ctx.rejected is not None:
                if tokens["access_token"] != ctx.rejected.value:
                    return Credential("bearer", tokens["access_token"])
            elif self._usable(ctx, kind, tokens):
                return Credential("bearer", tokens["access_token"])
            return Credential("bearer", await self._refresh(ctx, kind, tokens))

    def status(self, ctx: ConnectionContext, connection: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"account_email": connection["account_email"], "connected_at": connection["connected_at"],
                "access_expires_at": connection["access_expires_at"],
                "scopes": connection["scopes"].split()}

    async def listen_for_callback(self, ctx: ConnectionContext, finish, *, host="127.0.0.1", port=None,
                                  timeout=None):
        timeout = sign_in_policy.policy().connection_attempt_seconds if timeout is None else timeout
        port = port if port is not None else urlsplit(str(ctx.kind.oauth["redirect_uri"])).port
        listener = CallbackListener(finish, host=host, port=int(port), timeout=timeout)
        return listener if await listener.open() else None


class CallbackListener:
    """A loopback listener that hands parsed values to the service completion callback."""

    def __init__(self, finish, *, host: str, port: int, timeout: float) -> None:
        self._finish, self._host, self._port, self._timeout = finish, host, port, timeout
        self._server: asyncio.AbstractServer | None = None
        self.result: dict[str, Any] | None = None
        self.error: SubscriptionError | None = None
        self.done = asyncio.Event()

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1] if self._server else self._port

    async def open(self) -> bool:
        try:
            self._server = await asyncio.start_server(self._handle, self._host, self._port)
        except OSError:
            return False
        asyncio.get_running_loop().call_later(self._timeout, self.close)
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self.done.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            target = line.split(" ")[1] if line.count(" ") >= 2 else ""
            parts = urlsplit(target)
            if parts.path != "/auth/callback":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            try:
                self.result = await self._finish(dict(parse_qsl(parts.query, keep_blank_values=True)))
                text = b"Sign-in received. You can close this tab."
            except SessionEnded:  # the sign-in that began the attempt ended before the callback arrived
                self.error = SubscriptionError(401, "login_required", "Sign in again to connect the plan.")
                text = b"This sign-in could not be completed. Go back to Azizam and start again."
            except SubscriptionError as error:
                self.error = error
                text = b"This sign-in could not be completed. Go back to Azizam and start again."
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                         + str(len(text)).encode() + b"\r\n\r\n" + text)
            self.close()
        except (asyncio.TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()


_OAUTH_KEYS = ("authorize_url", "token_url", "discovery_url", "issuer", "resource", "scope", "required_scope",
               "redirect_uri", "dynamic_client_id", "agent_name_hint", "host_id_scheme")


def validate(config: Mapping[str, Any], *, source: Any) -> None:
    oauth = config.get("oauth")
    if not isinstance(oauth, Mapping) or not all(oauth.get(key) for key in _OAUTH_KEYS):
        raise ValueError(f"{source}: oauth needs {', '.join(_OAUTH_KEYS)}")
    for key in ("authorize_url", "token_url", "discovery_url", "issuer", "resource"):
        if urlsplit(str(oauth[key])).scheme != "https":
            raise ValueError(f"{source}: oauth.{key} must be an https address")
    if str(oauth["required_scope"]) not in str(oauth["scope"]).split():
        raise ValueError(f"{source}: oauth.scope must include the required scope")


CONNECT_MECHANISMS["oauth-pkce-dynamic"] = OAuthPkceMechanism()
