#!/usr/bin/env python3
"""Compare one multi-turn chat on a plan model through the gateway with the same chat in Codex CLI.

Runs the same prompts four ways and prints one table per turn (input, cached input, output and reasoning tokens):
  1. the gateway's /v1/responses (the client sends the whole conversation again, reasoning items included),
  2. the explicit route pin on /v1/responses (control),
  3. the gateway's /v1/chat/completions (messages only),
  4. ``codex exec --json`` then ``codex exec resume`` (Codex's own turn.completed usage).

    AZIZAM_BASE_URL=https://gateway.example AZIZAM_API_KEY=... \\
        scripts/plan_parity_check.py --model gpt-6.1-terra --route-pin chatgpt-plan/gpt-6.1-terra --effort low --turns 4

The base URL and the key come from the environment and are never printed. Codex must already be signed in to the
same plan. Each path costs real plan usage: --turns defaults to 4. Use --skip-codex or --skip-gateway to run half.
Usage counts differ a little between runs (the model answers differently), so compare the shape: cached input
should grow turn by turn on every path, and input per turn should be about the same.

--tool-loop N runs one user turn on /v1/responses in which the model calls a tool N times, so every request after the
first continues the same turn. It prints the usage of each request and the gateway's x-azizam-turn-state header
(started, replayed, continued, or "-" when the gateway sends no turn state). The instructions are about 2,000 tokens
and start with a fresh run id, so the first request is cold and the later ones can only hit the cache of this run:

    scripts/plan_parity_check.py --model gpt-6.1-terra --effort low --tool-loop 5
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote

PROMPTS = [
    "Name three prime numbers above 100, in one line.",
    "Now name the sum of those three, in one line.",
    "Is that sum prime? Answer in one line.",
    "Give the next prime after the largest of the three, in one line.",
    "Repeat my first question, word for word.",
    "Summarise this chat in one line.",
]
COLUMNS = ("input", "cached", "output", "reasoning")


def api_request(path: str, body: dict | None = None) -> dict:
    base, key = os.environ.get("AZIZAM_BASE_URL", "").rstrip("/"), os.environ.get("AZIZAM_API_KEY", "")
    if not base or not key:
        sys.exit("Set AZIZAM_BASE_URL and AZIZAM_API_KEY.")
    request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            reply = json.load(response)
            reply["_parity_facts"] = {"route": response.headers.get("x-azizam-route"),
                                      "billing": response.headers.get("x-azizam-billing"),
                                      "turn_state": response.headers.get("x-azizam-turn-state")}
            return reply
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:600].replace(key, "[key]")
        sys.exit(f"{path} answered {error.code}: {detail}")


def post(path: str, body: dict) -> dict:
    return api_request(path, body)


def get(path: str) -> dict:
    return api_request(path)


def check_catalog_publication(before: dict, *, pin_prefix: str, get, post, owner_claims=()) -> dict:
    """Owner-run deploy check; two plan calls per pin, no hard-coded model set.

    Capture the before listing immediately before deployment. A discovery
    refresh can change that baseline; recapture it when comparing deployments.
    """
    if not pin_prefix or not pin_prefix.endswith("/"):
        raise ValueError("pin prefix must include its trailing slash")
    before_ids = {row["id"] for row in before["data"]}
    pins = sorted(mid for mid in before_ids if mid.startswith(pin_prefix))
    if not pins:
        raise ValueError("the baseline listing has no pins under that prefix")
    after = get("/v1/models")
    after_ids = {row["id"] for row in after["data"]}
    if set(pins) & after_ids:
        raise ValueError("a route pin remains in the ordinary listing")
    expected = {pin[len(pin_prefix):] for pin in pins} - set(owner_claims)
    if missing := expected - after_ids:
        raise ValueError(f"logical model ids missing from the listing: {sorted(missing)}")
    # Finish all listing checks before making any plan call.
    for pin in pins:
        retrieved = get("/v1/models/" + quote(pin, safe=""))
        if retrieved.get("id") != pin:
            raise ValueError(f"pin retrieval did not return {pin!r}")
        bodies = (("/v1/responses", {"model": pin, "store": False,
                    "input": [{"role": "user", "content": "Reply OK."}]}),
                  ("/v1/chat/completions", {"model": pin,
                    "messages": [{"role": "user", "content": "Reply OK."}]}))
        for path, body in bodies:
            reply = post(path, body)
            if not reply.get("id") or (reply.get("_parity_facts") or {}).get("route") != pin:
                raise ValueError(f"{path} did not complete on pin {pin!r}")
    return {"ids_in": sorted(after_ids - before_ids), "ids_out": sorted(before_ids - after_ids),
            "pins_checked": pins, "plan_calls": len(pins) * 2}


def counts_from_responses(usage: dict) -> dict:
    inputs, outputs = usage.get("input_tokens_details") or {}, usage.get("output_tokens_details") or {}
    return {"input": usage.get("input_tokens", 0), "cached": inputs.get("cached_tokens", 0),
            "output": usage.get("output_tokens", 0), "reasoning": outputs.get("reasoning_tokens", 0)}


def counts_from_chat(usage: dict) -> dict:
    return {"input": usage.get("prompt_tokens", 0), "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}


def run_responses(model: str, effort: str, prompts: list[str]) -> list[dict]:
    history: list[dict] = []
    rows = []
    for prompt in prompts:
        history.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]})
        reply = post("/v1/responses", {"model": model, "input": history, "reasoning": {"effort": effort}})
        history.extend(reply.get("output") or [])  # reasoning items go back next turn, as Codex does
        rows.append({**counts_from_responses(reply.get("usage") or {}),
                     "requested_model": model, "observed_model": reply.get("model"),
                     "observed_effort": (reply.get("reasoning") or {}).get("effort"),
                     **reply.get("_parity_facts", {})})
    return rows


def run_chat(model: str, effort: str, prompts: list[str]) -> list[dict]:
    messages: list[dict] = []
    rows = []
    for prompt in prompts:
        messages.append({"role": "user", "content": prompt})
        reply = post("/v1/chat/completions", {"model": model, "messages": messages, "reasoning_effort": effort})
        messages.append({"role": "assistant", "content": reply["choices"][0]["message"].get("content") or ""})
        rows.append({**counts_from_chat(reply.get("usage") or {}),
                     "requested_model": model, "observed_model": reply.get("model"),
                     "observed_effort": reply.get("reasoning_effort"), **reply.get("_parity_facts", {})})
    return rows


TOOL = {"type": "function", "name": "next_step", "description": "Returns the next step number, or done.",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}


def loop_instructions(run_id: str) -> str:
    """About 2,000 tokens of fixed rules, so the prefix is long enough to cache; the run id keeps each run cold."""
    rules = [f"Rule {n}: when step {n} is reached, keep the answer short, do not explain, and call the tool again."
             for n in range(1, 101)]
    return f"Run {run_id}. You follow a numbered procedure with a tool.\n" + "\n".join(rules)


def run_tool_loop(model: str, effort: str, steps: int, run_id: str) -> list[dict]:
    """One user turn: the model calls next_step until it returns done. Each request sends the whole input again."""
    history: list[dict] = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text":
        "Call next_step with n=1. Each time it returns a number, call it again with that number. "
        "When it returns done, reply with the single word finished."}]}]
    rows = []
    for _ in range(steps + 2):
        reply = post("/v1/responses", {"model": model, "instructions": loop_instructions(run_id), "input": history,
                                       "tools": [TOOL], "reasoning": {"effort": effort}})
        output = reply.get("output") or []
        history.extend(output)
        rows.append({**counts_from_responses(reply.get("usage") or {}),
                     "turn_state": (reply.get("_parity_facts") or {}).get("turn_state") or "-"})
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            break
        for item in calls:
            try:
                n = int(json.loads(item.get("arguments") or "{}").get("n", 0))
            except (ValueError, TypeError, AttributeError):
                n = 0
            history.append({"type": "function_call_output", "call_id": item.get("call_id"),
                            "output": "done" if n >= steps else str(n + 1)})
    return rows


def loop_table(rows: list[dict]) -> str:
    lines = ["request | " + " / ".join(f"{col:>8}" for col in COLUMNS) + " | turn state"]
    for number, row in enumerate(rows, 1):
        lines.append(f"{number:>7} | " + " / ".join(f"{row[col]:>8}" for col in COLUMNS) + f" | {row['turn_state']}")
    follow = rows[1:]
    hits = sum(1 for row in follow if row["cached"] > 0)
    lines.append(f"follow-up requests with cached input: {hits} of {len(follow)}")
    return "\n".join(lines)


def codex_turn(args: list[str], prompt: str, cwd: str) -> tuple[str | None, dict]:
    done = subprocess.run(["codex", "exec", *args, prompt], cwd=cwd, capture_output=True, text=True, timeout=900)
    thread, usage = None, {}
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            thread = event.get("thread_id")
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
        elif event.get("type") == "turn.failed":
            sys.exit("codex turn failed: " + json.dumps(event)[:400])
    if not usage:
        sys.exit("codex printed no turn.completed usage; stderr: " + done.stderr[-400:])
    return thread, {"input": usage.get("input_tokens", 0), "cached": usage.get("cached_input_tokens", 0),
                    "output": usage.get("output_tokens", 0), "reasoning": usage.get("reasoning_output_tokens", 0)}


def run_codex(model: str, effort: str, prompts: list[str]) -> list[dict]:
    common = ["--json", "--skip-git-repo-check", "-m", model, "-c", f'model_reasoning_effort="{effort}"']
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as work:
        thread, row = codex_turn([*common, "-s", "read-only"], prompts[0], work)
        rows.append(row)
        for prompt in prompts[1:]:
            _, row = codex_turn(["resume", thread, *common], prompt, work)
            rows.append(row)
    return rows


def table(results: dict[str, list[dict]]) -> str:
    names = list(results)
    head = "turn | " + " | ".join(f"{name:^{9 * len(COLUMNS) + 3 * (len(COLUMNS) - 1)}}" for name in names)
    sub = "     | " + " | ".join(" / ".join(f"{col[:8]:>8}" for col in COLUMNS) for _ in names)
    lines = [head, sub, "-" * len(sub)]
    for turn in range(max(len(rows) for rows in results.values())):
        cells = []
        for name in names:
            rows = results[name]
            cells.append(" / ".join(f"{rows[turn][col]:>8}" for col in COLUMNS) if turn < len(rows) else " " * 40)
        lines.append(f"{turn + 1:>4} | " + " | ".join(cells))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="the plan's own model name, as Codex takes it (for example gpt-6.1-terra)")
    parser.add_argument("--gateway-model", help="the logical gateway model id (default: --model)")
    parser.add_argument("--route-pin", help="the explicit route pin to use as the Responses control")
    parser.add_argument("--effort", default="low", help="reasoning effort for every path (default: low)")
    parser.add_argument("--turns", type=int, default=4, help=f"1 to {len(PROMPTS)} (default 4)")
    parser.add_argument("--tool-loop", type=int, metavar="N", help="one turn of N tool calls on /v1/responses (see above)")
    parser.add_argument("--skip-codex", action="store_true")
    parser.add_argument("--skip-gateway", action="store_true")
    parser.add_argument("--check-catalog", metavar="BEFORE_JSON", help="owner deploy check: list delta, pin retrieval, and two real plan calls per pin")
    parser.add_argument("--pin-prefix", help="pin namespace for the deploy check, including its trailing slash")
    parser.add_argument("--owner-claims", metavar="JSON", help="optional JSON list of owner-authored public ids; never infer a route group")
    args = parser.parse_args()
    if args.check_catalog:
        if not args.pin_prefix:
            parser.error("--pin-prefix is required with --check-catalog")
        before = json.loads(Path(args.check_catalog).read_text())
        claims = json.loads(Path(args.owner_claims).read_text()) if args.owner_claims else []
        try:
            result = check_catalog_publication(before, pin_prefix=args.pin_prefix, get=get, post=post, owner_claims=claims)
        except ValueError as error:
            sys.exit(str(error))
        print(json.dumps(result, indent=2))
        return
    if not args.model:
        parser.error("--model is required for parity")
    if args.tool_loop:
        run_id = os.urandom(6).hex()
        rows = run_tool_loop(args.gateway_model or args.model, args.effort, max(1, min(args.tool_loop, 20)), run_id)
        print(f"model {args.gateway_model or args.model}, effort {args.effort}, tool loop, run {run_id}; "
              "tokens: input / cached / output / reasoning\n")
        print(loop_table(rows))
        return
    prompts = PROMPTS[:max(1, min(args.turns, len(PROMPTS)))]
    gateway_model = args.gateway_model or args.model
    if not args.skip_gateway and not args.route_pin:
        parser.error("--route-pin is required for the gateway pinned control")
    results: dict[str, list[dict]] = {}
    if not args.skip_gateway:
        results["gateway /v1/responses"] = run_responses(gateway_model, args.effort, prompts)
        results["pinned control /v1/responses"] = run_responses(args.route_pin, args.effort, prompts)
        results["gateway /v1/chat"] = run_chat(gateway_model, args.effort, prompts)
    if not args.skip_codex:
        results["codex exec"] = run_codex(args.model, args.effort, prompts)
    print(f"model {args.model}, effort {args.effort}, {len(prompts)} turns; tokens: input / cached / output / reasoning\n")
    print(table(results))
    for name, rows in results.items():
        if name == "codex exec":
            continue
        for turn, row in enumerate(rows, 1):
            facts = {key: row.get(key) for key in ("requested_model", "observed_model", "observed_effort", "route", "billing")}
            print(f"{name}, turn {turn}: {json.dumps(facts, sort_keys=True)}")
    print("Plan billing reports USD unknown, not zero. Check the request receipts for cost and reconciliation facts.")


if __name__ == "__main__":
    main()
