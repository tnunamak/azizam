#!/usr/bin/env python3
"""Lightweight local smoke checks for the gateway's public protocol frontends.

Unlike ``run_matrix.py`` (which drives real third-party SDK probes), this
script speaks raw HTTP via ``urllib`` and is intended to be run by the
owner after live routes are wired locally. It performs a small set of
safe checks and never prints secrets, prompts, or raw response bodies by
default.

Environment::

    AZIZAM_BASE_URL       e.g. http://127.0.0.1:5000   (required for non-skip)
    AZIZAM_API_KEY        e.g. opk_...                 (required for auth checks)
    AZIZAM_SMOKE_LIVE_LLM 1 to enable LLM round-trips  (default: off)
    AZIZAM_SMOKE_LIVE_EMBEDDINGS 1 to enable embedding round-trips
    AZIZAM_SMOKE_TIMEOUT  request timeout seconds      (default: 10)
    AZIZAM_SMOKE_ANTHROPIC_MODEL   override Anthropic probe model
    AZIZAM_SMOKE_OLLAMA_MODEL      override Ollama probe model
    AZIZAM_SMOKE_EMBEDDINGS_MODEL  override embedding probe model

Each check prints a single compact row:

    [PASS] anthropic.messages.nonstream  status=200
    [SKIP] ollama.chat.nonstream         reason=AZIZAM_SMOKE_LIVE_LLM not set
    [FAIL] gateway.capabilities          status=500

Exit code is 0 when no checks failed (skips are not failures), else 1.
"""

from __future__ import annotations

from gateway.compat import get_env

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple

DEFAULT_TIMEOUT_S = 10
DEFAULT_ANTHROPIC_MODEL = "default"
DEFAULT_OLLAMA_MODEL = "default"
DEFAULT_EMBEDDINGS_MODEL = "nomic-embed-text-v1.5"
TINY_PROMPT = "ping"  # fixed non-secret string


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "pass" | "skip" | "fail"
    detail: str = ""

    def format_row(self) -> str:
        tag = {"pass": "PASS", "skip": "SKIP", "fail": "FAIL"}[self.status]
        return f"[{tag}] {self.name:<38} {self.detail}".rstrip()


@dataclass(frozen=True)
class SmokeConfig:
    base_url: Optional[str]
    api_key: Optional[str]
    live_llm: bool
    live_embeddings: bool
    timeout_s: int
    anthropic_model: str
    ollama_model: str
    embeddings_model: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SmokeConfig":
        if env is None:
            env = os.environ
        raw_base = get_env("AZIZAM_BASE_URL", "", environ=env).strip()
        base = raw_base.rstrip("/") if raw_base else None
        api_key = get_env("AZIZAM_API_KEY", "", environ=env).strip() or None
        live = get_env("AZIZAM_SMOKE_LIVE_LLM", "", environ=env).strip() == "1"
        live_embeddings = (
            get_env("AZIZAM_SMOKE_LIVE_EMBEDDINGS", "", environ=env).strip() == "1"
        )
        try:
            timeout = int(get_env("AZIZAM_SMOKE_TIMEOUT", "", environ=env).strip() or DEFAULT_TIMEOUT_S)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_S
        return cls(
            base_url=base,
            api_key=api_key,
            live_llm=live,
            live_embeddings=live_embeddings,
            timeout_s=max(1, timeout),
            anthropic_model=get_env("AZIZAM_SMOKE_ANTHROPIC_MODEL", "", environ=env).strip()
            or DEFAULT_ANTHROPIC_MODEL,
            ollama_model=get_env("AZIZAM_SMOKE_OLLAMA_MODEL", "", environ=env).strip()
            or DEFAULT_OLLAMA_MODEL,
            embeddings_model=get_env("AZIZAM_SMOKE_EMBEDDINGS_MODEL", "", environ=env).strip()
            or DEFAULT_EMBEDDINGS_MODEL,
        )


