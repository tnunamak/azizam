"""Environment-driven configuration for the SDK compatibility harness.

The harness keeps a deliberately small set of inputs so probes for any
SDK (OpenAI, Anthropic, Ollama, Vercel AI SDK, ...) can share the same
base URL and bearer key. Probes that need backend-specific overrides
should layer extra env vars on top, not redefine these.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when the harness environment is misconfigured."""


@dataclass(frozen=True)
class HarnessConfig:
    """Resolved harness configuration.

    Attributes:
        base_url: Root URL of the gateway, e.g. ``http://127.0.0.1:8080``.
            Probes are expected to append protocol-specific suffixes
            such as ``/v1`` (OpenAI), ``/v1/messages`` (Anthropic), or
            ``/api`` (Ollama). The base URL must not already include a
            protocol suffix so a single value can drive every probe.
        api_key: Bearer token presented to the gateway. May be empty
            only when ``allow_anonymous`` is true.
        request_timeout_s: Per-request timeout in seconds.
        allow_anonymous: When true, an empty api_key is allowed.
        extra: Bag of additional env values probes may want, e.g.
            ``ANTHROPIC_BASE_URL`` overrides.
    """

    base_url: str
    api_key: str
    request_timeout_s: float = 30.0
    allow_anonymous: bool = False
    extra: Mapping[str, str] = field(default_factory=dict)

    def openai_base_url(self) -> str:
        """URL suitable for the OpenAI SDK ``base_url`` argument."""

        return self.base_url.rstrip("/") + "/v1"

    def anthropic_base_url(self) -> str:
        """URL suitable for the Anthropic SDK ``base_url`` argument."""

        # Anthropic SDKs append ``/v1/messages`` themselves, so we hand
        # them the gateway root.
        return self.base_url.rstrip("/")

    def ollama_host(self) -> str:
        """Host URL suitable for the Ollama SDK ``host`` argument."""

        return self.base_url.rstrip("/")


_RESERVED_SUFFIXES = ("/v1", "/v1/", "/api", "/api/")


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    allow_anonymous: bool = False,
) -> HarnessConfig:
    """Read ``OPENAI_PROXY_BASE_URL`` / ``OPENAI_PROXY_API_KEY`` from env.

    Pass ``env`` to make this deterministic in tests. ``allow_anonymous``
    is used by probe modules that exercise public endpoints such as
    ``GET /v1/models`` against gateways that do not require auth.
    """

    env = os.environ if env is None else env

    base_url = (env.get("OPENAI_PROXY_BASE_URL") or "").strip()
    if not base_url:
        raise ConfigError(
            "OPENAI_PROXY_BASE_URL is not set. Example: "
            "OPENAI_PROXY_BASE_URL=http://127.0.0.1:8080"
        )

    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"OPENAI_PROXY_BASE_URL must start with http:// or https://, got {base_url!r}"
        )
    if not parsed.netloc:
        raise ConfigError(f"OPENAI_PROXY_BASE_URL has no host: {base_url!r}")

    path = parsed.path.rstrip("/")
    for suffix in _RESERVED_SUFFIXES:
        if path.endswith(suffix.rstrip("/")) and suffix.strip("/"):
            raise ConfigError(
                "OPENAI_PROXY_BASE_URL must be the gateway root, not a "
                f"protocol prefix. Drop the trailing {suffix!r} from {base_url!r}."
            )

    api_key = (env.get("OPENAI_PROXY_API_KEY") or "").strip()
    if not api_key and not allow_anonymous:
        raise ConfigError(
            "OPENAI_PROXY_API_KEY is not set. Use allow_anonymous=True "
            "only for probes that exercise unauthenticated endpoints."
        )

    timeout_raw = env.get("OPENAI_PROXY_TIMEOUT_S")
    if timeout_raw:
        try:
            request_timeout_s = float(timeout_raw)
        except ValueError as exc:
            raise ConfigError(
                f"OPENAI_PROXY_TIMEOUT_S must be a number, got {timeout_raw!r}"
            ) from exc
        if request_timeout_s <= 0:
            raise ConfigError("OPENAI_PROXY_TIMEOUT_S must be positive")
    else:
        request_timeout_s = 30.0

    extra = {
        k: v
        for k, v in env.items()
        if k.startswith("OPENAI_PROXY_")
        and k
        not in {
            "OPENAI_PROXY_BASE_URL",
            "OPENAI_PROXY_API_KEY",
            "OPENAI_PROXY_TIMEOUT_S",
        }
    }

    return HarnessConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        request_timeout_s=request_timeout_s,
        allow_anonymous=allow_anonymous,
        extra=extra,
    )
