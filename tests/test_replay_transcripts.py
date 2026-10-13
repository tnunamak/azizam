"""RP1: replay machinery (TEST_STRATEGY 4.7), proved against the fake upstream as a stand-in.

A recording session runs against a hand-written fake that plays the real
upstream (extra fields, a host in a URL, raw ids); the recorder keeps only the
allowlisted, normalized part; the fake then replays that transcript strictly.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from tests.support.fake_upstream import FakeUpstreams, PROTOCOLS, scenario_from_data
from tests.support.fake_upstream.scenario import SCENARIO_DIR
from tests.support.replay import allowlist, canaries, idle, recorder, transcript

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import replay_scan  # noqa: E402

TODAY = dt.date(2026, 10, 12)
EXAMPLE = ROOT / "tests" / "live" / "canaries.example.toml"
IDLE = lambda: None  # noqa: E731


def _service(protocol: str, base_url: str, features=("text", "stream", "embeddings", "observe"), **limits) -> dict:
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    service = data["services"][0]
    service.update(protocol=protocol, base_url=base_url, capabilities=list(features), review_by=TODAY + dt.timedelta(days=30))
    service["models"] = [{"id": "big-model", "size_bytes": 900, "features": list(features)},
                         {"id": "tiny-model", "size_bytes": 5, "features": list(features)}]
    service["limits"].update(max_requests=4, **limits)
    return service


def _standin(protocol: str, path: str, response: dict, *, request_json: dict | None = None, method: str = "POST") -> dict:
    """A hand-written fake that plays the real upstream, noise included."""
    match: dict = {"method": method, "path": path}
    if request_json is not None:
        match["json"] = request_json
    return {"protocol": protocol, "name": "standin", "provenance": {"source": "hand-written", "reference": "unit test"},
            "exchanges": [{"id": "x", "match": match, "response": response}]}


NOISY_CHAT = {"id": "chatcmpl-real-123", "object": "chat.completion", "created": 1767225600, "model": "tiny-model",
              "system_fingerprint": "fp_secret_build", "timings": {"predicted_ms": 12.5},
              "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                           "message": {"role": "assistant", "content": "Hi!", "reasoning_content": "thinking"}}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def _record(fakes: FakeUpstreams, protocol: str, scenario: dict, set_name: str, **kw) -> dict:
    fake = fakes.loopback(scenario_from_data(scenario))
    return recorder.record(_service(protocol, fake.base_url), set_name, idle_check=IDLE, today=TODAY, **kw)


def _replay(fakes: FakeUpstreams, data: dict):
    return fakes.loopback(scenario_from_data(data))


def _post(base: str, path: str, body: dict, headers: dict | None = None) -> httpx.Response:
    with httpx.Client(trust_env=False) as client:
        return client.post(base + path, json=body, headers=headers)


@pytest.fixture
def standalone():
    """A FakeUpstreams the test judges itself, for cases that must latch a violation."""
    mp = pytest.MonkeyPatch()
    fakes = FakeUpstreams(mp)
    yield fakes
    fakes.close()
    mp.undo()


# --- canaries.toml -----------------------------------------------------------------------------

def test_example_canaries_are_valid() -> None:
    assert canaries.validate(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")), TODAY) == []


@pytest.mark.parametrize("limit", sorted(canaries.LIMITS))
def test_a_row_without_a_finite_limit_fails(limit: str) -> None:
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    del data["services"][0]["limits"][limit]
    assert any(f"limits.{limit} is required" in p for p in canaries.validate(data, TODAY))
    for bad in (float("inf"), float("nan"), -1, True, "20", 10**9):
        data["services"][0]["limits"][limit] = bad
        assert any(f"limits.{limit}" in p for p in canaries.validate(data, TODAY)), bad


@pytest.mark.parametrize("change,expect", [
    ({"base_url": "http://127.0.0.1:5000"}, "forbidden gateway port 5000"),
    ({"base_url": "http://127.0.0.1:15000"}, "forbidden gateway port 15000"),
    ({"base_url": "http://192.168.1.5:8080"}, "literal loopback"),
    ({"base_url": "http://localhost:8080"}, "literal loopback"),
    ({"base_url": "http://127.0.0.1"}, "explicit port"),
    ({"base_url": "http://127.0.0.1:8080/v1"}, "origin only"),
    ({"protocol": "no-such-protocol"}, "unknown protocol"),
    ({"target_kind": "gateway"}, "target_kind"),
    ({"review_by": dt.date(2026, 1, 1)}, "has passed"),
    ({"review_by": dt.date(2027, 6, 1)}, "more than 90 days"),
    ({"surprise": 1}, "unknown key"),
])
def test_canaries_reject_unsafe_rows(change: dict, expect: str) -> None:
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["services"][0].update(change)
    assert any(expect in p for p in canaries.validate(data, TODAY))


def test_retries_must_be_zero() -> None:
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["services"][0]["limits"]["max_retries"] = 1
    assert any("max_retries" in p for p in canaries.validate(data, TODAY))


# --- recording against the fake as a stand-in, replayed strictly ---------------------------------

def test_openai_chat_text_records_scrubs_and_replays_green(standalone) -> None:
    standin = _standin("openai-compatible", "/v1/chat/completions", {"json": NOISY_CHAT, "headers": {"set-cookie": "s=1", "x-host": "box.lan"}},
                       request_json={"model": "tiny-model"})
    data = _record(standalone, "openai-compatible", standin, "chat-text")
    assert data["provenance"]["source"] == "transcript" and data["provenance"]["engine"] == "UNVERIFIED"
    body = data["exchanges"][0]["response"]
    assert "set-cookie" not in body.get("headers", {}) and "x-host" not in body.get("headers", {})
    kept = body["json"]
    assert kept["id"] == "rp-id-1" and kept["created"] == 0 and "system_fingerprint" not in kept and "timings" not in kept
    assert kept["choices"][0]["message"]["reasoning_content"] == "thinking"  # a field the allowlist names, kept
    sent = data["exchanges"][0]["match"]["json"]
    assert sent["model"] == "tiny-model"  # smallest model with the feature
    fake = _replay(standalone, data)
    got = _post(fake.base_url, "/v1/chat/completions", sent, {"content-type": "application/json"})
    assert got.status_code == 200 and got.json() == kept
    assert fake.problems() == []


STREAM_PROTOCOLS = {
    "openai-compatible": ("/v1/chat/completions", "chat-stream", "sse-data"),
    "anthropic": ("/v1/messages", "messages-stream", "sse-named-event"),
    "ollama": ("/api/chat", "chat-stream", "ndjson"),
}


def _stream_events(protocol: str) -> list:
    if protocol == "openai-compatible":
        chunk = lambda delta, finish=None, **extra: {"id": "c-9", "object": "chat.completion.chunk", "created": 1, "model": "tiny-model",  # noqa: E731
                                                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return [{"event": chunk({"role": "assistant", "content": ""})}, {"event": chunk({"content": "Hi"})},
                {"event": chunk({}, "stop", timings={"x": 1})},
                {"event": {"id": "c-9", "object": "chat.completion.chunk", "created": 1, "model": "tiny-model", "choices": [],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}}, {"done": True}]
    if protocol == "anthropic":
        return [{"event": {"type": "message_start", "message": {"id": "msg_7", "type": "message", "role": "assistant", "model": "tiny-model",
                                                                  "content": [], "usage": {"input_tokens": 3, "output_tokens": 1}}}},
                {"event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}},
                {"event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}},
                {"event": {"type": "content_block_stop", "index": 0}},
                {"event": {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}},
                {"event": {"type": "message_stop"}}]
    return [{"event": {"model": "tiny-model", "created_at": "2026-10-12T10:00:00.123Z", "message": {"role": "assistant", "content": "Hi"}, "done": False}},
            {"event": {"model": "tiny-model", "created_at": "2026-10-12T10:00:01Z", "message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "stop", "total_duration": 123456, "eval_count": 2, "context": [1, 2, 3]}}]


@pytest.mark.parametrize("protocol", sorted(STREAM_PROTOCOLS))
def test_stream_framing_records_and_replays_green(standalone, protocol: str) -> None:
    path, set_name, framing = STREAM_PROTOCOLS[protocol]
    assert PROTOCOLS[protocol].framing == framing
    standin = _standin(protocol, path, {"status": 200, "body": _stream_events(protocol)}, request_json={"model": "tiny-model"})
    data = _record(standalone, protocol, standin, set_name)
    parts = data["exchanges"][0]["response"]["body"]
    assert len(parts) == len(_stream_events(protocol))
    assert "context" not in json.dumps(parts) and "timings" not in json.dumps(parts)
    fake = _replay(standalone, data)
    match = data["exchanges"][0]["match"]
    with httpx.Client(trust_env=False) as client:
        got = client.post(fake.base_url + path, json=match["json"], headers=match["headers"])
    assert got.status_code == 200
    again = allowlist.parse_stream(framing, got.content)
    assert again == parts
    assert fake.problems() == []


def test_anthropic_headers_ollama_embed_and_comfy_observe_record(standalone) -> None:
    anth = _standin("anthropic", "/v1/messages", {"json": {"id": "msg_1", "type": "message", "role": "assistant", "model": "tiny-model",
                    "content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 1}}},
                    request_json={"model": "tiny-model"})
    data = _record(standalone, "anthropic", anth, "messages-text")
    assert data["exchanges"][0]["match"]["headers"] == {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    emb = _standin("ollama", "/api/embed", {"json": {"model": "tiny-model", "embeddings": [[0.1, 0.2]], "total_duration": 9, "load_duration": 3}},
                   request_json={"model": "tiny-model"})
    data = _record(standalone, "ollama", emb, "embed")
    assert data["exchanges"][0]["response"]["json"]["embeddings"] == [[0.1, 0.2]]
    queue = _standin("comfyui", "/queue", {"json": {"queue_running": [], "queue_pending": []}}, method="GET")
    data = _record(standalone, "comfyui", queue, "observe")
    assert data["exchanges"][0]["match"]["json"] is None
    busy = _standin("comfyui", "/queue", {"json": {"queue_running": [[0, "job-1"]], "queue_pending": []}}, method="GET")
    with pytest.raises(recorder.RecordingError, match="not idle"):
        _record(standalone, "comfyui", busy, "observe")


def test_reported_engine_identity_comes_from_the_named_headers(standalone) -> None:
    standin = _standin("openai-compatible", "/v1/chat/completions", {"json": NOISY_CHAT, "headers": {"server": "stand-in/1", "x-engine-version": "9.9"}},
                       request_json={"model": "tiny-model"})
    fake = standalone.loopback(scenario_from_data(standin))
    service = _service("openai-compatible", fake.base_url)
    data = recorder.record(service, "chat-text", idle_check=IDLE, today=TODAY)
    assert (data["provenance"]["engine"], data["provenance"]["engine_version"]) == ("stand-in/1", "9.9")


# --- strict replay ------------------------------------------------------------------------------

def _chat_transcript(standalone) -> dict:
    standin = _standin("openai-compatible", "/v1/chat/completions", {"json": NOISY_CHAT}, request_json={"model": "tiny-model"})
    return _record(standalone, "openai-compatible", standin, "chat-text")


@pytest.mark.parametrize("change", [
    lambda body: body.update(model="other-model"),
    lambda body: body.update(max_tokens=9),
    lambda body: body.update(max_tokens=8.0),
    lambda body: body.update(temperature=False),
    lambda body: body.update(extra_param=True),
    lambda body: body.pop("max_tokens"),
], ids=["model", "value", "int-as-float", "zero-as-false", "extra-key", "missing-key"])
def test_a_changed_request_fails_replay(standalone, change) -> None:
    data = _chat_transcript(standalone)
    body = copy.deepcopy(data["exchanges"][0]["match"]["json"])
    change(body)
    fake = _replay(standalone, data)
    assert _post(fake.base_url, "/v1/chat/completions", body).status_code == 599
    problems = "\n".join(fake.problems())
    assert "unmatched request" in problems and "required exchange" in problems


def test_a_hand_written_scenario_keeps_subset_matching(standalone) -> None:
    hand = _standin("openai-compatible", "/v1/chat/completions", {"json": {"ok": True}}, request_json={"model": "m"})
    fake = standalone.loopback(scenario_from_data(hand))
    assert _post(fake.base_url, "/v1/chat/completions", {"model": "m", "extra": 1}).status_code == 200
    assert fake.problems() == []


def test_a_changed_job_id_fails_replay(standalone) -> None:
    data = {"protocol": "comfyui", "name": "job", "provenance": {"source": "transcript", "reference": "unit"},
            "exchanges": [{"id": "h", "match": {"method": "GET", "path": "/history/rp-id-1", "query": {}, "headers": {}, "json": None},
                           "response": {"json": {"status": {"completed": True}}}},
                          {"id": "c", "match": {"method": "POST", "path": "/interrupt", "query": {}, "headers": {}, "json": {"prompt_id": "rp-id-1"}},
                           "response": {"json": {}}}]}
    fake = _replay(standalone, data)
    assert httpx.get(fake.base_url + "/history/rp-id-2", trust_env=False).status_code == 599
    assert any("unmatched request" in p for p in fake.problems())
    other = _replay(standalone, data)
    assert httpx.get(other.base_url + "/history/rp-id-1", trust_env=False).status_code == 200
    assert _post(other.base_url, "/interrupt", {"prompt_id": "rp-id-2"}).status_code == 599


def test_a_missing_call_fails_the_transcript(standalone) -> None:
    fake = _replay(standalone, _chat_transcript(standalone))
    assert any("required exchange" in p for p in fake.problems())


# --- allowlists and normalizers -------------------------------------------------------------------

def test_normalizer_maps_ids_one_to_one_and_drops_the_rest() -> None:
    n = allowlist.Normalizer("anthropic")
    out = n.response({"id": "msg_A", "type": "message", "model": "m", "container": {"x": 1}, "usage": {"input_tokens": 1, "cache_read": 5},
                      "message": {"id": "msg_B"}})
    assert out == {"id": "rp-id-1", "type": "message", "model": "m", "usage": {"input_tokens": 1}, "message": {"id": "rp-id-2"}}
    assert n.response({"id": "msg_A"}) == {"id": "rp-id-1"}  # the same id stays the same


def test_normalizer_rejects_a_wrong_shape_instead_of_leaking() -> None:
    n = allowlist.Normalizer("openai-compatible")
    for bad in ({"choices": "text"}, {"usage": [1]}, {"model": {"path": "/x"}}, {"id": ""}, {"choices": [{"message": "text"}]}):
        with pytest.raises(allowlist.NormalizeError):
            n.response(bad)


def test_normalizer_idle_queue_must_be_empty() -> None:
    n = allowlist.Normalizer("comfyui")
    assert n.response({"queue_running": [], "queue_pending": [], "devices": ["gpu"]}) == {"queue_running": [], "queue_pending": []}
    with pytest.raises(allowlist.NormalizeError, match="not idle"):
        n.response({"queue_running": [1]})


def test_url_leaves_lose_host_query_and_credentials() -> None:
    n = allowlist.Normalizer("openai-compatible")
    n.protocol = "x"
    n_shape = allowlist.Normalizer.__new__(allowlist.Normalizer)
    n_shape.protocol, n_shape.ids = "x", {}
    assert n_shape._project("http://user:pw@10.0.0.7:8000/view/a.png?sig=SECRET#frag", "url", "$") == "https://replay.invalid/view/a.png"
    with pytest.raises(allowlist.NormalizeError):
        n_shape._project("file:///etc/passwd", "url", "$")


def test_request_headers_keep_only_the_allowlist() -> None:
    kept = allowlist.request_headers("openai-compatible", {"Authorization": "Bearer abc123def456", "Content-Type": "application/json", "Host": "x"})
    assert kept == {"content-type": "application/json"}


@pytest.mark.parametrize("framing,body", [
    ("sse-data", b"data: {\"a\":1}\n\n: keep-alive\n\n"), ("sse-data", b"data: {\"a\":1}"),
    ("sse-named-event", b"event: one\ndata: {\"type\":\"two\"}\n\n"), ("ndjson", b"{\"a\":1}\nnot json\n"),
    ("none", b"x"),
])
def test_a_stream_that_is_not_whole_events_is_refused(framing: str, body: bytes) -> None:
    with pytest.raises((allowlist.NormalizeError, ValueError)):
        allowlist.parse_stream(framing, body)


def test_protocol_framing_on_the_wire() -> None:
    assert PROTOCOLS["anthropic"].frame({"type": "ping"}) == b'event: ping\ndata: {"type":"ping"}\n\n'
    assert PROTOCOLS["ollama"].frame({"done": True}) == b'{"done":true}\n'
    assert PROTOCOLS["openai-compatible"].frame({"a": 1}) == b'data: {"a":1}\n\n'
    with pytest.raises(ValueError):
        PROTOCOLS["anthropic"].frame({"no": "type"})


# --- the recorder's guards --------------------------------------------------------------------------

def test_a_busy_host_sends_nothing(standalone) -> None:
    standin = _standin("openai-compatible", "/v1/chat/completions", {"json": NOISY_CHAT}, request_json={"model": "tiny-model"})
    fake = standalone.loopback(scenario_from_data(standin))
    with pytest.raises(recorder.RecordingError, match="not idle, nothing sent: GPU busy"):
        recorder.record(_service("openai-compatible", fake.base_url), "chat-text", idle_check=lambda: "GPU busy", today=TODAY)
    assert fake.requests == []


def test_responses_over_the_byte_limit_and_errors_and_redirects_are_refused(standalone) -> None:
    big = _standin("openai-compatible", "/v1/chat/completions", {"json": {"id": "x", "model": "tiny-model", "pad": "x" * 5000}}, request_json={"model": "tiny-model"})
    fake = standalone.loopback(scenario_from_data(big))
    service = _service("openai-compatible", fake.base_url, max_response_bytes=1000)
    with pytest.raises(recorder.RecordingError, match="max_response_bytes"):
        recorder.record(service, "chat-text", idle_check=IDLE, today=TODAY)
    for status, headers in ((401, {}), (302, {"location": "http://127.0.0.1:5000/"})):
        denied = _standin("openai-compatible", "/v1/chat/completions", {"status": status, "headers": headers, "json": {}}, request_json={"model": "tiny-model"})
        fake = standalone.loopback(scenario_from_data(denied))
        with pytest.raises(recorder.RecordingError, match=f"HTTP {status}"):
            recorder.record(_service("openai-compatible", fake.base_url), "chat-text", idle_check=IDLE, today=TODAY)
        assert len(fake.requests) == 1  # nothing followed, nothing retried


def test_a_set_needing_a_capability_the_service_lacks_is_refused(standalone) -> None:
    service = _service("openai-compatible", "http://127.0.0.1:18000", features=("text",))
    with pytest.raises(recorder.RecordingError, match="capability 'stream'"):
        recorder.record(service, "chat-stream", idle_check=IDLE, today=TODAY)
    service = _service("openai-compatible", "http://127.0.0.1:5000")
    with pytest.raises(recorder.RecordingError, match="forbidden gateway port"):
        recorder.record(service, "chat-text", idle_check=IDLE, today=TODAY)


def test_idle_means_no_active_inference_and_fails_closed(standalone) -> None:
    def run_with(*outputs: str, code: int = 0):
        queue = list(outputs)
        return lambda *a, **k: subprocess.CompletedProcess(a, code, queue.pop(0) if queue else queue_end, "")
    queue_end = "0\n"
    naps: list[float] = []
    assert idle.gpu_idle(run_with("0\n3\n", "5\n0\n", "1\n1\n"), naps.append) is None
    assert naps == [10, 10]  # three samples, about 10 s apart
    assert idle.gpu_idle(run_with("0\n", "0\n", "42\n"), lambda s: None) == "GPU utilization 42% in sample 3"
    assert idle.gpu_idle(run_with("6\n"), lambda s: None) == "GPU utilization 6% in sample 1"
    assert "unreadable" in idle.gpu_idle(run_with("", code=0), lambda s: None)
    assert "unreadable" in idle.gpu_idle(run_with("0\n", code=9), lambda s: None)
    assert "unreadable" in idle.gpu_idle(run_with("N/A\n"), lambda s: None)

    def queue(payload, code: int = 200) -> str:
        scenario = _standin("comfyui", "/queue", {"status": code, "json": payload}, method="GET")
        return standalone.loopback(scenario_from_data(scenario)).base_url + "/queue"

    assert idle.queue_idle(queue({"queue_running": [], "queue_pending": []})) is None
    assert idle.queue_idle(queue({"queue_running": [[0, "j"]], "queue_pending": []})) == "media queue is not empty"
    assert idle.queue_idle(queue({"queue_running": [], "queue_pending": [1]})) == "media queue is not empty"
    assert idle.queue_idle(queue({"queue_running": []})) == "media queue unreadable"
    assert idle.queue_idle(queue({}, 500)) == "media queue unreadable"


def test_waiting_for_idle_polls_but_gives_up_and_never_acts() -> None:
    clock = [0.0]
    answers = iter(["busy", "busy", None])
    napped: list[float] = []

    def nap(seconds: float) -> None:
        napped.append(seconds)
        clock[0] += seconds
    assert idle.wait_until_idle(lambda: next(answers), timeout_s=1800, poll_s=30, sleep=nap, now=lambda: clock[0]) is None
    assert napped == [30, 30]
    clock[0] = 0.0
    assert idle.wait_until_idle(lambda: "busy", timeout_s=100, poll_s=30, sleep=nap, now=lambda: clock[0]) == "busy"
    assert clock[0] <= 100


def test_the_recorder_script_checks_canaries_and_never_prints_values(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(EXAMPLE.read_text(encoding="utf-8").replace("2026-12-31", (dt.date.today() + dt.timedelta(days=30)).isoformat()), encoding="utf-8")
    run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts" / "record-upstream"), *args], capture_output=True, text=True, cwd=tmp_path)  # noqa: E731
    ok = run("--check", "--canaries", str(good))
    assert ok.returncode == 0 and "1 service row(s) valid" in ok.stdout
    bad = tmp_path / "bad.toml"
    bad.write_text(good.read_text(encoding="utf-8").replace("max_requests = 2 ", "# max_requests = 2 ").replace("127.0.0.1:18081", "127.0.0.1:5000"), encoding="utf-8")
    failed = run("--check", "--canaries", str(bad))
    assert failed.returncode == 2 and "limits.max_requests is required" in failed.stderr and "forbidden gateway port 5000" in failed.stderr


# --- policy scan and size limits -------------------------------------------------------------------------

def _tree(tmp_path: Path, data: dict, name: str = "rp-planted") -> Path:
    path = tmp_path / "tests/support/fake_upstream/scenarios" / data["protocol"] / f"{name}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("planted,category", [
    ("opk_abcdef1234567890", "credential"), ("oak_abcdef1234567890", "credential"), ("sk-proj-abcdefghijkl", "credential"),
    ("Bearer eyJhbGciOi123456", "credential"),
    ("/home/someone/models/x.gguf", "home-path"), ("C:\\\\Users\\\\someone\\\\x", "home-path"),
    ("http://192.168.1.50:8080/x", "lan-address"), ("10.1.2.3", "lan-address"), ("172.20.0.4", "lan-address"),
    ("fd12:3456::1", "lan-address"), ("::ffff:192.168.0.9", "lan-address"),
    ("\\u002fhome\\u002fsomeone\\u002fx", "home-path"), ("%2Fhome%2Fsomeone%2Fx", "home-path"),
])
def test_policy_fails_on_a_planted_value_without_echoing_it(tmp_path: Path, planted: str, category: str) -> None:
    data = {"protocol": "openai-compatible", "name": "rp-planted", "provenance": {"source": "transcript", "reference": "x"},
            "exchanges": []}
    root = _tree(tmp_path, data)
    raw = (root / "tests/support/fake_upstream/scenarios/openai-compatible/rp-planted.json")
    raw.write_text(raw.read_text(encoding="utf-8").replace('"exchanges": []', '"note": "' + planted + '", "exchanges": []'), encoding="utf-8")
    problems = replay_scan.check_root(root)
    assert any(f"disallowed {category}" in p for p in problems), problems
    assert all("someone" not in p and "abcdef" not in p and "192.168" not in p for p in problems)
    with pytest.raises(ValueError):
        transcript.validate(json.loads(raw.read_text(encoding="utf-8")), TODAY)


def test_policy_scans_every_fixtures_directory_and_passes_clean_text(tmp_path: Path) -> None:
    fixtures = tmp_path / "web/e2e/fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "clean.json").write_text('{"ip": "127.0.0.1", "host": "8.8.8.8", "task": "desk-lamp", "doc": "bearer credentials"}', encoding="utf-8")
    assert replay_scan.check_root(tmp_path) == []
    (fixtures / "leak.json").write_text('{"u": "/home/x/y"}', encoding="utf-8")
    assert replay_scan.check_root(tmp_path) == ["replay: web/e2e/fixtures/leak.json: disallowed home-path"]


def test_policy_enforces_the_metadata_and_corpus_size_limits(tmp_path: Path) -> None:
    pad = "a" * (replay_scan.METADATA_LIMIT + 1)
    data = {"protocol": "ollama", "name": "rp-big", "provenance": {"source": "transcript", "reference": "x"}, "exchanges": [], "pad": pad}
    root = _tree(tmp_path, data, "rp-big")
    assert "replay: tests/support/fake_upstream/scenarios/ollama/rp-big.json: metadata exceeds 256 KiB" in replay_scan.check_root(root)
    small = tmp_path / "small"
    media = small / replay_scan.MEDIA_DIR
    media.mkdir(parents=True)
    (media / "a.bin").write_bytes(b"\0" * (replay_scan.CORPUS_LIMIT - 10))
    assert replay_scan.check_root(small) == []
    (media / "b.bin").write_bytes(b"\0" * 11)
    assert replay_scan.check_root(small) == ["replay: corpus exceeds 20 MiB"]


def test_ci_policy_runs_the_replay_scan() -> None:
    source = (ROOT / "scripts" / "ci" / "policy.py").read_text(encoding="utf-8")
    assert "replay_scan.check_root(root)" in source
    assert replay_scan.check_root(ROOT) == []


# --- transcript rules --------------------------------------------------------------------------------------

def test_transcript_validation_rules(standalone) -> None:
    good = _chat_transcript(standalone)
    transcript.validate(good, TODAY)
    breaks = {
        "late review": lambda d: d["provenance"].update(review_by="2027-06-01"),
        "future recording": lambda d: d["provenance"].update(recorded_on="2026-12-01", review_by="2027-01-01"),
        "bad hash": lambda d: d["provenance"].update(request_set_hash="abc"),
        "extra provenance": lambda d: d["provenance"].update(host="x"),
        "header outside the allowlist": lambda d: d["exchanges"][0]["match"]["headers"].update(authorization="x"),
        "response header": lambda d: d["exchanges"][0]["response"].setdefault("headers", {}).update({"set-cookie": "x"}),
        "not normalized": lambda d: d["exchanges"][0]["response"]["json"].update(id="chatcmpl-raw"),
        "optional exchange": lambda d: d["exchanges"][0].update(required=False),
        "partial match": lambda d: d["exchanges"][0]["match"].pop("query"),
    }
    for label, mutate in breaks.items():
        data = copy.deepcopy(good)
        mutate(data)
        with pytest.raises(ValueError):
            transcript.validate(data, TODAY)
        assert label


def test_write_is_atomic_private_and_never_overwrites(standalone, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(transcript, "SCENARIO_DIR", tmp_path)
    data = _chat_transcript(standalone)
    path = transcript.write(data, TODAY)
    assert path == tmp_path / "openai-compatible" / "rp-chat-text.json" and json.loads(path.read_text(encoding="utf-8")) == data
    with pytest.raises(FileExistsError):
        transcript.write(data, TODAY)
    assert [p.name for p in path.parent.iterdir()] == ["rp-chat-text.json"]
    with pytest.raises(ValueError):
        transcript.path_for("openai-compatible", "../escape")


# --- committed transcripts (RP2 adds them under scenarios/<protocol>/rp-*.json) ------------------------------------

def _committed() -> list[Path]:
    return [p for p in sorted(SCENARIO_DIR.rglob("*.json")) if replay_scan.is_transcript(p)]


COMMITTED = ("openai-compatible/rp-chat-text", "openai-compatible/rp-chat-stream", "openai-compatible/rp-embeddings", "comfyui/rp-observe")


@pytest.mark.parametrize("name", COMMITTED)
def test_each_committed_transcript_is_valid_current_and_replays_green(standalone, name: str) -> None:
    path = SCENARIO_DIR / f"{name}.json"
    _replay_committed(standalone, path, json.loads(path.read_text(encoding="utf-8")))


def test_no_committed_transcript_goes_unlisted() -> None:
    assert sorted(p.relative_to(SCENARIO_DIR).with_suffix("").as_posix() for p in _committed()) == sorted(COMMITTED)


def test_a_model_path_echoed_by_the_upstream_keeps_only_its_last_segment() -> None:
    n = allowlist.Normalizer("openai-compatible")
    assert n.response({"model": "/media/disk/Some Dir/m-Q4.gguf"}) == {"model": "m-Q4.gguf"}
    assert n.response({"model": "C:\\models\\m.gguf"}) == {"model": "m.gguf"}
    assert n.response({"model": "plain-name"}) == {"model": "plain-name"}


def _replay_committed(standalone, path: Path, data: dict) -> None:
    transcript.validate(data)
    assert path.parent.name == data["protocol"] and path.stem == data["name"], path
    set_name = data["name"].removeprefix("rp-")
    current = recorder.load_request_sets()[data["protocol"]][set_name]
    assert data["provenance"]["request_set_hash"] == transcript.set_hash(current), f"{path.name}: request set changed; re-record or delete"
    fake = _replay(standalone, data)
    with httpx.Client(base_url=fake.base_url, trust_env=False) as client:
        for exchange in data["exchanges"]:
            match, response = exchange["match"], exchange["response"]
            got = client.request(match["method"], match["path"], json=match["json"], headers=match["headers"])
            assert got.status_code == response.get("status", 200), path
            if "json" in response:
                assert got.json() == response["json"], path
            else:
                assert allowlist.parse_stream(PROTOCOLS[data["protocol"]].framing, got.content) == response["body"], path
    assert fake.problems() == [], path


def test_the_replay_check_accepts_a_fresh_recording_and_rejects_a_stale_set(standalone, tmp_path: Path) -> None:
    data = _chat_transcript(standalone)
    path = tmp_path / "openai-compatible" / "rp-chat-text.json"
    _replay_committed(standalone, path, data)
    stale = copy.deepcopy(data)
    stale["provenance"]["request_set_hash"] = "0" * 64
    with pytest.raises(AssertionError, match="request set changed"):
        _replay_committed(standalone, path, stale)


# --- RP cells ---------------------------------------------------------------------------------------------------

def _cells_root(tmp_path: Path, cells: str, sets: dict | None = None) -> Path:
    (tmp_path / "tests/live").mkdir(parents=True)
    (tmp_path / replay_scan.REQUEST_SETS).write_text(json.dumps(sets or {"ollama": {"chat-text": {}}}), encoding="utf-8")
    (tmp_path / replay_scan.CELLS).write_text(cells, encoding="utf-8")
    return tmp_path


PENDING = '[[cell]]\nprotocol = "ollama"\nset = "chat-text"\nstatus = "pending"\nowner = "o"\nreason = "r"\nexpires_on = {date}\n'


def test_the_real_cells_cover_every_request_set_today() -> None:
    assert replay_scan.check_cells(ROOT, dt.date.today()) == []
    assert replay_scan.check_cells(ROOT, TODAY) == []


def test_cells_need_a_recorded_transcript_or_an_unexpired_pending_with_a_reason(tmp_path: Path) -> None:
    ok = _cells_root(tmp_path / "ok", PENDING.format(date="2026-12-01"))
    assert replay_scan.check_cells(ok, TODAY) == []
    assert replay_scan.check_cells(ok, dt.date(2026, 12, 2)) == ["replay cell ollama/chat-text: pending cell expired on 2026-12-01"]
    far = _cells_root(tmp_path / "far", PENDING.format(date="2027-03-01"))
    assert "more than 90 days" in replay_scan.check_cells(far, TODAY)[0]
    bare = _cells_root(tmp_path / "bare", PENDING.format(date="2026-12-01").replace('reason = "r"\n', ""))
    assert "needs owner and reason" in replay_scan.check_cells(bare, TODAY)[0]
    missing = _cells_root(tmp_path / "missing", "")
    assert replay_scan.check_cells(missing, TODAY) == ["replay cell ollama/chat-text: request set has no cell"]
    recorded = _cells_root(tmp_path / "rec", '[[cell]]\nprotocol = "ollama"\nset = "chat-text"\nstatus = "recorded"\n')
    assert "transcript is missing" in replay_scan.check_cells(recorded, TODAY)[0]
    ghost = _cells_root(tmp_path / "ghost", PENDING.format(date="2026-12-01") + PENDING.format(date="2026-12-01").replace("chat-text", "no-set"))
    assert any("no such request set" in p for p in replay_scan.check_cells(ghost, TODAY))


NOT_APPLICABLE = ('[[cell]]\nprotocol = "ollama"\nset = "chat-text"\nstatus = "not-applicable"\n'
                  'upstream_protocol = "ollama"\nreason = "no local engine speaks it upstream"\n')


def _local_presets(root: Path, *protocols: str | None) -> None:
    (root / "gateway").mkdir(parents=True, exist_ok=True)
    rows = ", ".join(f'{{"id": "p{i}", "protocol": {protocol!r}}}' for i, protocol in enumerate(protocols))
    (root / replay_scan.LOCAL_PRESETS).write_text(f"PROVIDER_PRESETS: tuple = ({rows},)\n", encoding="utf-8")


def test_the_shipped_local_presets_are_read_as_data() -> None:
    assert replay_scan.local_upstream_protocols(ROOT) == {"openai-compatible"}


def test_a_not_applicable_cell_holds_only_while_no_local_preset_speaks_its_protocol(tmp_path: Path) -> None:
    absent = _cells_root(tmp_path / "absent", NOT_APPLICABLE)
    _local_presets(absent, None)
    assert replay_scan.check_cells(absent, TODAY) == []
    present = _cells_root(tmp_path / "present", NOT_APPLICABLE)
    _local_presets(present, None, "ollama")
    assert replay_scan.check_cells(present, TODAY) == [
        "replay cell ollama/chat-text: a local engine preset speaks ollama upstream; record it (status recorded or pending)"]
    bare = _cells_root(tmp_path / "bare", NOT_APPLICABLE.replace('upstream_protocol = "ollama"\n', ""))
    _local_presets(bare, None)
    assert replay_scan.check_cells(bare, TODAY) == ["replay cell ollama/chat-text: not-applicable needs upstream_protocol and reason"]


def test_a_local_upstream_protocol_without_a_request_set_fails(tmp_path: Path) -> None:
    root = _cells_root(tmp_path / "uncovered", NOT_APPLICABLE)
    _local_presets(root, "openai-compatible")
    assert replay_scan.check_cells(root, TODAY) == [
        "replay: a local engine preset speaks openai-compatible upstream, but no request set covers it"]