def _request(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Tuple[int, str]:
    """Perform an HTTP request and return (status_code, short_error_text).

    The response body is intentionally discarded — callers only see the
    status code, plus a short error string when the call could not
    complete or the server returned an HTTP error code.
    """

    url = f"{base_url}{path}"
    req = urllib.request.Request(url=url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Drain a small amount so the connection can close cleanly,
            # but we don't expose body content to the caller.
            resp.read(64)
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.reason or "http_error"
    except urllib.error.URLError as exc:
        return 0, f"url_error: {exc.reason!r}"
    except TimeoutError:
        return 0, "timeout"
    except OSError as exc:
        return 0, f"os_error: {exc.__class__.__name__}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_health(cfg: SmokeConfig) -> CheckResult:
    name = "gateway.health"
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    status, err = _request(base_url=cfg.base_url, path="/health", timeout=cfg.timeout_s)
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def check_capabilities(cfg: SmokeConfig) -> CheckResult:
    name = "gateway.capabilities"
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    if not cfg.api_key:
        return CheckResult(name, "skip", "reason=AZIZAM_API_KEY not set")
    status, err = _request(
        base_url=cfg.base_url,
        path="/gateway/v1/capabilities",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=cfg.timeout_s,
    )
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def check_anthropic_messages(cfg: SmokeConfig) -> CheckResult:
    name = "anthropic.messages.nonstream"
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    if not cfg.api_key:
        return CheckResult(name, "skip", "reason=AZIZAM_API_KEY not set")
    if not cfg.live_llm:
        return CheckResult(name, "skip", "reason=AZIZAM_SMOKE_LIVE_LLM not set")
    body = json.dumps(
        {
            "model": cfg.anthropic_model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
        }
    ).encode("utf-8")
    status, err = _request(
        base_url=cfg.base_url,
        path="/v1/messages",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
            "x-api-key": cfg.api_key,
            "anthropic-version": "2023-06-01",
        },
        body=body,
        timeout=cfg.timeout_s,
    )
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def check_ollama_tags(cfg: SmokeConfig) -> CheckResult:
    name = "ollama.tags"
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    if not cfg.api_key:
        return CheckResult(name, "skip", "reason=AZIZAM_API_KEY not set")
    status, err = _request(
        base_url=cfg.base_url,
        path="/api/tags",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=cfg.timeout_s,
    )
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def check_ollama_chat(cfg: SmokeConfig) -> CheckResult:
    name = "ollama.chat.nonstream"
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    if not cfg.api_key:
        return CheckResult(name, "skip", "reason=AZIZAM_API_KEY not set")
    if not cfg.live_llm:
        return CheckResult(name, "skip", "reason=AZIZAM_SMOKE_LIVE_LLM not set")
    body = json.dumps(
        {
            "model": cfg.ollama_model,
            "stream": False,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "options": {"num_predict": 8},
        }
    ).encode("utf-8")
    status, err = _request(
        base_url=cfg.base_url,
        path="/api/chat",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        body=body,
        timeout=cfg.timeout_s,
    )
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def _check_embedding_post(
    cfg: SmokeConfig,
    *,
    name: str,
    path: str,
    payload: dict,
) -> CheckResult:
    if not cfg.base_url:
        return CheckResult(name, "skip", "reason=AZIZAM_BASE_URL not set")
    if not cfg.api_key:
        return CheckResult(name, "skip", "reason=AZIZAM_API_KEY not set")
    if not cfg.live_embeddings:
        return CheckResult(
            name,
            "skip",
            "reason=AZIZAM_SMOKE_LIVE_EMBEDDINGS not set",
        )
    status, err = _request(
        base_url=cfg.base_url,
        path=path,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        body=json.dumps(payload).encode("utf-8"),
        timeout=cfg.timeout_s,
    )
    if status == 200:
        return CheckResult(name, "pass", f"status={status}")
    return CheckResult(name, "fail", f"status={status} {err}".strip())


def check_openai_embeddings(cfg: SmokeConfig) -> CheckResult:
    return _check_embedding_post(
        cfg,
        name="openai.embeddings",
        path="/v1/embeddings",
        payload={"model": cfg.embeddings_model, "input": TINY_PROMPT},
    )


def check_ollama_embed(cfg: SmokeConfig) -> CheckResult:
    return _check_embedding_post(
        cfg,
        name="ollama.embed",
        path="/api/embed",
        payload={"model": cfg.embeddings_model, "input": TINY_PROMPT},
    )


def check_ollama_embeddings(cfg: SmokeConfig) -> CheckResult:
    return _check_embedding_post(
        cfg,
        name="ollama.embeddings",
        path="/api/embeddings",
        payload={"model": cfg.embeddings_model, "prompt": TINY_PROMPT},
    )


CHECKS: Tuple[Callable[[SmokeConfig], CheckResult], ...] = (
    check_health,
    check_capabilities,
    check_anthropic_messages,
    check_openai_embeddings,
    check_ollama_tags,
    check_ollama_chat,
    check_ollama_embed,
    check_ollama_embeddings,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(cfg: SmokeConfig) -> List[CheckResult]:
    return [check(cfg) for check in CHECKS]


def _summarize(results: List[CheckResult]) -> Tuple[int, int, int]:
    passed = sum(1 for r in results if r.status == "pass")
    skipped = sum(1 for r in results if r.status == "skip")
    failed = sum(1 for r in results if r.status == "fail")
    return passed, skipped, failed


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local protocol-frontend smoke checks (no third-party SDKs).",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print resolved config (with API key redacted) and exit.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    cfg = SmokeConfig.from_env()

    if args.print_config:
        print(f"base_url        = {cfg.base_url or '(unset)'}")
        print(f"api_key         = {'set' if cfg.api_key else '(unset)'}")
        print(f"live_llm        = {cfg.live_llm}")
        print(f"live_embeddings = {cfg.live_embeddings}")
        print(f"timeout_s       = {cfg.timeout_s}")
        print(f"anthropic_model = {cfg.anthropic_model}")
        print(f"ollama_model    = {cfg.ollama_model}")
        print(f"embeddings_model = {cfg.embeddings_model}")
        return 0

    results = run(cfg)
    for r in results:
        print(r.format_row())
    passed, skipped, failed = _summarize(results)
    print(f"-- pass={passed} skip={skipped} fail={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
