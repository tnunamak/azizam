"""Record one synthetic request set against one upstream into a transcript.

Manual use only, by the owner (TEST_STRATEGY 4.7). The upstream is a local
engine addressed directly; the service comes from ``canaries.toml``. Code here
branches on protocol framing, capability and step type, never on an engine.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from tests.support.fake_upstream.protocols import PROTOCOLS

from . import allowlist, canaries, transcript

REQUEST_SETS = Path(__file__).resolve().parents[2] / "live" / "request_sets.json"


class RecordingError(RuntimeError):
    pass


def load_request_sets() -> dict[str, Any]:
    return json.loads(REQUEST_SETS.read_text(encoding="utf-8"))


def pick_model(service: dict[str, Any], feature: str) -> str:
    """The smallest model in the operator's inventory that lists ``feature``."""
    fitting = [m for m in service["models"] if feature in m["features"]]
    if not fitting and feature == "observe":
        return ""
    if not fitting:
        raise RecordingError(f"no model of service {service['id']!r} lists feature {feature!r}")
    return min(fitting, key=lambda m: m["size_bytes"])["id"]


def _fill(value: Any, model: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{model}}", model)
    if isinstance(value, dict):
        return {k: _fill(v, model) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, model) for v in value]
    return value


def record(
    service: dict[str, Any],
    set_name: str,
    *,
    idle_check: Callable[[], str | None],
    owner: str | None = None,
    today: dt.date | None = None,
    request_sets: dict[str, Any] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Run the set's steps, one request each, and return the validated transcript.

    ``idle_check`` returns None when the host is idle, else the reason it is not.
    It runs before every request and a busy answer stops the recording.
    """
    today = today or dt.date.today()
    problems = canaries.validate({"schema_version": 1, "services": [service]}, today)
    if problems:
        raise RecordingError("canaries invalid:\n" + "\n".join(problems))
    protocol = service["protocol"]
    sets = request_sets or load_request_sets()
    request_set = sets.get(protocol, {}).get(set_name)
    if request_set is None:
        raise RecordingError(f"no request set {set_name!r} for protocol {protocol!r}")
    if request_set["feature"] not in service["capabilities"]:
        raise RecordingError(f"service {service['id']!r} does not declare capability {request_set['feature']!r}")
    limits = service["limits"]
    steps = request_set["steps"]
    if len(steps) > limits["max_requests"]:
        raise RecordingError("the request set needs more requests than limits.max_requests allows")
    model = pick_model(service, request_set["feature"])
    framing = PROTOCOLS[protocol].framing
    normalizer = allowlist.Normalizer(protocol)
    deadline = time.monotonic() + limits["wall_s"]
    exchanges: list[dict[str, Any]] = []
    identity: dict[str, str] = {}
    # No redirects, no proxies from the environment, no retries: one request per step.
    with httpx.Client(base_url=service["base_url"], follow_redirects=False, trust_env=False, transport=transport) as client:
        for number, step in enumerate(steps, 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecordingError("limits.wall_s ran out")
            busy = idle_check()
            if busy:
                raise RecordingError(f"not idle, nothing sent: {busy}")
            body = _fill(copy.deepcopy(step.get("json")), model)
            headers = {"accept": "text/event-stream" if step["stream"] and framing.startswith("sse") else "application/json"}
            if body is not None:
                headers["content-type"] = "application/json"
            headers.update(step.get("headers", {}))
            status, response_headers, raw = _send(client, step, body, headers, limits, remaining)
            if not 200 <= status < 300:
                raise RecordingError(f"step {number} answered HTTP {status}; only successful exchanges are recorded")
            for key in ("engine_header", "version_header"):
                if key in service and response_headers.get(service[key].lower()):
                    identity[key] = response_headers[service[key].lower()]
            exchanges.append(_exchange(f"{set_name}-{number}", step, body, headers, status,
                                       response_headers, raw, protocol, framing, normalizer))
    reported = lambda key: identity.get(key, transcript.UNVERIFIED)  # noqa: E731
    data = {
        "protocol": protocol,
        "name": f"rp-{set_name}",
        "description": f"Recorded request set {set_name!r}: {request_set['feature']}.",
        "provenance": {
            "source": "transcript",
            "reference": f"scripts/record-upstream request set {protocol}/{set_name}",
            "protocol": protocol,
            "engine": reported("engine_header"),
            "engine_version": reported("version_header"),
            "identity_source": "response headers named by canaries.toml" if identity else transcript.UNVERIFIED,
            "recorded_on": today.isoformat(),
            "request_set_hash": transcript.set_hash(request_set),
            "recorder_version": transcript.RECORDER_VERSION,
            "owner": owner or service["owner"],
            "review_by": (today + dt.timedelta(days=transcript.REVIEW_WINDOW_DAYS)).isoformat(),
        },
        "exchanges": exchanges,
    }
    transcript.validate(data, today)
    return data


def _send(client: httpx.Client, step: dict[str, Any], body: Any, headers: dict[str, str],
          limits: dict[str, Any], remaining: float) -> tuple[int, dict[str, str], bytes]:
    cap = limits["max_response_bytes"]
    timeout = httpx.Timeout(min(float(limits["wall_s"]), remaining))
    try:
        with client.stream(step["method"], step["path"], json=body, headers=headers, timeout=timeout) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw += chunk
                if len(raw) > cap:
                    raise RecordingError("response exceeded limits.max_response_bytes")
            return response.status_code, {k.lower(): v for k, v in response.headers.items()}, bytes(raw)
    except httpx.HTTPError as error:
        raise RecordingError(f"request failed: {type(error).__name__}") from None  # never echo the URL or body


def _exchange(exchange_id: str, step: dict[str, Any], body: Any, headers: dict[str, str], status: int,
              response_headers: dict[str, str], raw: bytes, protocol: str, framing: str,
              normalizer: allowlist.Normalizer) -> dict[str, Any]:
    response: dict[str, Any] = {"status": status}
    kept = allowlist.response_headers(response_headers)
    if kept:
        response["headers"] = kept
    try:
        if step["stream"]:
            response["body"] = [
                part if "done" in part else {"event": normalizer.response(part["event"])}
                for part in allowlist.parse_stream(framing, raw)
            ]
        else:
            response["json"] = normalizer.response(json.loads(raw))
    except (ValueError, UnicodeDecodeError) as error:
        raise RecordingError(f"{exchange_id}: response does not fit the {protocol} allowlist ({type(error).__name__}: {error})") from None
    return {
        "id": exchange_id,
        "match": {"method": step["method"], "path": step["path"], "query": {},
                  "headers": allowlist.request_headers(protocol, headers), "json": body},
        "response": response,
    }
