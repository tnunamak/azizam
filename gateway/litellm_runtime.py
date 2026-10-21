"""The executable LiteLLM config, rendered only here (decision D30).

``gateway/litellm_substrate.py`` compiles the router config from public data and cannot read a secret. This module
gives it a ``Materializer`` that resolves what a record only references (a secret header, the credentials of an
endpoint) at the moment the file is written, and writes the result to a restricted file. The executable config
is never returned to a management route and never logged: ``GET /admin/api/operator/litellm-config`` answers with
the preview the substrate compiles without a materializer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway import runtime_secrets
from gateway.service_registry import service_base_url
from gateway.litellm_substrate import (
    Materializer, compile_litellm_config_from_registry, litellm_config_path_from_registry,
)


def _header(row: Mapping[str, Any], origin: str | None) -> str | None:
    try:
        return runtime_secrets.resolve(row.get("secret_ref"), origin=origin)
    except runtime_secrets.SecretUnavailable:
        return None


def _url(config: Mapping[str, Any]) -> str:
    """A record's endpoint with its stored credentials; the public endpoint when they cannot be read (the engine then
    refuses the call, and one unreadable secret does not stop the router config of every other engine)."""
    try:
        return runtime_secrets.service_url(config)
    except runtime_secrets.SecretUnavailable:
        return service_base_url(config)


def materializer() -> Materializer:
    return Materializer(header=_header, url=_url)


def compile_executable_config(registry: Mapping[str, Any]) -> dict[str, Any]:
    return compile_litellm_config_from_registry(registry, materializer=materializer())


def write_litellm_config_artifact(registry: Mapping[str, Any], *, base_dir: Path, path: Path | None = None) -> Path:
    """Write the executable config, readable by this user only, replacing the file in one step."""
    path = path if path is not None else litellm_config_path_from_registry(registry, base_dir=base_dir)
    compiled = compile_executable_config(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(compiled, sort_keys=False, allow_unicode=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path
