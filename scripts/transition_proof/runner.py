"""The proof flow for one case, over the gateway's HTTP surface only.

seed -> park -> (wake) -> follow-up -> cold baseline. Every step is a record in the result; the first step that
fails stops the case, and the steps after it are recorded as skipped. The harness never repairs a held gate: a
``failed_closed`` or ``reconciling`` domain is reported and left for the owner.
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Callable, Optional

import httpx

from .manifest import Case, read_path

ADMIN_DOMAINS = "/admin/api/engines/domains"
"""Where the gateway mounts the owner transition routes (gateway/transition_admin.py, on the admin router)."""
TERMINAL_OK = "succeeded"
HOLDING = ("failed_closed", "reconciling")
KNOWN_STATES = ("running", "succeeded", "aborted", *HOLDING)


class Proof:
    def __init__(self, client: httpx.Client, *, domain: str, model: str, wake_mode: str, run_id: str,
                 poll_interval_s: float, poll_timeout_s: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client
        self.domain = domain
        self.model = model
        self.wake_mode = wake_mode
        self.run_id = run_id
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.sleep = sleep

    # -- HTTP ---------------------------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """One request. Never raises: a transport error is a record with ``http_status`` None."""
        started = time.monotonic()
        record: dict[str, Any] = {"http_status": None, "json": None, "error": None}
        try:
            response = self.client.request(method, path, json=body)
            record["http_status"] = response.status_code
            try:
                record["json"] = response.json()
            except ValueError:
                record["error"] = response.text[:300]
            if response.status_code >= 400 and record["error"] is None:
                record["error"] = response.text[:300]
        except httpx.HTTPError as error:
            record["error"] = f"{type(error).__name__}: {error}"[:300]
        record["seconds"] = round(time.monotonic() - started, 4)
        return record

    def _gate(self) -> Optional[str]:
        reply = self._call("GET", f"{ADMIN_DOMAINS}/{self.domain}/gate")
        data = reply["json"]
        return data.get("gate") if reply["http_status"] == 200 and isinstance(data, dict) else None

    def _chat(self, body: dict[str, Any], config: Any) -> dict[str, Any]:
        reply = self._call("POST", "/v1/chat/completions", {**body, "model": self.model, "stream": False})
        data = reply.pop("json")
        ok = reply["http_status"] == 200 and isinstance(data, dict) and bool(read_path(data, "choices.0"))
        reply["ok"] = ok
        reply["reply_text"] = _text(read_path(data, "choices.0.message.content")) if ok else None
        reply["usage"] = data.get("usage") if ok and isinstance(data.get("usage"), dict) else None
        reply["cached_tokens"] = read_path(data, config.cached_tokens_path) if ok else None
        reply["prompt_tokens"] = read_path(data, config.prompt_tokens_path) if ok else None
        if not ok and reply["error"] is None:
            reply["error"] = "the reply has no choices"
        return reply

    # -- transitions --------------------------------------------------------------------------------------

    def _transition(self, kind: str) -> dict[str, Any]:
        key = f"proof-{self.run_id}-{kind}"
        step: dict[str, Any] = {"name": kind, "idempotency_key": key, "op_id": None, "states": [], "ok": False}
        started = time.monotonic()
        reply = self._call("POST", f"{ADMIN_DOMAINS}/{self.domain}/transitions",
                           {"kind": kind, "idempotency_key": key})
        data = reply["json"] if isinstance(reply["json"], dict) else {}
        step["http_status"] = reply["http_status"]
        step["op_id"] = data.get("op_id")
        if reply["http_status"] != 202 or not step["op_id"]:
            step["response"] = reply["error"]
            step["error"] = {
                404: "unknown domain, or a domain without transition enabled",
                409: f"another operation holds the domain (op {step['op_id']}, state {data.get('state')})",
            }.get(reply["http_status"], reply["error"] or f"unexpected status {reply['http_status']}")
            step["seconds"] = round(time.monotonic() - started, 4)
            return step
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            poll = self._call("GET", f"{ADMIN_DOMAINS}/{self.domain}/transitions/{step['op_id']}")
            body = poll["json"] if isinstance(poll["json"], dict) else {}
            operation = body.get("operation") if isinstance(body.get("operation"), dict) else {}
            state = operation.get("state")
            if poll["http_status"] != 200 or state not in KNOWN_STATES:
                step["error"] = poll["error"] or f"the operation read gave status {poll['http_status']}, state {state!r}"
                break
            if not step["states"] or step["states"][-1] != state:
                step["states"].append(state)
            if state != "running":
                step["final_state"] = state
                step["ok"] = state == TERMINAL_OK
                if state in HOLDING:
                    step["error"] = f"the operation is {state}: the gate stays held until the owner resolves it"
                elif state != TERMINAL_OK:
                    step["error"] = f"the operation ended {state}"
                break
            if time.monotonic() >= deadline:
                step["error"] = f"the operation was still running after {self.poll_timeout_s}s"
                break
            self.sleep(self.poll_interval_s)
        step["seconds"] = round(time.monotonic() - started, 4)
        return step

    # -- one case -----------------------------------------------------------------------------------------

    def run_case(self, case: Case) -> dict[str, Any]:
        record: dict[str, Any] = {"label": case.label, "file": case.file, "body_sha256": case.sha256,
                                  "status": "passed", "steps": [], "gate_trace": [], "assertions": []}
        steps, assertions = record["steps"], record["assertions"]
        check = case.cache

        def assertion(name: str, passed: bool, **detail: Any) -> bool:
            assertions.append({"name": name, "passed": passed, **detail})
            return passed

        def observe(at: str) -> Optional[str]:
            gate = self._gate()
            record["gate_trace"].append({"at": at, "gate": gate})
            return gate

        def skip_rest(*names: str) -> None:
            steps.extend({"name": name, "status": "skipped", "ok": False} for name in names)

        follow_messages = [*case.body["messages"], {"role": "user", "content": case.followup}]
        follow_body = {**case.body, "messages": follow_messages}

        seed = {"name": "seed", **self._chat(case.body, check)}
        steps.append(seed)
        if not assertion("seed_replied", seed["ok"], http_status=seed["http_status"], error=seed["error"]):
            skip_rest("park", "wake", "followup", "cold_baseline")
            record["status"] = "failed"
            return record
        denominator = _count(seed["prompt_tokens"])
        source = "seed_reply"
        if denominator is None and case.estimated_prompt_tokens:
            denominator, source = case.estimated_prompt_tokens, "manifest_count_estimate"

        observe("before_park")
        park = self._transition("park")
        steps.append(park)
        observe("after_park")
        if not assertion("park_succeeded", park["ok"], op_id=park["op_id"], final_state=park.get("final_state"),
                         error=park.get("error")):
            skip_rest("wake", "followup", "cold_baseline")
            record["status"] = "failed"
            return record

        if self.wake_mode == "post":
            wake = self._transition("wake")
            steps.append(wake)
            gate = observe("after_wake")
            ok = assertion("wake_succeeded", wake["ok"], op_id=wake["op_id"], final_state=wake.get("final_state"),
                           error=wake.get("error"))
            ok = assertion("gate_open_after_wake", gate == "open", gate=gate) and ok
            if not ok:
                skip_rest("followup", "cold_baseline")
                record["status"] = "failed"
                return record
        else:
            steps.append({"name": "wake", "mode": "request", "ok": True,
                          "note": "the follow-up request triggers the wake"})

        follow = {"name": "followup", **self._chat(follow_body, check)}
        steps.append(follow)
        gate = observe("after_followup")
        ok = assertion("followup_replied", follow["ok"], http_status=follow["http_status"], error=follow["error"])
        if self.wake_mode == "request":
            ok = assertion("gate_open_after_followup", gate == "open", gate=gate) and ok
        if not ok:
            skip_rest("cold_baseline")
            record["status"] = "failed"
            return record

        cached = _count(follow["cached_tokens"])
        passed = assertion("cached_tokens_reported", cached is not None, path=check.cached_tokens_path,
                           value=follow["cached_tokens"])
        passed = assertion("seed_prompt_tokens_known", denominator is not None, path=check.prompt_tokens_path,
                           source=source if denominator is not None else None) and passed
        if cached is not None and denominator:
            fraction = round(cached / denominator, 4)
            passed = assertion("cached_prefix_kept", fraction >= check.min_cached_fraction, cached_tokens=cached,
                               seed_prompt_tokens=denominator, denominator_source=source, fraction=fraction,
                               min_cached_fraction=check.min_cached_fraction) and passed

        if check.max_timing_ratio is None:
            skip_rest("cold_baseline")
        else:
            cold = {"name": "cold_baseline", **self._chat(_bust_cache(follow_body, self.run_id, case.label), check)}
            steps.append(cold)
            if assertion("cold_baseline_replied", cold["ok"], http_status=cold["http_status"], error=cold["error"]):
                ratio = round(follow["seconds"] / cold["seconds"], 4) if cold["seconds"] > 0 else None
                passed = assertion("followup_faster_than_cold", ratio is not None and ratio <= check.max_timing_ratio,
                                   followup_seconds=follow["seconds"], cold_seconds=cold["seconds"], ratio=ratio,
                                   max_timing_ratio=check.max_timing_ratio) and passed
            else:
                passed = False
        record["status"] = "passed" if passed else "failed"
        return record


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _count(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _bust_cache(body: dict[str, Any], run_id: str, label: str) -> dict[str, Any]:
    """The same request with a unique first line, so no engine can share a cached prefix with it."""
    busted = copy.deepcopy(body)
    nonce = f"[cold-baseline {run_id}-{label}-{uuid.uuid4().hex}]\n"
    first = busted["messages"][0]
    if isinstance(first["content"], list):
        first["content"] = [{"type": "text", "text": nonce}, *first["content"]]
    else:
        first["content"] = nonce + str(first["content"])
    return busted
