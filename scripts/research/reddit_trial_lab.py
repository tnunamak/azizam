#!/usr/bin/env python3
"""Run a task-level product trial lab for Reddit-readiness simulation.

This is the "100x" layer above endpoint probes. It treats each target user as
trying to finish a concrete job, records the evidence for every step, and emits
artifacts that can ground a realistic Reddit thread or product gap review.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research import reddit_simulation as sim  # noqa: E402


DEFAULT_SCENARIOS = Path(__file__).with_name("reddit_trial_lab_scenarios.json")
DEFAULT_OUT = Path("/tmp/ai-gateway-simulations/100x")


@dataclasses.dataclass
class StepResult:
    scenario_id: str
    step_id: str
    kind: str
    action: str
    required: bool
    costly: bool
    ok: bool
    skipped: bool
    status: int | None
    elapsed_ms: int | None
    reason: str
    summary: dict[str, Any]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def run(args: argparse.Namespace) -> int:
    scenarios = load_json(Path(args.scenarios))
    selected = _filter_scenarios(scenarios, set(args.scenario or []))
    base_url = sim.gateway_root_url(_configured_base_url(args))
    runtime_env, runtime_key = sim._env_key(sim.RUNTIME_KEY_ENVS)
    admin_env, admin_key = sim._env_key(sim.ADMIN_KEY_ENVS)
    out_dir = Path(args.out)
    if args.timestamp:
        out_dir = out_dir / dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for scenario in selected:
        for step in scenario.get("steps", []):
            if step.get("costly") and not args.include_costly:
                result = StepResult(
                    scenario_id=scenario["id"],
                    step_id=step["id"],
                    kind=step["type"],
                    action=_step_action(step),
                    required=bool(step.get("required", True)),
                    costly=True,
                    ok=False,
                    skipped=True,
                    status=None,
                    elapsed_ms=None,
                    reason="costly step skipped; pass --include-costly",
                    summary={},
                )
            else:
                result = execute_step(
                    step,
                    scenario_id=scenario["id"],
                    base_url=base_url,
                    runtime_key=runtime_key,
                    admin_key=admin_key,
                    timeout=args.timeout,
                )
            results.append(dataclasses.asdict(result))
            print(
                f"{scenario['id']}:{step['id']} "
                f"ok={result.ok} skipped={result.skipped} reason={result.reason}",
                flush=True,
            )
            time.sleep(args.sleep)

    context = {
        "base_url": base_url,
        "runtime_key_env": runtime_env,
        "admin_key_env": admin_env,
        "include_costly": args.include_costly,
        "corpus": sim.corpus_summary(Path(args.corpus)),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    lab = {
        "context": context,
        "scenarios": selected,
        "results": results,
        "results_by_scenario": group_results(results),
        "scenario_summaries": scenario_summaries(selected, results),
    }
    (out_dir / "100x-trial-lab.json").write_text(json.dumps(lab, indent=2) + "\n")
    (out_dir / "100x-journey-transcripts.md").write_text(
        render_journey_transcripts(selected, results, context)
    )
    (out_dir / "100x-product-gap-ledger.md").write_text(
        render_gap_ledger(selected, results, context)
    )
    (out_dir / "100x-reddit-thread-brief.md").write_text(
        render_reddit_thread_brief(selected, results, context)
    )
    print(f"wrote {out_dir}", flush=True)
    return 0


def _filter_scenarios(
    scenarios: list[dict[str, Any]], requested: set[str]
) -> list[dict[str, Any]]:
    if not requested:
        return scenarios
    return [scenario for scenario in scenarios if scenario["id"] in requested]


def _configured_base_url(args: argparse.Namespace) -> str:
    return (
        args.base_url
        or os.environ.get("AI_GATEWAY_BASE_URL")
        or os.environ.get("VIVID_OPENAI_BASE_URL")
        or sim.DEFAULT_BASE_URL
    )


def execute_step(
    step: dict[str, Any],
    *,
    scenario_id: str,
    base_url: str,
    runtime_key: str | None,
    admin_key: str | None,
    timeout: float,
) -> StepResult:
    kind = step["type"]
    if kind == "http":
        result = sim._request(
            base_url,
            step,
            runtime_key=runtime_key,
            admin_key=admin_key,
            timeout=timeout,
        )
        return StepResult(
            scenario_id=scenario_id,
            step_id=step["id"],
            kind=kind,
            action=_step_action(step),
            required=bool(step.get("required", True)),
            costly=bool(step.get("costly", False)),
            ok=result.ok,
            skipped=result.skipped,
            status=result.status,
            elapsed_ms=result.elapsed_ms,
            reason=result.reason,
            summary=result.summary,
        )
    if kind == "local_exists":
        return _execute_local_exists(step, scenario_id=scenario_id)
    return StepResult(
        scenario_id=scenario_id,
        step_id=step["id"],
        kind=kind,
        action=_step_action(step),
        required=bool(step.get("required", True)),
        costly=bool(step.get("costly", False)),
        ok=False,
        skipped=False,
        status=None,
        elapsed_ms=None,
        reason=f"unknown step type: {kind}",
        summary={},
    )


def _execute_local_exists(step: dict[str, Any], *, scenario_id: str) -> StepResult:
    started = time.monotonic()
    paths = [ROOT / path for path in step.get("paths", [])]
    present = [str(path.relative_to(ROOT)) for path in paths if path.exists()]
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.exists()]
    return StepResult(
        scenario_id=scenario_id,
        step_id=step["id"],
        kind="local_exists",
        action=_step_action(step),
        required=bool(step.get("required", True)),
        costly=False,
        ok=not missing,
        skipped=False,
        status=None,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        reason="all files present" if not missing else "missing files",
        summary={
            "present": present,
            "missing": missing,
            "note": step.get("note", ""),
        },
    )


def _step_action(step: dict[str, Any]) -> str:
    if step["type"] == "http":
        return f"{step.get('method', 'GET')} {step['path']} ({step.get('auth', 'none')} auth)"
    if step["type"] == "local_exists":
        return "Check local files: " + ", ".join(step.get("paths", []))
    return step["id"]


def group_results(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["scenario_id"], []).append(result)
    return grouped


def scenario_summaries(
    scenarios: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped = group_results(results)
    summaries = []
    for scenario in scenarios:
        items = grouped.get(scenario["id"], [])
        required = [item for item in items if item["required"]]
        required_failures = [
            item for item in required if not item["ok"] and not item["skipped"]
        ]
        required_skipped = [item for item in required if item["skipped"]]
        optional_failures = [
            item
            for item in items
            if not item["required"] and not item["ok"] and not item["skipped"]
        ]
        status = "passed"
        if required_failures:
            status = "failed"
        elif required_skipped:
            status = "untested"
        elif optional_failures:
            status = "passed_with_gaps"
        summaries.append(
            {
                "id": scenario["id"],
                "label": scenario["label"],
                "status": status,
                "steps": len(items),
                "passed": sum(1 for item in items if item["ok"]),
                "skipped": sum(1 for item in items if item["skipped"]),
                "failed": sum(
                    1 for item in items if not item["ok"] and not item["skipped"]
                ),
                "required_failures": [item["step_id"] for item in required_failures],
                "required_skipped": [item["step_id"] for item in required_skipped],
                "optional_failures": [item["step_id"] for item in optional_failures],
            }
        )
    return summaries


def render_journey_transcripts(
    scenarios: list[dict[str, Any]],
    results: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    grouped = group_results(results)
    summaries = {item["id"]: item for item in scenario_summaries(scenarios, results)}
    lines = [
        "# 100x Live Product Trial Lab",
        "",
        f"- Base URL: `{context['base_url']}`",
        f"- Runtime key env: `{context['runtime_key_env'] or 'missing'}`",
        f"- Admin key env: `{context['admin_key_env'] or 'missing'}`",
        f"- Costly/generative steps: {'included' if context['include_costly'] else 'skipped'}",
        f"- Corpus threads: {context['corpus']['unique_thread_count']} unique",
        "",
    ]
    for scenario in scenarios:
        summary = summaries[scenario["id"]]
        lines.extend(
            [
                f"## {scenario['label']}",
                "",
                f"Persona: {scenario['persona']}",
                "",
                f"Task: {scenario['task']}",
                "",
                f"Why it matters: {scenario['why_it_matters']}",
                "",
                f"Verdict: **{summary['status']}** ({summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped)",
                "",
                "### Action / Thought Transcript",
                "",
            ]
        )
        for result in grouped.get(scenario["id"], []):
            lines.append(f"- Action: `{result['action']}`")
            lines.append(f"  Thought: {result_thought(result)}")
        lines.extend(["", "### Reddit Friction", ""])
        lines.extend(f"- {angle}" for angle in scenario.get("reddit_angles", []))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def result_thought(result: dict[str, Any]) -> str:
    if result["skipped"]:
        return f"Not tested: {result['reason']}."
    if result["ok"]:
        detail = result_summary(result["summary"])
        timing = f" in {result['elapsed_ms']} ms" if result["elapsed_ms"] is not None else ""
        status = f"HTTP {result['status']}" if result["status"] is not None else result["reason"]
        return f"Works: {status}{timing}. {detail}".strip()
    detail = result_summary(result["summary"])
    status = f"HTTP {result['status']}" if result["status"] is not None else "no HTTP status"
    return f"Problem: {status}, {result['reason']}. {detail}".strip()


def result_summary(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    if "missing" in summary:
        if summary["missing"]:
            return "Missing: " + ", ".join(summary["missing"]) + "."
        return "Found: " + ", ".join(summary.get("present", [])[:6]) + "."
    return sim._summary_sentence(summary)


def render_gap_ledger(
    scenarios: list[dict[str, Any]],
    results: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    summaries = scenario_summaries(scenarios, results)
    grouped = group_results(results)
    lines = [
        "# 100x Product Gap Ledger",
        "",
        "| Scenario | Verdict | Evidence | Product read |",
        "|---|---:|---|---|",
    ]
    scenario_by_id = {scenario["id"]: scenario for scenario in scenarios}
    for summary in summaries:
        scenario = scenario_by_id[summary["id"]]
        evidence = (
            f"{summary['passed']} passed, {summary['failed']} failed, "
            f"{summary['skipped']} skipped"
        )
        product_read = product_read_for(summary, grouped.get(summary["id"], []))
        lines.append(
            f"| {scenario['label']} | {summary['status']} | {evidence} | {product_read} |"
        )
    lines.extend(["", "## Failure Details", ""])
    any_failure = False
    for scenario in scenarios:
        for result in grouped.get(scenario["id"], []):
            if result["ok"] or result["skipped"]:
                continue
            any_failure = True
            lines.append(
                f"- **{scenario['label']} / {result['step_id']}**: "
                f"{result['action']} -> {result['reason']}. {result_summary(result['summary'])}"
            )
    if not any_failure:
        lines.append("- No non-skipped failures in this run.")
    lines.extend(["", "## Untested Required Evidence", ""])
    untested = [
        (scenario, result)
        for scenario in scenarios
        for result in grouped.get(scenario["id"], [])
        if result["required"] and result["skipped"]
    ]
    if untested:
        for scenario, result in untested:
            lines.append(
                f"- **{scenario['label']} / {result['step_id']}**: {result['reason']}."
            )
    else:
        lines.append("- No required steps were skipped.")
    lines.extend(
        [
            "",
            "## Context",
            "",
            f"- Base URL: `{context['base_url']}`",
            f"- Runtime key env: `{context['runtime_key_env'] or 'missing'}`",
            f"- Admin key env: `{context['admin_key_env'] or 'missing'}`",
            f"- Corpus: `{context['corpus']['path']}` ({context['corpus']['unique_thread_count']} unique threads)",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def product_read_for(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    if summary["status"] == "passed":
        return "Claim has live evidence for this slice."
    if summary["status"] == "passed_with_gaps":
        return "Happy path works, but optional evidence exposes product/docs gaps."
    if summary["status"] == "untested":
        skipped = ", ".join(summary["required_skipped"])
        return f"Core claim not proven in this run: {skipped}."
    failures = ", ".join(summary["required_failures"])
    return f"Core workflow has blocking evidence gap: {failures}."


def render_reddit_thread_brief(
    scenarios: list[dict[str, Any]],
    results: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    summaries = scenario_summaries(scenarios, results)
    grouped = group_results(results)
    summary_by_id = {item["id"]: item for item in summaries}
    lines = [
        "# 100x Reddit Thread Brief",
        "",
        "This brief is grounded in task-level live trials, not just endpoint smoke tests.",
        "",
        "## OP Evidence",
        "",
        f"- Gateway root: `{context['base_url']}`",
        f"- Runtime key: `{context['runtime_key_env'] or 'missing'}`",
        f"- Admin key: `{context['admin_key_env'] or 'missing'}`",
        f"- Corpus: {context['corpus']['unique_thread_count']} unique target-subreddit threads",
        f"- Costly/generative tasks: {'included' if context['include_costly'] else 'skipped'}",
        "",
        "## Scenario Outcomes",
        "",
    ]
    for scenario in scenarios:
        summary = summary_by_id[scenario["id"]]
        lines.append(f"### {scenario['label']} on r/{scenario['subreddit']}")
        lines.append("")
        lines.append(f"- Persona: {scenario['persona']}")
        lines.append(f"- Task: {scenario['task']}")
        lines.append(f"- Verdict: {summary['status']}")
        lines.append(f"- Evidence: {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped")
        positives = [
            item for item in grouped.get(scenario["id"], []) if item["ok"]
        ][:5]
        failures = [
            item
            for item in grouped.get(scenario["id"], [])
            if not item["ok"] and not item["skipped"]
        ][:5]
        skipped = [
            item for item in grouped.get(scenario["id"], []) if item["skipped"]
        ][:5]
        if positives:
            lines.append("- Concrete positives:")
            lines.extend(
                f"  - `{item['action']}`: {result_summary(item['summary']) or item['reason']}"
                for item in positives
            )
        if failures:
            lines.append("- Concrete criticism:")
            lines.extend(
                f"  - `{item['action']}`: {item['reason']} {result_summary(item['summary'])}"
                for item in failures
            )
        if skipped:
            lines.append("- Missing evidence:")
            lines.extend(f"  - `{item['action']}`: {item['reason']}" for item in skipped)
        lines.append("- Likely comments:")
        lines.extend(f"  - {angle}" for angle in scenario.get("reddit_angles", []))
        lines.append("")
    lines.extend(
        [
            "## Thread Requirements",
            "",
            "- Include OP edits when follow-up probes change the evidence.",
            "- Include skeptical comparisons to LiteLLM, Open WebUI, Ollama, direct ComfyUI, and Bifrost.",
            "- Keep admin claims separate from runtime claims if no admin key/session was available.",
            "- Separate live-tested, statically discovered, unsupported, and skipped evidence.",
            "- Include latency numbers for local LLM calls.",
            "- Include biting comments about any compatible-ish route that fails a real client expectation.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--corpus", default=str(sim.DEFAULT_CORPUS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--scenario", action="append", help="Scenario id to run")
    parser.add_argument("--include-costly", action="store_true")
    parser.add_argument("--timestamp", action="store_true", default=True)
    parser.add_argument("--no-timestamp", dest="timestamp", action="store_false")
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.set_defaults(func=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
