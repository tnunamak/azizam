#!/usr/bin/env python3
"""Run available SDK compatibility probes and emit a matrix JSON.

The CLI ships with a registry of known probe modules, each of which is
*optional*. A probe is included when:

- its SDK dependency is importable, AND
- ``OPENAI_PROXY_BASE_URL`` (and ``OPENAI_PROXY_API_KEY`` unless the
  probe is anonymous) are set.

When both checks pass, this script loads the probe module's unittest
TestCase and executes the test methods that back each declared case,
mapping unittest outcomes to ``pass`` / ``fail`` / ``skipped``. Probes
whose SDK or env are missing are recorded as ``skipped`` rather than
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
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
    """Static description of a probe registered with the CLI.

    ``case_map`` maps each declared case to the unittest method on the
    probe's TestCase class that backs it. The runner executes those
    methods and maps the unittest outcome to a ``Result``.
    """

    protocol: str
    sdk: str
    module: str
    test_class: str
    sdk_dependency: Optional[str]
    case_map: Tuple[Tuple[str, str], ...]
    allow_anonymous: bool = False

    @property
    def cases(self) -> Tuple[str, ...]:
        return tuple(case for case, _ in self.case_map)


PROBES: Tuple[ProbeSpec, ...] = (
    ProbeSpec(
        protocol="openai",
        sdk="raw-http",
        module="tests.compat.python.probe_gateway_contract",
        test_class="TestGatewayContractHTTP",
        sdk_dependency=None,
        case_map=(
            ("chat.visible_content", "test_chat_completion_has_visible_content"),
            (
                "chat.streaming_content_delta",
                "test_chat_stream_has_content_delta_and_done_frame",
            ),
            ("chat.malformed_json_400", "test_malformed_json_returns_400_error_object"),
            (
                "chat.missing_model_rejected",
                "test_missing_model_is_rejected_before_default_routing",
            ),
            ("chat.unknown_model_rejected", "test_unknown_model_is_rejected"),
            (
                "anthropic.messages.text_content",
                "test_anthropic_messages_has_text_content_block",
            ),
            (
                "audio.transcriptions.error_shape",
                "test_audio_transcription_empty_body_error_is_openai_shaped",
            ),
            (
                "audio.translations.unsupported_error_shape",
                "test_audio_translation_empty_body_error_is_openai_shaped_unsupported",
            ),
            (
                "chat.validation_error_envelope",
                "test_chat_non_object_body_returns_protocol_shaped_400",
            ),
            (
                "responses.hosted_tool_rejected",
                "test_responses_hosted_tool_returns_protocol_shaped_400",
            ),
            (
                "audio.speech.unsupported_response_format",
                "test_audio_speech_unsupported_response_format_is_openai_shaped",
            ),
        ),
    ),
    ProbeSpec(
        protocol="openai",
        sdk="openai-python",
        module="tests.compat.python.probe_openai",
        test_class="TestOpenAIPythonSDK",
        sdk_dependency="openai",
        case_map=(
            ("client.construct", "test_client_construct"),
            ("models.list", "test_models_list"),
            ("embeddings.create", "test_embeddings_create"),
            ("chat.completions.create", "test_chat_completions_create"),
            ("chat.completions.streaming", "test_chat_completions_streaming"),
            (
                "chat.completions.tool_call_envelope",
                "test_chat_completions_tool_call_envelope",
            ),
            (
                "chat.completions.json_response_format",
                "test_chat_completions_json_response_format",
            ),
            (
                "chat.completions.image_input_block",
                "test_chat_completions_image_input_block",
            ),
        ),
    ),
    ProbeSpec(
        protocol="openai",
        sdk="openai-python-responses-media",
        module="tests.compat.python.probe_openai_responses_media",
        test_class="TestOpenAIResponsesMediaSurfaces",
        sdk_dependency="openai",
        case_map=(
            ("responses.resource", "test_responses_resource_exists"),
            ("images.resource", "test_image_resource_exists"),
            ("audio.resource", "test_audio_resources_exist"),
            ("videos.resource", "test_video_resource_exists_when_sdk_supports_it"),
        ),
        allow_anonymous=True,
    ),
    ProbeSpec(
        protocol="openai",
        sdk="openai-python-responses-media-live",
        module="tests.compat.python.probe_openai_responses_media",
        test_class="TestOpenAIResponsesMediaLive",
        sdk_dependency="openai",
        case_map=(
            ("responses.create", "test_responses_create"),
            ("images.generate", "test_images_generate"),
            ("audio.speech.create", "test_audio_speech_create"),
            ("videos.create", "test_video_create"),
        ),
    ),
    ProbeSpec(
        protocol="anthropic",
        sdk="anthropic-python",
        module="tests.compat.python.probe_anthropic",
        test_class="TestAnthropicPythonSDK",
        sdk_dependency="anthropic",
        case_map=(
            ("client.construct", "test_client_construct"),
            ("models.list", "test_models_list"),
            ("messages.count_tokens", "test_messages_count_tokens"),
            (
                "messages.count_tokens.image_block",
                "test_messages_count_tokens_image_block",
            ),
            ("messages.create", "test_messages_create"),
            ("messages.create.image_block", "test_messages_create_image_block"),
            ("messages.streaming", "test_messages_streaming"),
            (
                "messages.create.system_prompt",
                "test_messages_create_system_prompt",
            ),
            ("messages.create.tool_use", "test_messages_create_tool_use"),
            (
                "messages.create.max_tokens_validation",
                "test_messages_create_max_tokens_validation",
            ),
            (
                "messages.streaming.event_names",
                "test_messages_streaming_event_names",
            ),
            (
                "messages.create.thinking_unsupported",
                "test_messages_create_thinking_unsupported",
            ),
            (
                "messages.create.unknown_model",
                "test_messages_create_unknown_model",
            ),
        ),
    ),
    ProbeSpec(
        protocol="ollama",
        sdk="ollama-python",
        module="tests.compat.python.probe_ollama",
        test_class="TestOllamaPythonSDK",
        sdk_dependency="ollama",
        case_map=(
            ("client.construct", "test_client_construct"),
            ("tags.list", "test_tags_list"),
            ("embed.current", "test_embed_current_route"),
            ("embeddings.legacy", "test_embeddings_legacy_route"),
            ("show.model", "test_show_model"),
            ("ps.list", "test_ps_list"),
            ("chat.non_streaming", "test_chat_nonstream"),
            ("chat.streaming", "test_chat_stream"),
            ("generate.non_streaming", "test_generate_nonstream"),
            ("generate.streaming", "test_generate_stream"),
            ("pull.unsupported", "test_pull_returns_unsupported_error"),
            ("delete.unsupported", "test_delete_returns_unsupported_error"),
            ("push.unsupported", "test_push_returns_unsupported_error"),
        ),
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


def _format_failure(entry: Tuple[unittest.TestCase, str]) -> str:
    """Reduce a unittest (test, traceback_string) entry to a one-liner."""

    _test, tb = entry
    # The last non-blank line of a traceback is the exception summary
    # (e.g. ``AssertionError: 1 != 2``). That is the most useful note
    # for a matrix consumer.
    for line in reversed(tb.strip().splitlines()):
        line = line.strip()
        if line:
            return line
    return "error"


def _run_one(suite: unittest.TestSuite) -> Tuple[Result, Optional[str]]:
    """Run a single-test suite and reduce its outcome to a Result."""

    result = unittest.TestResult()
    suite.run(result)
    if result.skipped:
        _test, reason = result.skipped[0]
        return Result.SKIPPED, reason or "skipped"
    if result.errors:
        return Result.FAIL, _format_failure(result.errors[0])
    if result.failures:
        return Result.FAIL, _format_failure(result.failures[0])
    return Result.PASS, None


ProbeRunner = Callable[[ProbeSpec], List[ProbeResult]]


def execute_probe(spec: ProbeSpec) -> List[ProbeResult]:
    """Default executor: load the probe's TestCase and run each case.

    Each case maps to a single test method. The TestCase is instantiated per
    case so a single ``setUp`` failure does not poison sibling cases' results.
    """

    module = importlib.import_module(spec.module)
    try:
        test_cls = getattr(module, spec.test_class)
    except AttributeError as exc:
        reason = f"probe module missing TestCase {spec.test_class!r}: {exc}"
        return [
            ProbeResult(case=case, result=Result.FAIL, note=reason)
            for case in spec.cases
        ]

    if not isinstance(test_cls, type) or not issubclass(test_cls, unittest.TestCase):
        reason = f"{spec.test_class!r} is not a unittest.TestCase"
        return [
            ProbeResult(case=case, result=Result.FAIL, note=reason)
            for case in spec.cases
        ]

    probes: List[ProbeResult] = []
    for case, method in spec.case_map:
        if not hasattr(test_cls, method):
            probes.append(
                ProbeResult(
                    case=case,
                    result=Result.FAIL,
                    note=f"test method {method!r} not found on {spec.test_class}",
                )
            )
            continue
        suite = unittest.TestSuite([test_cls(method)])
        outcome, note = _run_one(suite)
        probes.append(ProbeResult(case=case, result=outcome, note=note))
    return probes


def collect_matrices(
    *,
    cfg_loader: Callable[..., HarnessConfig] = load_config,
    probe_specs: Tuple[ProbeSpec, ...] = PROBES,
    runner: ProbeRunner = execute_probe,
) -> List[ProtocolMatrix]:
    """Build a matrix entry for each registered probe.

    When the SDK + env preconditions hold, ``runner`` executes the
    probe's test cases and the matrix records pass/fail/skipped. When
    they do not hold, every case is marked skipped with the reason.
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
        # Module-importability check (probe imports the SDK at top-level)
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

        try:
            probes = runner(spec)
        except Exception as exc:  # defensive: a runner crash becomes fail
            probes = [
                ProbeResult(
                    case=case,
                    result=Result.FAIL,
                    note=f"runner crashed: {exc!r}",
                )
                for case in spec.cases
            ]

        matrices.append(
            build_matrix(
                protocol=spec.protocol,
                sdk=spec.sdk,
                sdk_version=_sdk_version(spec.sdk_dependency),
                gateway_version=gateway_version,
                probes=probes,
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
        help="Print the probe registry and resolved env without executing probes.",
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
            installed = (
                "n/a"
                if spec.sdk_dependency is None
                else "yes"
                if _sdk_version(spec.sdk_dependency) != "missing"
                else "no"
            )
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
