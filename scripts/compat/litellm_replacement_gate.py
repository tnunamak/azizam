#!/usr/bin/env python3
"""Run LiteLLM replacement gates for the commodity LLM layer.

This script is intentionally stdlib-only. It does not import LiteLLM and
is safe for the normal unit-test environment. Live gates run only when:

    LITELLM_BASE_URL   root URL, for example http://127.0.0.1:4000
    LITELLM_API_KEY    bearer token for the LiteLLM proxy

Optional model overrides are declared in tests/compat/litellm_gate_matrix.json.

Failure report format:

    [FAIL] openai.chat status=500 expected=200 detail=http_error: Internal Server Error

Use --json for a machine-readable report with one object per gate.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = REPO_ROOT / "tests" / "compat" / "litellm_gate_matrix.json"
DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Gate:
    id: str
    label: str
    protocol: str
    method: str
    path: str
    request: Mapping[str, Any]
    expect: Mapping[str, Any]
    headers: Mapping[str, str]
    model_env: Optional[str]
    default_model: Optional[str]
    caveats: Tuple[str, ...]


@dataclass(frozen=True)
class Config:
    base_url: Optional[str]
    api_key: Optional[str]
    timeout_s: float
    models: Mapping[str, str]


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    error: str = ""


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    label: str
    status: str
    detail: str
    http_status: Optional[int] = None
    caveats: Tuple[str, ...] = ()

    def format_row(self) -> str:
        tag = self.status.upper()
        bits = [f"[{tag}]", f"{self.gate_id:<28}"]
        if self.http_status is not None:
            bits.append(f"status={self.http_status}")
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "gate": self.gate_id,
            "label": self.label,
            "result": self.status,
            "detail": self.detail,
        }
        if self.http_status is not None:
            data["http_status"] = self.http_status
        if self.caveats:
            data["caveats"] = list(self.caveats)
        return data


def load_gates(path: Path = DEFAULT_MATRIX) -> List[Gate]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    gates = raw.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ValueError("LiteLLM gate matrix must contain a non-empty 'gates' list")

    parsed: List[Gate] = []
    seen: set[str] = set()
    for item in gates:
        if not isinstance(item, Mapping):
            raise ValueError("each LiteLLM gate must be an object")
        gate_id = _required_str(item, "id")
        if gate_id in seen:
            raise ValueError(f"duplicate LiteLLM gate id: {gate_id}")
        seen.add(gate_id)
        parsed.append(
            Gate(
                id=gate_id,
                label=_required_str(item, "label"),
                protocol=_required_str(item, "protocol"),
                method=_required_str(item, "method").upper(),
                path=_required_str(item, "path"),
                request=_optional_mapping(item, "request"),
                expect=_optional_mapping(item, "expect"),
                headers=_string_mapping(item.get("headers", {}), "headers"),
                model_env=_optional_str(item, "model_env"),
                default_model=_optional_str(item, "default_model"),
                caveats=tuple(str(c) for c in item.get("caveats", ())),
            )
        )
    return parsed


def config_from_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    gates: Iterable[Gate] = (),
) -> Config:
    env = os.environ if env is None else env
    base_url = env.get("LITELLM_BASE_URL", "").strip().rstrip("/") or None
    api_key = env.get("LITELLM_API_KEY", "").strip() or None
    try:
        timeout_s = float(env.get("LITELLM_TIMEOUT_S", "").strip() or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    if timeout_s <= 0:
        timeout_s = DEFAULT_TIMEOUT_S

    models: Dict[str, str] = {}
    for gate in gates:
        if not gate.model_env:
            continue
        override = env.get(gate.model_env, "").strip()
        if override:
            models[gate.model_env] = override
        elif gate.default_model:
            models[gate.model_env] = gate.default_model
    return Config(base_url=base_url, api_key=api_key, timeout_s=timeout_s, models=models)


def run_gate(gate: Gate, cfg: Config) -> GateResult:
    if not cfg.base_url:
        return GateResult(
            gate.id,
            gate.label,
            "skip",
            "reason=LITELLM_BASE_URL not set",
            caveats=gate.caveats,
        )
    if not cfg.api_key:
        return GateResult(
            gate.id,
            gate.label,
            "skip",
            "reason=LITELLM_API_KEY not set",
            caveats=gate.caveats,
        )

    request_body: Optional[bytes] = None
    if gate.method != "GET":
        payload = inject_model(gate.request, gate, cfg)
        request_body = json.dumps(payload).encode("utf-8")

    result = http_request(
        base_url=cfg.base_url,
        path=gate.path,
        method=gate.method,
        api_key=cfg.api_key,
        headers=gate.headers,
        body=request_body,
        timeout_s=cfg.timeout_s,
    )
    return evaluate_http_result(gate, result)


def inject_model(payload: Mapping[str, Any], gate: Gate, cfg: Config) -> Dict[str, Any]:
    copied = dict(payload)
    if gate.model_env:
        model = cfg.models.get(gate.model_env)
        if model:
            copied.setdefault("model", model)
    return copied


def http_request(
    *,
    base_url: str,
    path: str,
    method: str,
    api_key: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout_s: float,
) -> HttpResult:
    req = urllib.request.Request(
        url=f"{base_url}{path}",
        method=method,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return HttpResult(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(1024 * 1024),
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            status=exc.code,
            headers={k.lower(): v for k, v in exc.headers.items()},
            body=exc.read(8192),
            error=f"http_error: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return HttpResult(
            status=0,
            headers={},
            body=b"",
            error=f"url_error: {exc.reason!r}",
        )
    except TimeoutError:
        return HttpResult(status=0, headers={}, body=b"", error="timeout")
    except OSError as exc:
        return HttpResult(
            status=0,
            headers={},
            body=b"",
            error=f"os_error: {exc.__class__.__name__}",
        )


def evaluate_http_result(gate: Gate, result: HttpResult) -> GateResult:
    expected = int(gate.expect.get("status", 200))
    if result.status != expected:
        detail = f"expected={expected}"
        if result.error:
            detail += f" detail={result.error}"
        return GateResult(gate.id, gate.label, "fail", detail, result.status, gate.caveats)

    if gate.expect.get("stream_has_data") or gate.expect.get("stream_has_done"):
        return evaluate_stream_gate(gate, result)

    try:
        body = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return GateResult(
            gate.id,
            gate.label,
            "fail",
            f"invalid_json={exc.__class__.__name__}",
            result.status,
            gate.caveats,
        )

    missing = [
        p
        for p in gate.expect.get("json_path_exists", ())
        if resolve_path(body, p) is None
    ]
    if missing:
        return GateResult(
            gate.id,
            gate.label,
            "fail",
            "missing_json_path=" + ",".join(missing),
            result.status,
            gate.caveats,
        )

    any_paths = list(gate.expect.get("json_path_exists_any", ()))
    if any_paths and not any(resolve_path(body, p) is not None for p in any_paths):
        return GateResult(
            gate.id,
            gate.label,
            "fail",
            "missing_any_json_path=" + ",".join(any_paths),
            result.status,
            gate.caveats,
        )

    return GateResult(gate.id, gate.label, "pass", "ok", result.status, gate.caveats)


def evaluate_stream_gate(gate: Gate, result: HttpResult) -> GateResult:
    text = result.body.decode("utf-8", errors="replace")
    has_data = any(line.startswith("data: ") for line in text.splitlines())
    has_done = "[DONE]" in text
    if gate.expect.get("stream_has_data") and not has_data:
        return GateResult(
            gate.id,
            gate.label,
            "fail",
            "stream_missing_data_frame",
            result.status,
            gate.caveats,
        )
    if gate.expect.get("stream_has_done") and not has_done:
        return GateResult(
            gate.id,
            gate.label,
            "fail",
            "stream_missing_done_frame",
            result.status,
            gate.caveats,
        )
    return GateResult(gate.id, gate.label, "pass", "ok", result.status, gate.caveats)


def resolve_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, Mapping):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def summarize(results: Iterable[GateResult]) -> Tuple[int, int, int]:
    rows = list(results)
    return (
        sum(1 for r in rows if r.status == "pass"),
        sum(1 for r in rows if r.status == "skip"),
        sum(1 for r in rows if r.status == "fail"),
    )


def _required_str(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LiteLLM gate missing required string field {key!r}")
    return value


def _optional_str(item: Mapping[str, Any], key: str) -> Optional[str]:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"LiteLLM gate field {key!r} must be a string when present")
    return value


def _optional_mapping(item: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = item.get(key, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"LiteLLM gate field {key!r} must be an object")
    return value


def _string_mapping(value: Any, key: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"LiteLLM gate field {key!r} must be an object")
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"LiteLLM gate field {key!r} must map strings to strings")
        out[k] = v
    return out


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run LiteLLM commodity-layer replacement gates."
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=DEFAULT_MATRIX,
        help="Gate matrix JSON path.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report.")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print resolved non-secret config and exit.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    gates = load_gates(args.matrix)
    cfg = config_from_env(gates=gates)

    if args.print_config:
        print(f"base_url  = {cfg.base_url or '(unset)'}")
        print(f"api_key   = {'set' if cfg.api_key else '(unset)'}")
        print(f"timeout_s = {cfg.timeout_s}")
        for key in sorted(cfg.models):
            print(f"{key} = {cfg.models[key]}")
        return 0

    results = [run_gate(gate, cfg) for gate in gates]
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
    else:
        for result in results:
            print(result.format_row())
            for caveat in result.caveats:
                print(f"       caveat={caveat}")
        passed, skipped, failed = summarize(results)
        print(f"-- pass={passed} skip={skipped} fail={failed}")
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
