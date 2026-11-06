#!/usr/bin/env python3
"""Safe local diagnostics for the Azizam gateway.

The doctor deliberately avoids local key stores. Auth-related diagnostics only
report whether relevant environment variables are present, with values redacted.
"""

from __future__ import annotations


import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.compat import get_env  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.manifests import ManifestError, bind_backend_request, load_workflow_manifest, validate_inputs  # noqa: E402


MANIFESTS_DIR = ROOT / "manifests" / "workflows"
CONFIG_PATH = ROOT / "proxy_config.yml"
AUTH_ENV_NAMES = (
    "OPENAI_API_KEY",
    "AZIZAM_API_KEY",
    "AZIZAM_ADMIN_TOKEN",
    "ACCOUNT_ADMIN_API_TOKEN",
)


@dataclass
class CheckResult:
    name: str
    status: str
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def ok(name: str, message: str, **details: Any) -> CheckResult:
    return CheckResult(name, "ok", message, details or None)


def warn(name: str, message: str, **details: Any) -> CheckResult:
    return CheckResult(name, "warn", message, details or None)


def fail(name: str, message: str, **details: Any) -> CheckResult:
    return CheckResult(name, "fail", message, details or None)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return loaded if isinstance(loaded, dict) else {}


def check_manifest_registry(manifests_dir: Path = MANIFESTS_DIR) -> CheckResult:
    try:
        registry = WorkflowRegistry.from_directory(manifests_dir)
    except Exception as exc:  # noqa: BLE001
        return fail("manifest_registry", "registry could not load", error=str(exc))

    if registry.errors:
        return fail(
            "manifest_registry",
            "registry loaded with manifest errors",
            manifest_count=len(registry.records()),
            errors=[
                {"source": str(error.source_path.relative_to(ROOT)), "message": error.message}
                for error in registry.errors
            ],
        )
    return ok(
        "manifest_registry",
        "registry loaded",
        manifest_count=len(registry.records()),
        workflow_ids=registry.ids(),
    )


def check_workflow_bindings(manifests_dir: Path = MANIFESTS_DIR) -> CheckResult:
    errors: list[dict[str, str]] = []
    checked = 0

    for manifest_path in sorted(manifests_dir.glob("*.y*ml")):
        try:
            manifest = load_workflow_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001
            errors.append({"manifest": _rel(manifest_path), "message": str(exc)})
            continue

        bindings = manifest.get("bindings") or {}
        if not isinstance(bindings, dict):
            errors.append({"manifest": _rel(manifest_path), "message": "bindings must be a mapping"})
            continue

        for backend, binding in sorted(bindings.items()):
            if not isinstance(binding, dict):
                errors.append(
                    {
                        "manifest": _rel(manifest_path),
                        "backend": str(backend),
                        "message": "binding must be a mapping",
                    }
                )
                continue
            workflow_ref = binding.get("workflow")
            if not workflow_ref:
                checked += 1
                continue
            workflow_path = (ROOT / str(workflow_ref)).resolve()
            if not _inside_root(workflow_path) or not workflow_path.exists():
                errors.append(
                    {
                        "manifest": _rel(manifest_path),
                        "backend": str(backend),
                        "message": f"workflow artifact not found: {workflow_ref}",
                    }
                )
                continue
            try:
                workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
                sample_inputs = _sample_inputs_for_manifest(manifest)
                result = validate_inputs(manifest, sample_inputs)
                if result.rejected:
                    raise ManifestError(f"sample inputs rejected: {result.rejected}")
                bind_backend_request(manifest, str(backend), workflow, result.accepted)
                checked += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manifest": _rel(manifest_path),
                        "backend": str(backend),
                        "workflow": str(workflow_ref),
                        "message": str(exc),
                    }
                )

    if errors:
        return fail("workflow_bindings", "committed workflow bindings have shape errors", errors=errors)
    return ok("workflow_bindings", "committed workflow bindings match artifacts", checked=checked)


