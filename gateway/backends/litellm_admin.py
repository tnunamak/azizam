"""Thin client for the LiteLLM sidecar virtual-key management API.

The "LiteLLM owns LLM runtime keys" split (see ``docs/LITELLM_SUBSTRATE_DECISION.md``)
puts the durable App keys, OIDC users, Profiles and policy gates in the gateway,
and the *virtual execution keys* — the per-model/per-budget keys LiteLLM mints
and meters for commodity LLM execution — in the LiteLLM sidecar. The gateway
forwards a virtual key on execution as the ``X-Litellm-Key`` delegated header;
this module is the operator-facing side: list / create / inspect / delete those
virtual keys via the sidecar's ``/key/*`` admin endpoints.

Design constraints, matching the rest of ``gateway/backends`` (the substrate
transport boundary):

* No imports of ``proxy.py`` and no ``os.environ`` reads. The caller resolves a
  :class:`~gateway.backends.llm_execution.LiteLlmRuntimeConfig` and hands it in.
* ``/key/*`` is master-key protected. The master key is exactly the gateway's
  sidecar bearer (``start.sh`` sets ``LITELLM_MASTER_KEY=$AZIZAM_LITELLM_API_KEY``),
  so ``runtime_config.api_key`` IS the management credential — no separate field.
* Errors surface as a single typed :class:`LiteLLMAdminError` carrying an HTTP
  status + machine code, so the admin endpoint layer can map them to a
  protocol-shaped response without leaking httpx/transport details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from gateway.backends.llm_execution import (
    LITELLM_DELEGATED_KEY_HEADER,
    LiteLlmRuntimeConfig,
    litellm_url,
)

# A short timeout: these are local management calls against the sidecar, run
# synchronously inside an admin request. They must never hang the admin UI.
_KEY_ADMIN_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class LiteLLMAdminError(Exception):
    """A failure talking to the sidecar key API, mapped to an HTTP response.

    ``status_code`` is the operator-facing HTTP status, ``code`` a stable
    machine code, ``message`` a human hint. ``upstream_status`` records the raw
    sidecar status when the failure was an upstream rejection (vs. a transport
    error), for diagnosability.
    """

    status_code: int
    code: str
    message: str
    upstream_status: int | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class LiteLLMKeyAdminClient:
    """Operate on LiteLLM virtual keys via the sidecar ``/key/*`` endpoints.

    Construct with a resolved runtime config; all methods are ``async`` and
    raise :class:`LiteLLMAdminError` on any non-success outcome (substrate
    disabled, transport failure, or upstream rejection).
    """

    def __init__(self, runtime_config: LiteLlmRuntimeConfig) -> None:
        self._config = runtime_config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and self._config.base_url)

    # -- public operations ---------------------------------------------------

    async def list_keys(
        self, *, page: int = 1, size: int = 100
    ) -> list[dict[str, Any]]:
        """Return the sidecar's virtual keys as a list of full key objects."""
        query = urlencode(
            {"page": page, "size": size, "return_full_object": "true"}
        )
        payload = await self._request("GET", "key/list", query=query)
        return _normalize_key_list(payload)

    async def generate_key(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Mint a new virtual key. ``spec`` is a sanitized /key/generate body.

        Returns the raw sidecar response, which includes the secret ``key`` —
        the ONLY time the key material is available (reveal-once).
        """
        payload = await self._request("POST", "key/generate", body=dict(spec))
        if not isinstance(payload, Mapping) or not payload.get("key"):
            raise LiteLLMAdminError(
                502,
                "litellm_bad_response",
                "LiteLLM did not return a key for the generate request.",
            )
        return dict(payload)

    async def key_info(self, key: str) -> dict[str, Any]:
        """Return metadata for one key (does not expose the secret)."""
        query = urlencode({"key": key})
        payload = await self._request("GET", "key/info", query=query)
        info = payload.get("info") if isinstance(payload, Mapping) else None
        return dict(info) if isinstance(info, Mapping) else dict(payload or {})

    async def delete_keys(
        self,
        *,
        keys: list[str] | None = None,
        key_aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """Revoke keys by secret value and/or by alias."""
        body: dict[str, Any] = {}
        if keys:
            body["keys"] = list(keys)
        if key_aliases:
            body["key_aliases"] = list(key_aliases)
        if not body:
            raise LiteLLMAdminError(
                400,
                "invalid_request",
                "delete requires at least one key or key_alias.",
            )
        payload = await self._request("POST", "key/delete", body=body)
        return dict(payload) if isinstance(payload, Mapping) else {}

    # -- transport -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self.enabled:
            raise LiteLLMAdminError(
                503,
                "litellm_unconfigured",
                "The LiteLLM execution substrate is not configured; "
                "no key management endpoint is available.",
            )
        if not self._config.api_key:
            # A base_url is wired but no master key is configured. The /key/* API
            # is master-key protected, so without it we cannot manage keys. Fail
            # with an actionable message rather than building an illegal
            # "Bearer " (empty token) header that httpx rejects as a transport
            # error, which would surface as a confusing "unreachable" 503.
            raise LiteLLMAdminError(
                503,
                "litellm_unconfigured",
                "The LiteLLM master key is not configured "
                "(set AZIZAM_LITELLM_API_KEY to match the sidecar's "
                "LITELLM_MASTER_KEY); virtual-key management is unavailable.",
            )
        url = litellm_url(base_url=self._config.base_url, path=path, query=query)
        headers = {
            LITELLM_DELEGATED_KEY_HEADER: f"Bearer {self._config.api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        content = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            async with httpx.AsyncClient(timeout=_KEY_ADMIN_TIMEOUT_S) as client:
                resp = await client.request(
                    method, url, headers=headers, content=content
                )
        except httpx.HTTPError as exc:
            raise LiteLLMAdminError(
                503,
                "litellm_unreachable",
                "Could not reach the LiteLLM sidecar key API.",
            ) from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise LiteLLMAdminError(
                502,
                "litellm_admin_auth_failed",
                "The gateway's LiteLLM master key was rejected by the sidecar "
                "key API. Confirm LITELLM_MASTER_KEY matches the gateway's "
                "AZIZAM_LITELLM_API_KEY.",
                upstream_status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise LiteLLMAdminError(
                502,
                "litellm_key_api_error",
                _upstream_message(resp),
                upstream_status=resp.status_code,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise LiteLLMAdminError(
                502,
                "litellm_bad_response",
                "LiteLLM key API returned a non-JSON response.",
                upstream_status=resp.status_code,
            ) from exc


def _upstream_message(resp: httpx.Response) -> str:
    """The message for a failed LiteLLM key call: ours, from the status alone. The sidecar's own words are not passed on
    (it echoes what it was sent, and it was sent the master key; decision D44)."""
    return f"LiteLLM key API returned HTTP {resp.status_code}."


def _normalize_key_list(payload: Any) -> list[dict[str, Any]]:
    """Flatten the /key/list response into a list of key dicts.

    LiteLLM has returned this shape a few ways across versions: a bare list, a
    ``{"keys": [...]}`` wrapper, or ``{"keys": ["sk-..."]}`` (strings only when
    return_full_object is not honored). Normalize all to dicts with at least a
    ``token``/``key_name`` and ``key_alias`` so the panel can render uniformly.
    """
    if isinstance(payload, Mapping):
        rows = payload.get("keys")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            out.append(dict(row))
        elif isinstance(row, str):
            out.append({"token": row, "key_name": row})
    return out
