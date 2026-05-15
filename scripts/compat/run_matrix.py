#!/usr/bin/env python3
"""Run available SDK compatibility probes and emit a matrix JSON.

This is intentionally minimal: the scaffold ships with a registry of
known probe modules, each of which is *optional*. A probe is included
when:

- its SDK dependency is importable, AND
- ``OPENAI_PROXY_BASE_URL`` (and ``OPENAI_PROXY_API_KEY`` unless the
  probe is anonymous) are set.

Probes that fail either check are recorded as ``skipped`` rather than
silently omitted, so the matrix always documents the full case grid.

Usage::

    scripts/compat/run_matrix.py --help
    scripts/compat/run_matrix.py --dry-run
    scripts/compat/run_matrix.py --out tests/compat/matrix/compatibility.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.compat.harness import (  # noqa: E402
    ConfigError,
    HarnessConfig,
    ProbeResult,
    ProtocolMatrix,
    Result,
    build_matrix,
    load_config,
    matrix_to_json,
)
from tests.compat.harness.matrix import validate_matrix_dict  # noqa: E402


@dataclass(frozen=True)
class ProbeSpec:
    """Static description of a probe registered with the CLI."""

    protocol: str
    sdk: str
    module: str
    sdk_dependency: Optional[str]
    cases: tuple[str, ...]
    allow_anonymous: bool = False


PROBES: tuple[ProbeSpec, ...] = (
    ProbeSpec(
        protocol="openai",
        sdk="openai-python",
        module="tests.compat.python.probe_openai",
        sdk_dependency="openai",
        cases=("client.construct", "models.list"),
    ),
    ProbeSpec(
        protocol="anthropic",
        sdk="anthropic-python",
        module="tests.compat.python.probe_anthropic",
        sdk_dependency="anthropic",
        cases=("client.construct",),
    ),
    ProbeSpec(
        protocol="ollama",
        sdk="ollama-python",
        module="tests.compat.python.probe_ollama",
        sdk_dependency="ollama",
        cases=("client.construct", "tags.list"),
        allow_anonymous=True,
    ),
)


def _sdk_version(dep: Optional[str]) -> str:
    if not dep:
        return "n/a"
    try:
        mod = importlib.import_module(dep)
    except ImportError:
        return "missing"
    return getattr(mod, "__version__", "unknown")


def _skip_all(
    spec: ProbeSpec, *, reason: str, gateway_version: str
) -> ProtocolMatrix:
    return build_matrix(
        protocol=spec.protocol,
        sdk=spec.sdk,
        sdk_version=_sdk_version(spec.sdk_dependency),
        gateway_version=gateway_version,
        probes=[
            ProbeResult(case=case, result=Result.SKIPPED, note=reason)
            for case in spec.cases
        ],
    )


def _gateway_version() -> str:
    # Pre-1.0 — there is no shipped version string yet, so we emit the
    # git-style label "local" and let consumers tag the matrix from CI.
    return "local"


def collect_matrices(
    *,
    cfg_loader: Callable[..., HarnessConfig] = load_config,
    probe_specs: tuple[ProbeSpec, ...] = PROBES,
) -> List[ProtocolMatrix]:
    """Build a matrix entry for each registered probe.

    Probes are not executed live by this function — that runs in the
    unittest layer. This function records availability so the matrix
    reflects what *could* run in the current environment.
    """

    gateway_version = _gateway_version()
    matrices: List[ProtocolMatrix] = []
    for spec in probe_specs:
        # Env check
        try:
            cfg_loader(allow_anonymous=spec.allow_anonymous)
        except ConfigError as exc:
            matrices.append(
                _skip_all(spec, reason=str(exc), gateway_version=gateway_version)
            )
            continue
        # SDK availability check
        if spec.sdk_dependency:
            try:
                importlib.import_module(spec.sdk_dependency)
            except ImportError:
                matrices.append(
                    _skip_all(
                        spec,
                        reason=f"{spec.sdk_dependency} SDK not installed",
                        gateway_version=gateway_version,
                    )
                )
                continue
        # Module-importability check (without executing test cases)
        try:
            importlib.import_module(spec.module)
        except Exception as exc:  # pragma: no cover - defensive
            matrices.append(
                _skip_all(
                    spec,
                    reason=f"probe module failed to import: {exc!r}",
                    gateway_version=gateway_version,
                )
            )
            continue

        # Scaffold: mark every case ``skipped`` with a pointer to the
        # unittest invocation. Future slices flip these to pass/fail by
        # actually calling the probe.
        matrices.append(
            build_matrix(
                protocol=spec.protocol,
                sdk=spec.sdk,
                sdk_version=_sdk_version(spec.sdk_dependency),
                gateway_version=gateway_version,
                probes=[
                    ProbeResult(
                        case=case,
                        result=Result.SKIPPED,
                        note=(
                            "scaffold: run "
                            f"'python -m unittest {spec.module}' to exercise"
                        ),
                    )
                    for case in spec.cases
                ],
            )
        )
    return matrices


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See tests/compat/README.md for setup details.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write matrix JSON to this path. Default: stdout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the probe registry and resolved env without writing output.",
    )
    parser.add_argument(
        "--list-probes",
        action="store_true",
        help="List registered probes and exit.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    if args.list_probes:
        for spec in PROBES:
            print(f"{spec.protocol}\t{spec.sdk}\t{spec.module}")
        return 0

    if args.dry_run:
        try:
            cfg = load_config(allow_anonymous=True)
            print(f"base_url   = {cfg.base_url}")
            print(f"auth       = {'set' if cfg.api_key else 'anonymous'}")
            print(f"timeout_s  = {cfg.request_timeout_s}")
        except ConfigError as exc:
            print(f"env        = unset ({exc})")
        print("registered probes:")
        for spec in PROBES:
            installed = "yes" if _sdk_version(spec.sdk_dependency) not in {
                "missing",
                "n/a",
            } else "no"
            print(
                f"  - {spec.protocol}/{spec.sdk}  "
                f"sdk_installed={installed}  cases={len(spec.cases)}"
            )
        return 0

    matrices = collect_matrices()
    # Validate each matrix against the documented schema before writing.
    for m in matrices:
        validate_matrix_dict(m.to_dict())
    payload = matrix_to_json(matrices)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
        # Round-trip self-check
        roundtrip = json.loads(args.out.read_text(encoding="utf-8"))
        for entry in roundtrip:
            validate_matrix_dict(entry)
        print(f"wrote {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
