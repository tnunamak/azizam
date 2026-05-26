#!/usr/bin/env python3
"""Run corpus-grounded live user simulations for Reddit-thread drafting.

The script does not try to be a chatbot. It executes explicit HTTP probes for
each persona, writes action/thought trial transcripts, and produces a Reddit
thread brief that can be handed to a writer or LLM.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://ai.vivid.fish"
DEFAULT_PERSONAS = Path(__file__).with_name("reddit_simulation_personas.json")
DEFAULT_CORPUS = Path("/tmp/ai-gateway-reddit-corpus")
DEFAULT_OUT = Path("/tmp/ai-gateway-simulations/live")
RUNTIME_KEY_ENVS = ("AI_GATEWAY_API_KEY", "VIVID_OPENAI_API_KEY", "OPENAI_PROXY_API_KEY")
ADMIN_KEY_ENVS = ("AI_GATEWAY_ADMIN_KEY", "VIVID_ADMIN_API_KEY", "OPENAI_PROXY_ADMIN_KEY")


@dataclasses.dataclass
class ProbeResult:
    persona_id: str
    probe_id: str
    method: str
    path: str
    auth: str
    status: int | None
    elapsed_ms: int | None
    ok: bool
    skipped: bool
    reason: str
    summary: dict[str, Any]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _env_key(names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    return None, None


def _base_url(args: argparse.Namespace) -> str:
    configured = (
        args.base_url
        or os.environ.get("AI_GATEWAY_BASE_URL")
        or os.environ.get("VIVID_OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    return gateway_root_url(configured)


def gateway_root_url(value: str) -> str:
    base = value.rstrip("/")
    for suffix in ("/openai/v1", "/anthropic/v1", "/ollama/api", "/v1", "/api"):
        if base.endswith(suffix):
            return base[: -len(suffix)] or base
    return base


def _timestamp_dir(root: Path) -> Path:
    now = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    return root / now


def _request(
    base_url: str,
    probe: dict[str, Any],
    *,
    runtime_key: str | None,
    admin_key: str | None,
    timeout: float,
) -> ProbeResult:
    auth = probe.get("auth", "none")
    key = None
    if auth == "runtime":
        key = runtime_key
    elif auth == "admin":
        key = admin_key
    if auth in ("runtime", "admin") and not key:
        return ProbeResult(
            persona_id="",
            probe_id=probe["id"],
            method=probe.get("method", "GET"),
            path=probe["path"],
            auth=auth,
            status=None,
            elapsed_ms=None,
            ok=False,
            skipped=True,
            reason=f"missing {auth} key",
            summary={},
        )

    url = urllib.parse.urljoin(base_url + "/", probe["path"].lstrip("/"))
    body = None
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if "json" in probe:
        body = json.dumps(probe["json"]).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        method=probe.get("method", "GET"),
        headers=headers,
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(150_000)
            status = response.status
            content_type = response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read(80_000)
        status = exc.code
        content_type = exc.headers.get("content-type", "")
    except (OSError, TimeoutError) as exc:
        return ProbeResult(
            persona_id="",
            probe_id=probe["id"],
            method=probe.get("method", "GET"),
            path=probe["path"],
            auth=auth,
            status=None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            ok=False,
            skipped=False,
            reason=type(exc).__name__,
            summary={"error": str(exc)},
        )

    expected = set(probe.get("expect_status") or [200])
    return ProbeResult(
        persona_id="",
        probe_id=probe["id"],
        method=probe.get("method", "GET"),
        path=probe["path"],
        auth=auth,
        status=status,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        ok=status in expected,
        skipped=False,
        reason="expected status" if status in expected else "unexpected status",
        summary=summarize_body(raw, content_type),
    )


def summarize_body(raw: bytes, content_type: str = "") -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    summary: dict[str, Any] = {
        "content_type": content_type,
        "bytes_sampled": len(raw),
    }
    if _looks_like_sse(text, content_type):
        summary.update(_summarize_sse(text))
        return summary
    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            summary["text_preview"] = _clean_preview(text)
            return summary
        summary["json_type"] = type(value).__name__
        if isinstance(value, dict):
            summary["keys"] = sorted(str(key) for key in value.keys())[:20]
            summary.update(_known_json_counts(value))
            summary.update(_known_json_error(value))
        elif isinstance(value, list):
            summary["list_len"] = len(value)
        return summary
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title:
        summary["html_title"] = _clean_preview(title.group(1), max_chars=140)
    summary["text_preview"] = _clean_preview(text)
    return summary


def _looks_like_sse(text: str, content_type: str) -> bool:
    return "text/event-stream" in content_type.lower() or text.lstrip().startswith("data:")


def _summarize_sse(text: str) -> dict[str, Any]:
    payloads = [
        line.removeprefix("data:").strip()
        for line in text.splitlines()
        if line.startswith("data:")
    ]
    parsed = []
    content_events = 0
    reasoning_events = 0
    for payload in payloads:
        if not payload or payload == "[DONE]":
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        parsed.append(value)
        choices = value.get("choices") if isinstance(value, dict) else None
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict) and delta.get("content"):
                content_events += 1
            if isinstance(delta, dict) and delta.get("reasoning_content"):
                reasoning_events += 1
    return {
        "sse_event_count": len(payloads),
        "sse_json_count": len(parsed),
        "sse_done": "[DONE]" in payloads,
        "sse_content_events": content_events,
        "sse_reasoning_events": reasoning_events,
    }


def _known_json_counts(value: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("data", "models", "workflows", "capabilities", "providers", "jobs"):
        item = value.get(key)
        if isinstance(item, list):
            counts[f"{key}_count"] = len(item)
    return counts


def _known_json_error(value: dict[str, Any]) -> dict[str, str]:
    error = value.get("error")
    if isinstance(error, dict):
        details: dict[str, str] = {}
        for key in ("type", "code", "message"):
            item = error.get(key)
            if isinstance(item, str) and item:
                details[f"error_{key}"] = _clean_preview(item, max_chars=180)
        return details
    if isinstance(error, str) and error:
        return {"error_message": _clean_preview(error, max_chars=180)}
    return {}


def _clean_preview(value: str, max_chars: int = 240) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[:max_chars]


def run(args: argparse.Namespace) -> int:
    personas = _load_json(Path(args.personas))
    persona_filter = set(args.persona or [])
    base_url = _base_url(args)
    runtime_env, runtime_key = _env_key(RUNTIME_KEY_ENVS)
    admin_env, admin_key = _env_key(ADMIN_KEY_ENVS)
    out_dir = Path(args.out)
    if args.timestamp:
        out_dir = _timestamp_dir(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    selected_personas = [
        persona
        for persona in personas
        if not persona_filter or persona["id"] in persona_filter
    ]
    for persona in selected_personas:
        for probe in persona.get("probes", []):
            if probe.get("costly") and not args.include_generative:
                result = ProbeResult(
                    persona_id=persona["id"],
                    probe_id=probe["id"],
                    method=probe.get("method", "GET"),
                    path=probe["path"],
                    auth=probe.get("auth", "none"),
                    status=None,
                    elapsed_ms=None,
                    ok=False,
                    skipped=True,
                    reason="costly probe skipped; pass --include-generative",
                    summary={},
                )
            else:
                result = _request(
                    base_url,
                    probe,
                    runtime_key=runtime_key,
                    admin_key=admin_key,
                    timeout=args.timeout,
                )
                result.persona_id = persona["id"]
            results.append(dataclasses.asdict(result))
            if not result.skipped:
                print(
                    f"{persona['id']}:{probe['id']} "
                    f"status={result.status} ok={result.ok}",
                    flush=True,
                )
            time.sleep(args.sleep)

    context = {
        "base_url": base_url,
        "runtime_key_env": runtime_env,
        "admin_key_env": admin_env,
        "include_generative": args.include_generative,
        "corpus": corpus_summary(Path(args.corpus)),
    }
    (out_dir / "live-probes.json").write_text(
        json.dumps(
            {
                "context": context,
                "results": results,
                "results_by_persona": _group_results(results),
            },
            indent=2,
        )
        + "\n"
    )
    (out_dir / "persona-trials.md").write_text(
        render_trials(selected_personas, results, context)
    )
    (out_dir / "reddit-thread-brief.md").write_text(
        render_reddit_brief(selected_personas, results, context)
    )
    print(f"wrote {out_dir}", flush=True)
    return 0


def corpus_summary(corpus_dir: Path) -> dict[str, Any]:
    thread_dir = corpus_dir / "threads"
    report_dir = corpus_dir / "reports"
    thread_files = sorted(thread_dir.glob("*.md"))
    unique_threads = {
        " ".join(match.groups()).lower()
        for path in thread_files
        if (match := re.match(r"([^-]+)-([^-]+)-", path.name))
    }
    return {
        "path": str(corpus_dir),
        "thread_markdown_count": len(thread_files),
        "unique_thread_count": len(unique_threads),
        "reports": sorted(path.name for path in report_dir.glob("*.md")),
    }


def render_trials(
    personas: list[dict[str, Any]],
    results: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    grouped = _group_results(results)
    lines = [
        "# Live User Simulation Trials",
        "",
        f"- Base URL: `{context['base_url']}`",
        f"- Runtime key env: `{context['runtime_key_env'] or 'missing'}`",
        f"- Admin key env: `{context['admin_key_env'] or 'missing'}`",
        f"- Corpus threads: {context['corpus']['unique_thread_count']} unique",
        "",
    ]
    for persona in personas:
        lines.extend(_render_persona_trial(persona, grouped.get(persona["id"], [])))
    return "\n".join(lines).rstrip() + "\n"


def _render_persona_trial(persona: dict[str, Any], results: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"## {persona['label']}",
        "",
        f"Archetype: {persona['archetype']}",
        "",
        f"Borrowed environment: {persona['borrowed_environment']}",
        "",
        "### Goals",
        "",
    ]
    lines.extend(f"- {goal}" for goal in persona.get("goals", []))
    lines.extend(["", "### Action / Thought Transcript", ""])
    for result in results:
        action = f"{result['method']} {result['path']}"
        lines.append(f"- Action: `{action}` ({result['auth']} auth)")
        lines.append(f"  Thought: {_trial_thought(result)}")
    lines.extend(["", "### Likely Reddit Angles", ""])
    lines.extend(f"- {angle}" for angle in persona.get("comment_angles", []))
    lines.append("")
    return lines


def _trial_thought(result: dict[str, Any]) -> str:
    if result["skipped"]:
        return f"Not tested: {result['reason']}."
    status = result["status"]
    elapsed = result["elapsed_ms"]
    if result["ok"]:
        detail = _summary_sentence(result["summary"])
        return f"Works at HTTP {status} in {elapsed} ms. {detail}".strip()
    detail = _summary_sentence(result["summary"])
    return f"Problem: HTTP {status} in {elapsed} ms, {result['reason']}. {detail}".strip()


def _summary_sentence(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    if "sse_event_count" in summary:
        done = "with [DONE]" if summary.get("sse_done") else "without [DONE]"
        return (
            f"SSE events: {summary['sse_event_count']} ({summary.get('sse_json_count', 0)} JSON), "
            f"content deltas: {summary.get('sse_content_events', 0)}, {done}."
        )
    count_bits = [
        f"{key.replace('_count', '')}: {value}"
        for key, value in summary.items()
        if key.endswith("_count")
    ]
    if count_bits:
        return "Returned " + ", ".join(count_bits) + "."
    if "keys" in summary:
        details = "JSON keys: " + ", ".join(summary["keys"][:8]) + "."
        if "error_message" in summary:
            details += f" Error: {summary['error_message']}."
        return details
    if "html_title" in summary:
        return f"HTML title: {summary['html_title']}."
    if "text_preview" in summary and summary["text_preview"]:
        return f"Preview: {summary['text_preview']}."
    return ""


def render_reddit_brief(
    personas: list[dict[str, Any]],
    results: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    grouped = _group_results(results)
    lines = [
        "# Corpus-Grounded Reddit Thread Brief",
        "",
        "Use this as input for the next simulated Reddit thread. Comments should be",
        "written as reactions to these concrete trial outcomes, not as generic takes.",
        "",
        "## OP Claims To Test",
        "",
        "- Self-hosted AI gateway for local LLMs and media workflows.",
        "- OpenAI, Anthropic, Ollama, and gateway-native API surfaces.",
        "- Browser-first setup, OIDC/admin, Apps, Profiles, and API keys.",
        "- Workflow-capability discovery instead of hardcoded service wizards.",
        "- Durable media jobs for image/audio/video work.",
        "",
        "## Grounding Corpus",
        "",
        f"- Corpus path: `{context['corpus']['path']}`",
        f"- Unique thread IDs: {context['corpus']['unique_thread_count']}",
        f"- Reports: {', '.join(context['corpus']['reports'])}",
        "",
        "## Persona Comment Seeds",
        "",
    ]
    for persona in personas:
        persona_results = grouped.get(persona["id"], [])
        failures = [item for item in persona_results if not item["ok"] and not item["skipped"]]
        skipped = [item for item in persona_results if item["skipped"]]
        successes = [item for item in persona_results if item["ok"]]
        lines.append(f"### {persona['label']} on r/{persona['subreddit']}")
        lines.append("")
        lines.append(f"- Environment: {persona['borrowed_environment']}")
        lines.append(f"- Successes: {len(successes)}")
        lines.append(f"- Skipped/not tested: {len(skipped)}")
        lines.append(f"- Failures: {len(failures)}")
        if successes:
            lines.append("- Concrete positives:")
            lines.extend(
                f"  - `{item['method']} {item['path']}`: {_summary_sentence(item['summary']) or 'returned expected status'}"
                for item in successes[:4]
            )
        if skipped:
            lines.append("- Missing evidence:")
            lines.extend(f"  - `{item['path']}`: {item['reason']}" for item in skipped[:4])
        if failures:
            lines.append("- Concrete criticism:")
            lines.extend(
                f"  - `{item['method']} {item['path']}` returned HTTP {item['status']}"
                for item in failures[:4]
            )
        lines.append("- Tone/angle:")
        lines.extend(f"  - {angle}" for angle in persona.get("comment_angles", []))
        lines.append("")
    lines.extend(
        [
            "## Thread Quality Checklist",
            "",
            "- Include biting but useful objections: why not LiteLLM/Open WebUI/LibreChat/Ollama directly?",
            "- Include setup questions before architecture debates.",
            "- Include a ComfyUI user who rejects service-specific wizards and asks for workflow metadata.",
            "- Include a LocalLLaMA user who asks for compatibility matrices and streaming/tool-call proof.",
            "- Include a selfhosted operator who cares about OIDC, recovery, key revocation, and service visibility.",
            "- Have OP answer with concrete links/results, not defensive claims.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _group_results(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["persona_id"], []).append(result)
    return grouped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--personas", default=str(DEFAULT_PERSONAS))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--persona", action="append", help="Persona id to run")
    parser.add_argument("--include-generative", action="store_true")
    parser.add_argument("--timestamp", action="store_true", default=True)
    parser.add_argument("--no-timestamp", dest="timestamp", action="store_false")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.set_defaults(func=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