def check_http_json(name: str, base_url: str | None, path: str, timeout: float) -> CheckResult:
    if not base_url:
        return warn(name, "skipped; no base URL provided")
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        status, payload = fetch_json(url, timeout)
    except Exception as exc:  # noqa: BLE001
        return fail(name, "request failed", url=url, error=str(exc))
    if status < 200 or status >= 300:
        return fail(name, "unexpected status", url=url, status_code=status)
    if not isinstance(payload, dict):
        return fail(name, "response was not a JSON object", url=url, status_code=status)
    return ok(name, "endpoint returned JSON", url=url, status_code=status, keys=sorted(payload.keys()))


def check_auth_env(env: dict[str, str] | None = None) -> CheckResult:
    source = env if env is not None else os.environ
    present = {name: "<redacted>" for name in AUTH_ENV_NAMES if get_env(name, environ=source)}
    if present:
        return ok("auth_env", "auth environment variables are present", variables=present)
    return warn("auth_env", "no auth environment variables detected")


def default_comfy_url(config: dict[str, Any]) -> str | None:
    if os.environ.get("COMFY_HOST"):
        return os.environ["COMFY_HOST"]
    if os.environ.get("COMFY_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return None
    comfy_cfg = (config.get("backends") or {}).get("comfyui") or {}
    if not comfy_cfg:
        return None
    host = comfy_cfg.get("host", "127.0.0.1")
    port = comfy_cfg.get("port", 7890)
    return f"http://{host}:{port}"


def run_doctor(
    *,
    base_url: str | None = None,
    comfy_url: str | None = None,
    timeout: float = 3.0,
    include_comfy: bool = True,
) -> dict[str, Any]:
    config = load_config()
    resolved_comfy_url = comfy_url if comfy_url is not None else default_comfy_url(config)
    checks = [
        check_manifest_registry(),
        check_workflow_bindings(),
        check_auth_env(),
        check_http_json("public_health", base_url, "/health", timeout),
        check_http_json("well_known_azizam", base_url, "/.well-known/azizam", timeout),
    ]
    if include_comfy:
        checks.append(check_http_json("comfyui_system_stats", resolved_comfy_url, "/system_stats", timeout))

    overall = "ok"
    if any(check.status == "fail" for check in checks):
        overall = "fail"
    elif any(check.status == "warn" for check in checks):
        overall = "warn"
    return {"status": overall, "checks": [check.to_dict() for check in checks]}


def fetch_json(url: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(1024 * 1024)
        text = body.decode(response.headers.get_content_charset() or "utf-8")
        return response.status, json.loads(text)


def render_text(report: dict[str, Any]) -> str:
    lines = [f"doctor: {report['status']}"]
    for check in report["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['message']}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Proxy base URL for public endpoint probes")
    parser.add_argument("--comfy-url", help="ComfyUI base URL for /system_stats probe")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout in seconds")
    parser.add_argument("--no-comfy", action="store_true", help="Skip ComfyUI /system_stats")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report = run_doctor(
        base_url=args.base_url,
        comfy_url=args.comfy_url,
        timeout=args.timeout,
        include_comfy=not args.no_comfy,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 1 if report["status"] == "fail" else 0


def _sample_inputs_for_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    samples: dict[str, Any] = {}
    for name, spec in (manifest.get("inputs") or {}).items():
        if not isinstance(spec, dict):
            continue
        if "default" in spec:
            samples[name] = spec["default"]
            continue
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            samples[name] = enum[0]
            continue
        input_type = spec.get("type")
        if input_type == "number":
            samples[name] = _sample_number(spec)
        elif input_type == "integer":
            samples[name] = int(_sample_number(spec))
        elif input_type == "boolean":
            samples[name] = True
        elif input_type == "array":
            samples[name] = []
        elif input_type == "object":
            samples[name] = {}
        else:
            samples[name] = f"doctor-{name}"
    return samples


def _sample_number(spec: dict[str, Any]) -> float:
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        return (minimum + maximum) / 2
    if isinstance(minimum, (int, float)):
        return minimum
    if isinstance(maximum, (int, float)):
        return maximum
    return 1


def _inside_root(path: Path) -> bool:
    try:
        path.relative_to(ROOT)
        return True
    except ValueError:
        return False


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
