#!/usr/bin/env python3
"""Benchmark OpenAI-compatible local LLM endpoints.

This is intentionally small and dependency-free so it can run against either
llama.cpp directly or the proxy route during local model experiments.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_PROMPT = """\
Write a concise engineering review of this proposed local AI gateway design.

The gateway exposes OpenAI-compatible chat, image, audio, and video APIs while
routing to self-hosted engines. It should support model discovery, capability
manifests, workflow-specific extensions, credential provisioning, local policy,
and receipts. The design should stay simple in the Rich Hickey sense: separate
stable semantics from incidental engine mechanics, avoid complecting discovery
with dispatch, and keep extension points explicit rather than magical.

Assess the design in practical terms. Cover likely failure modes, operational
tradeoffs, and what sophisticated local-model users would expect before trusting
it. Keep the answer grounded and specific.
"""


@dataclass
class RunResult:
    label: str
    run: int
    status: str
    wall_s: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    prompt_tps: float | None
    generation_tps: float | None
    draft_tokens: int | None
    draft_tokens_accepted: int | None
    content_preview: str
    error: str | None = None

    @property
    def wall_completion_tps(self) -> float | None:
        if not self.completion_tokens or self.wall_s <= 0:
            return None
        return self.completion_tokens / self.wall_s


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def one_run(
    *,
    label: str,
    run: int,
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    disable_thinking: bool,
    extra_payload: dict[str, Any],
) -> RunResult:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "timings_per_token": True,
    }
    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    payload.update(extra_payload)

    start = time.perf_counter()
    try:
        data = post_json(url, payload, timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return RunResult(
            label=label,
            run=run,
            status="error",
            wall_s=time.perf_counter() - start,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            prompt_tps=None,
            generation_tps=None,
            draft_tokens=None,
            draft_tokens_accepted=None,
            content_preview="",
            error=str(e),
        )

    wall_s = time.perf_counter() - start
    usage = data.get("usage") or {}
    timings = data.get("timings") or {}
    choices = data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = str(message.get("content") or "").replace("\n", " ")

    return RunResult(
        label=label,
        run=run,
        status="ok",
        wall_s=wall_s,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        prompt_tps=timings.get("prompt_per_second"),
        generation_tps=timings.get("predicted_per_second"),
        draft_tokens=timings.get("draft_n"),
        draft_tokens_accepted=timings.get("draft_n_accepted"),
        content_preview=content[:120],
    )


def mean(values: list[float | int | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return statistics.fmean(clean)


def fmt(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def print_row(result: RunResult) -> None:
    accepted = "-"
    if result.draft_tokens and result.draft_tokens_accepted is not None:
        accepted = f"{result.draft_tokens_accepted}/{result.draft_tokens}"
    print(
        "\t".join(
            [
                result.label,
                str(result.run),
                result.status,
                fmt(result.wall_s),
                str(result.prompt_tokens or "-"),
                str(result.completion_tokens or "-"),
                fmt(result.prompt_tps),
                fmt(result.generation_tps),
                fmt(result.wall_completion_tps),
                accepted,
                result.error or result.content_preview,
            ]
        ),
        flush=True,
    )


def summarize(label: str, results: list[RunResult]) -> None:
    ok = [r for r in results if r.status == "ok"]
    if not ok:
        print(f"\n{label}: no successful runs", file=sys.stderr)
        return

    draft_generated = sum(r.draft_tokens or 0 for r in ok)
    draft_accepted = sum(r.draft_tokens_accepted or 0 for r in ok)
    draft_rate = (draft_accepted / draft_generated) if draft_generated else None

    print(f"\n{label} summary")
    print(f"  runs_ok:            {len(ok)}/{len(results)}")
    print(f"  wall_s_mean:        {fmt(mean([r.wall_s for r in ok]))}")
    print(f"  prompt_tps_mean:    {fmt(mean([r.prompt_tps for r in ok]))}")
    print(f"  generation_tps_mean:{fmt(mean([r.generation_tps for r in ok]))}")
    print(f"  wall_comp_tps_mean: {fmt(mean([r.wall_completion_tps for r in ok]))}")
    print(f"  completion_tokens:  {fmt(mean([r.completion_tokens for r in ok]), 0)} mean")
    print(f"  draft_acceptance:   {fmt(draft_rate, 4)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Full /v1/chat/completions URL")
    parser.add_argument("--label", required=True)
    parser.add_argument("--model", default="bench")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--prompt-file")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument(
        "--extra-json",
        default="{}",
        help="JSON object merged into each request payload, e.g. '{\"speculative.n_max\":4}'",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        extra_payload = json.loads(args.extra_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--extra-json must be valid JSON: {e}") from e
    if not isinstance(extra_payload, dict):
        raise SystemExit("--extra-json must decode to a JSON object")

    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()

    print(
        "\t".join(
            [
                "label",
                "run",
                "status",
                "wall_s",
                "prompt_tokens",
                "completion_tokens",
                "prompt_tps",
                "generation_tps",
                "wall_comp_tps",
                "draft_accepted",
                "preview_or_error",
            ]
        )
    )

    for i in range(args.warmups):
        one_run(
            label=args.label,
            run=-(i + 1),
            url=args.url,
            model=args.model,
            prompt=prompt,
            max_tokens=min(args.max_tokens, 64),
            timeout=args.timeout,
            disable_thinking=not args.enable_thinking,
            extra_payload=extra_payload,
        )

    results: list[RunResult] = []
    for run in range(1, args.runs + 1):
        result = one_run(
            label=args.label,
            run=run,
            url=args.url,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            disable_thinking=not args.enable_thinking,
            extra_payload=extra_payload,
        )
        results.append(result)
        print_row(result)

    summarize(args.label, results)
    return 0 if all(r.status == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
