"""A fake Claude Code binary for runner tests: it speaks the print-mode stream-json the real one speaks (2.1.329 shapes).

Started by a small wrapper named like the real executable. It reads its behaviour from ``scenario.json`` in its config
home (the directory ``CLAUDE_CONFIG_DIR`` names), records each call in ``calls.jsonl`` there, and keeps its sessions in
``projects/fake-project/<session>.jsonl`` (the real binary's layout: ``<config home>/projects/<project>/<session>.jsonl``).
It never needs a sign-in. The config home also holds a token-shaped ``.credentials.json`` that a
correct runner never reads.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "."))
EMAIL = "owner@private.test"
MODELS = [
    {"value": "default", "resolvedModel": "claude-test-opus", "displayName": "Default", "description": "x",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "opus", "resolvedModel": "claude-test-opus", "displayName": "Opus", "description": "x",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "resolvedModel": "claude-test-haiku", "displayName": "Haiku", "description": "x",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high"]},
    {"value": "hidden", "resolvedModel": "claude-test-hidden", "displayName": "Hidden", "description": "x", "disabled": True},
]


def scenario() -> dict:
    path = HOME / "scenario.json"
    return json.loads(path.read_text()) if path.exists() else {}


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def record(entry: dict) -> None:
    with open(HOME / "calls.jsonl", "a", encoding="utf-8") as out:
        out.write(json.dumps(entry) + "\n")


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def auth_status(plan: dict) -> int:
    auth = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", **plan.get("auth", {})}
    print(json.dumps({**auth, "apiProvider": "firstParty", "email": EMAIL, "orgId": "org-private-1",
                      "orgName": "Private Org", "configDirectory": str(HOME)}, indent=2))
    return 0 if auth["loggedIn"] else 1


def usage(fresh: int, written: int, read: int, output: int) -> dict:
    return {"input_tokens": fresh, "cache_creation_input_tokens": written, "cache_read_input_tokens": read,
            "output_tokens": output, "output_tokens_details": {"thinking_tokens": 1}, "service_tier": "standard"}


def result(session: str, *, text: str, error: bool = False, subtype: str = "success", num_turns: int = 1,
           use: dict | None = None, status=None) -> None:
    emit({"type": "result", "subtype": subtype, "is_error": error, "num_turns": num_turns, "result": text,
          "session_id": session, "api_error_status": status, "usage": use or usage(0, 0, 0, 0),
          "total_cost_usd": 0.0123, "modelUsage": {}, "stop_reason": "end_turn"})


def run(argv: list[str], plan: dict) -> int:
    # Like the real binary, answer each input line as it arrives (a control request needs no end of input).
    lines: list[dict] = []
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        if not raw.strip():
            continue
        line = json.loads(raw)
        lines.append(line)
        if line.get("type") == "control_request":
            record({"argv": argv, "stdin": [line], "cwd": os.getcwd(), "env": dict(os.environ), "pid": os.getpid()})
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": line["request_id"],
                  "response": {"models": MODELS, "account": {"email": EMAIL, "tokenSource": "claude.ai"}}}})
            if plan.get("hang_after_initialize"):
                time.sleep(600)
            lines.pop()
        elif line.get("type") == "user":
            break
    if not lines:
        return 0
    prompt_file = flag(argv, "--system-prompt-file")
    system = Path(prompt_file).read_text() if prompt_file and Path(prompt_file).exists() else None
    record({"argv": argv, "stdin": lines, "cwd": os.getcwd(), "env": dict(os.environ), "pid": os.getpid(),
            "system": system, "cwd_entries": sorted(os.listdir(os.getcwd()))})
    session = flag(argv, "--session-id") or str(uuid.uuid4())
    store = HOME / "projects" / "fake-project"
    store.mkdir(parents=True, exist_ok=True)
    history: list[str] = []
    resumed = flag(argv, "--resume")
    if resumed:
        if not (store / f"{resumed}.jsonl").exists():
            result(session, text="", error=True, subtype="error_during_execution", num_turns=0)
            return 1
        history = json.loads((store / f"{resumed}.jsonl").read_text())
    mode = plan.get("mode", "ok")
    mcp = flag(argv, "--mcp-config")
    config = json.loads(Path(mcp).read_text())["mcpServers"]["caller"] if mcp else None
    if config:
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor
        def rpc(method, params):
            request = urllib.request.Request(config["url"],
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={**config["headers"], "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)["result"]
        catalog = rpc("tools/list", {})["tools"]
    tools = plan.get("tools", ["mcp__caller__" + t["name"] for t in catalog] if config else [])
    def init() -> None:
        emit({"type": "system", "subtype": "init", "session_id": session, "cwd": os.getcwd(), "tools": tools,
              "mcp_servers": plan.get("mcp_servers", [{"name": "caller", "status": "connected", "source": "dynamic"}] if config else []), "model": flag(argv, "--model"), "permissionMode": "dontAsk",
              "apiKeySource": plan.get("api_key_source", "none")})

    if not plan.get("init_late"):
        init()
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        (HOME / "hang.json").write_text(json.dumps({"pid": os.getpid(), "child": child.pid}))
        time.sleep(600)
        return 0
    if mode == "exit_early":
        return 3
    def rate_limits() -> None:
        for info in plan.get("rate_limit_events", []):
            emit({"type": "rate_limit_event", "rate_limit_info": info})
    if mode in ("rate_limit", "logged_out"):
        rate_limits()
        if mode == "rate_limit" and not plan.get("rate_limit_events"):
            emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}})
        code = "rate_limit" if mode == "rate_limit" else "authentication_failed"
        text = "Claude AI usage limit reached" if mode == "rate_limit" else "Not logged in · Please run /login"
        emit({"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
              "content": [{"type": "text", "text": text}]}, "parent_tool_use_id": None, "session_id": session,
              "error": code, "is_api_error_message": True})
        result(session, text=text, error=True)
        return 1
    # call_usage: one [fresh, written, read, output] per API call this process makes. Like the real binary, each call
    # reports its own usage on its stream events, and the result reports the whole turn (every call summed).
    calls = plan.get("call_usage")
    def call_start(index):
        if not calls:
            return {}
        fresh, written, read, _ = calls[index]
        return {"usage": {"input_tokens": fresh, "cache_creation_input_tokens": written, "cache_read_input_tokens": read,
                          "output_tokens": 1}}
    def call_output(index, default):
        return calls[index][3] if calls else default
    tool_reply = None
    if config:
        def stream(event):
            emit({"type": "stream_event", "parent_tool_use_id": None, "event": event})
        if plan.get("park_child"):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            (HOME / "park-child.json").write_text(json.dumps({"pid": child.pid}))
        for round_index in range(plan.get("tool_rounds", 1)):
            stream({"type": "message_start", "message": {"model": flag(argv, "--model"), **call_start(round_index)}})
            count = plan.get("tool_count", 1)
            for i in range(count):
                stream({"type": "content_block_start", "index": i,
                        "content_block": {"type": "tool_use", "id": f"toolu_{round_index}_{i}" if round_index else f"toolu_{i}", "name": "mcp__caller__" + catalog[0]["name"], "input": {}}})
                stream({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '{"x":'}})
                stream({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": str(i) + '}'}})
                stream({"type": "content_block_stop", "index": i})
            stream({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": call_output(round_index, 5), **({"output_tokens_details": {"thinking_tokens": 1}} if calls else {})}})
            stream({"type": "message_stop"})
            if plan.get("exit_parked"):
                return 0
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(rpc, "tools/call", {"name": catalog[0]["name"], "arguments": {"x": i},
                           "_meta": {"claudecode/toolUseId": f"toolu_{round_index}_{i}" if round_index else f"toolu_{i}"}}) for i in range(count)]
                tool_reply = "|".join("".join(p.get("text", "[image]") for p in f.result()["content"]) for f in futures)
    blocks = lines[-1]["message"]["content"]
    texts = [block["text"] for block in blocks]
    reply = tool_reply or plan.get("reply") or f"echo: {texts[-1].split(chr(10))[-1]}"
    emit({"type": "stream_event", "parent_tool_use_id": None, "session_id": session,
          "event": {"type": "message_start", "message": {"model": flag(argv, "--model") + "-observed", **call_start(-1)}}})
    if plan.get("partial", True):
        if plan.get("init_late") == "after_text":  # a binary that breaks the order: text first, init after
            init()
        for index, piece in enumerate([reply[: len(reply) // 2], reply[len(reply) // 2:]]):
            if plan.get("delay"):
                time.sleep(plan["delay"])
            emit({"type": "stream_event", "parent_tool_use_id": None, "session_id": session,
                  "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}})
    emit({"type": "assistant", "message": {"model": flag(argv, "--model"), "role": "assistant",
          "content": [{"type": "text", "text": reply}]}, "parent_tool_use_id": None, "session_id": session})
    rate_limits()
    written = sum(len(text) for text in texts)
    read = sum(len(text) for text in history)
    saved = json.dumps([*history, *texts, reply])
    if plan.get("write_after_result"):  # a binary that finishes its session file only after it printed the result
        result(session, text=reply, use=usage(5, written, read, 7))
        time.sleep(plan["write_after_result"])
        (store / f"{session}.jsonl").write_text(saved)
        return 0
    (store / f"{session}.jsonl").write_text(saved)
    if plan.get("fail_after_write"):  # a session file is on disk, but the run ends in an error
        result(session, text="", error=True, status=429)
        return 1
    if calls:
        emit({"type": "stream_event", "parent_tool_use_id": None, "session_id": session,
              "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": calls[-1][3], "output_tokens_details": {"thinking_tokens": 1}}}})
        whole = usage(*(sum(c[k] for c in calls) for k in range(4)))
        whole["output_tokens_details"] = {"thinking_tokens": len(calls)}
        result(session, text=reply, use=whole)
        return 0
    result(session, text=reply, use=usage(5, written, read, 7))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    plan = scenario()
    if argv[:2] == ["auth", "login"]:
        record({"argv": argv, "env": dict(os.environ), "pid": os.getpid()})
        url = "https://claude.ai/oauth/authorize?opaque=fake"
        if plan.get("wrapped_url"):
            url = url.replace("?opaque", "\n?opaque")
        if plan.get("other_url"):
            print("Help: https://unrelated.example.test/help", flush=True)
        print("Opening browser to sign in…\nIf the browser didn't open, visit:\n" + url, flush=True)
        print("Paste code here if prompted >", end="", flush=True)
        if plan.get("login") == "self":
            time.sleep(0.1)
        elif sys.stdin.readline().strip().split("#", 1)[0] != "good":
            return 1
        plan["auth"] = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
        (HOME / "scenario.json").write_text(json.dumps(plan))
        return 0
    if argv[:2] == ["auth", "status"]:
        record({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()})
        return auth_status(plan)
    if "-p" in argv:
        return run(argv, plan)
    return 2


if __name__ == "__main__":
    sys.exit(main())
