from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from types import ModuleType, SimpleNamespace

import pytest

from engines import decision_engine_server as server
from gateway import decision_engine


def test_reset_usage_does_not_clear_another_clients_counters() -> None:
    first = object.__new__(server.LlamaServerClient)
    second = object.__new__(server.LlamaServerClient)
    first.input_tokens, first.output_tokens = 3, 1
    second.input_tokens, second.output_tokens = 8, 2

    first.reset_usage()

    assert first.usage() == {"input_tokens": 0, "output_tokens": 0}
    assert second.usage() == {"input_tokens": 8, "output_tokens": 2}


def test_shared_prefix_does_not_prime_past_shortest_prompt_or_first_difference():
    assert server._shared_prefix_len([[5, 6, 7], [5, 6], [5, 6, 8]]) == 2
    assert server._shared_prefix_len([[5, 6], [8, 6]]) == 0


def test_placement_plan_distinguishes_cpu_only_from_gpu_with_cpu_fallback():
    cpu = (("cpu", "cpu", "", "0"),)
    assert server._placement_attempts("cpu") == cpu
    assert server._placement_attempts("gpu") == (("gpu", "cuda:0", "0", "99"), *cpu)
    assert server._placement_attempts("cuda:2") == (("gpu", "cuda:2", "2", "99"), *cpu)


def test_tokenizer_adapter_preserves_special_token_choice_and_copies_ids() -> None:
    calls = []
    ids = [21, 34]

    def encode(text: str, *, add_special_tokens: bool) -> SimpleNamespace:
        calls.append((text, add_special_tokens))
        return SimpleNamespace(ids=ids)

    adapter = server.TokenizerAdapter(SimpleNamespace(encode=encode))
    default_ids = adapter.encode("café")
    assert default_ids == [21, 34]
    default_ids.append(55)
    assert adapter.encode("café", add_special_tokens=True) == [21, 34]
    assert ids == [21, 34]
    assert calls == [("café", False), ("café", True)]


def test_tokenizer_adapter_counts_added_tokens_in_vocabulary() -> None:
    def get_vocab_size(*, with_added_tokens: bool) -> int:
        return 130 if with_added_tokens else 128

    adapter = server.TokenizerAdapter(SimpleNamespace(get_vocab_size=get_vocab_size))
    assert adapter.vocab_size() == 130


def _systemone_body(question_id: str = "q") -> dict:
    return {
        "state": "A customer says payouts failed for three days.",
        "questions": {question_id: {"type": "noul", "instructions": "Is this urgent?"}},
    }


def _post_json(base_url: str, path: str, body: dict) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read())


def _post_raw(base_url: str, path: str, content: bytes) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=content,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read())


class _ThreadedServer:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        # A short poll lets shutdown() return at once instead of after up to half a second.
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, _exc_type, _exc, _tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _CountingSemaphore(threading.BoundedSemaphore):
    """Reports when `target` admissions are held, so a test can wait on that fact."""

    def __init__(self, value: int, target: int) -> None:
        super().__init__(value)
        self.target = target
        self.at_target = threading.Event()
        self._held = 0
        self._count_lock = threading.Lock()

    def acquire(self, blocking=True, timeout=None):
        got = super().acquire(blocking, timeout)
        if got:
            with self._count_lock:
                self._held += 1
                if self._held >= self.target:
                    self.at_target.set()
        return got


def _handler_for(scorer, admission=None):
    class TestHandler(server.Handler):
        pass

    TestHandler.scorer = scorer
    TestHandler.model_id = "decider-test"
    TestHandler.engine = "cpu"
    TestHandler.placement = "cpu"
    TestHandler.residency = "on_demand"
    TestHandler.request_lock = threading.Lock()
    TestHandler.admission = admission or threading.BoundedSemaphore(5)
    return TestHandler


class BlockingScorer:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def reset_usage(self) -> None:
        return None

    def usage(self) -> dict[str, int]:
        return {"input_tokens": 1, "output_tokens": 1}

    def score_options(self, _ctx: decision_engine.ScoreContext):
        with self.lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return {0: -2.0, 1: -0.1}


def test_handler_admits_five_requests_and_rejects_sixth_when_queue_full():
    scorer = BlockingScorer()
    admission = _CountingSemaphore(5, target=5)
    with _ThreadedServer(_handler_for(scorer, admission)) as base_url:
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending = [pool.submit(_post_json, base_url, "/v1/systemone", _systemone_body(str(i))) for i in range(5)]
            assert scorer.entered.wait(timeout=5)
            # The sixth request may only overflow once all five hold a slot.
            assert admission.at_target.wait(timeout=5)

            status, headers, body = _post_json(base_url, "/v1/systemone", _systemone_body("overflow"))
            assert status == 529
            assert headers["Retry-After"] == "1"
            assert body["error"]["code"] == "overloaded"

            scorer.release.set()
            results = [future.result(timeout=5) for future in pending]

    assert [status for status, _headers, _body in results] == [200, 200, 200, 200, 200]
    assert scorer.calls == 5


class UsageScorer:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.total_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.lock = threading.Lock()

    def reset_usage(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def usage(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    def score_options(self, _ctx: decision_engine.ScoreContext):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.total_calls += 1
        time.sleep(0.05)
        self.input_tokens += 11
        self.output_tokens += 1
        with self.lock:
            self.active -= 1
        return {0: -0.3, 1: -0.2}


def _post_headers_only(base_url: str, content_length: str | None) -> tuple[int, dict]:
    from http.client import HTTPResponse
    from urllib.parse import urlparse

    address = urlparse(base_url)
    with socket.create_connection((address.hostname, address.port), timeout=5) as conn:
        headers = [
            "POST /v1/systemone HTTP/1.0",
            "Host: localhost",
            "Content-Type: application/json",
        ]
        if content_length is not None:
            headers.append(f"Content-Length: {content_length}")
        # Do not send a body or close the write side. A response must be based
        # on the headers, not EOF rescuing an unbounded/blocking body read.
        conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = HTTPResponse(conn)
        try:
            response.begin()
            return response.status, json.loads(response.read())
        finally:
            response.close()


class _ReleasedAdmission(threading.BoundedSemaphore):
    def __init__(self) -> None:
        super().__init__(1)
        self.released = threading.Event()

    def release(self, n=1):
        super().release(n)
        self.released.set()


@pytest.mark.parametrize(
    ("length", "status", "code"),
    [
        pytest.param("-1", 400, "invalid_request", id="negative"),
        pytest.param("abc", 400, "invalid_request", id="nonnumeric"),
        pytest.param(str(1024 * 1024 + 1), 413, "request_too_large", id="oversized"),
        pytest.param("9" * 5000, 413, "request_too_large", id="oversized-many-digits"),
    ],
)
def test_handler_refuses_invalid_length_before_body_and_releases_slot(length, status, code):
    scorer = UsageScorer()
    admission = _ReleasedAdmission()
    with _ThreadedServer(_handler_for(scorer, admission)) as base_url:
        actual, body = _post_headers_only(base_url, length)
        assert actual == status
        assert body["error"]["code"] == code
        assert body["error"]["type"] == "invalid_request_error"
        assert scorer.total_calls == 0
        assert admission.released.wait(timeout=5)
        assert admission.acquire(blocking=False)
        admission.release()
        assert _post_json(base_url, "/v1/systemone", _systemone_body())[0] == 200
    assert scorer.total_calls == 1


@pytest.mark.parametrize("length", [None, "0"], ids=["missing", "zero"])
def test_handler_absent_body_keeps_empty_body_refusal(length):
    scorer = UsageScorer()
    admission = _ReleasedAdmission()
    with _ThreadedServer(_handler_for(scorer, admission)) as base_url:
        expected_status, _headers, expected = _post_raw(base_url, "/v1/systemone", b"")
        assert admission.released.wait(timeout=5)
        admission.released.clear()
        status, body = _post_headers_only(base_url, length)
        assert status == expected_status == 422
        assert body == expected
        assert admission.released.wait(timeout=5)
        assert admission.acquire(blocking=False)
        admission.release()
    assert scorer.total_calls == 0


@pytest.mark.parametrize("at_limit", [False, True], ids=["normal", "at-limit"])
def test_handler_accepts_body_within_request_limit(at_limit):
    assert server.MAX_REQUEST_BYTES == 1024 * 1024
    scorer = UsageScorer()
    payload = json.dumps(_systemone_body()).encode("utf-8")
    if at_limit:
        payload += b" " * (server.MAX_REQUEST_BYTES - len(payload))
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        status, _headers, body = _post_raw(base_url, "/v1/systemone", payload)
    assert status == 200
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 1}
    assert scorer.total_calls == 1


def test_handler_serializes_request_wide_usage_for_concurrent_calls():
    scorer = UsageScorer()
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_post_json, base_url, "/v1/systemone", _systemone_body(str(i))) for i in range(2)]
            results = [future.result(timeout=5) for future in futures]

    assert [status for status, _headers, _body in results] == [200, 200]
    assert [body["usage"] for _status, _headers, body in results] == [
        {"input_tokens": 11, "output_tokens": 1},
        {"input_tokens": 11, "output_tokens": 1},
    ]
    assert scorer.total_calls == 2
    assert scorer.max_active == 1


def test_handler_malformed_json_returns_422():
    scorer = UsageScorer()
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        status, _headers, body = _post_raw(base_url, "/v1/systemone", b'{"state":')

    assert status == 422
    assert body["error"] == {
        "message": "Request body must be valid JSON.",
        "type": "invalid_request_error",
        "code": "invalid_json",
    }
    assert scorer.total_calls == 0


@pytest.mark.parametrize("failure_type", [decision_engine.DecisionReadoutError, RuntimeError])
def test_handler_reports_scoring_failure_and_reuses_both_slots(monkeypatch, failure_type):
    body = json.dumps(_systemone_body()).encode("utf-8")
    replies = []
    failure_message = "Synthetic scoring failure."

    def fail(*args, **kwargs):
        raise failure_type(failure_message)

    monkeypatch.setattr(decision_engine, "build_systemone_response", fail)
    handler = SimpleNamespace(
        path="/v1/systemone",
        admission=threading.BoundedSemaphore(1),
        request_lock=threading.Lock(),
        headers=SimpleNamespace(
            get_all=lambda name: [str(len(body))] if name == "content-length" else [],
            get=lambda *args: None,
        ),
        max_request_body_bytes=len(body),
        _read_request_body=lambda deadline, length: body,
        _client_disconnected=lambda: False,
        _json=lambda status, payload: replies.append((status, payload)),
        scorer=object(),
        model_id="synthetic",
        engine="synthetic",
        placement="synthetic",
        residency="synthetic",
    )

    server.Handler.do_POST(handler)

    assert replies == [(502, decision_engine.error_body(
        failure_message, code="decision_engine_error", kind="server_error",
    ))]
    assert handler.admission.acquire(blocking=False)
    handler.admission.release()
    assert handler.request_lock.acquire(blocking=False)
    handler.request_lock.release()


def test_context_overflow_returns_422_before_llama_completion(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []

    prompt = ModuleType("decider.prompt")
    prompt.MAX_OPTIONS = 255
    prompt.letter_ids = lambda _tokenizer: [101, 102]
    prompt.build = lambda *_args, **_kwargs: {"ids": list(range(9)), "slots": [8], "nopts": [2]}
    temperature = ModuleType("decider.temperature")
    temperature.from_config = lambda _cfg: ((1.0, {}), None)
    temperature.for_types = lambda _t, _by_type, kinds: [1.0 for _kind in kinds]
    decider = ModuleType("decider")
    decider.temperature = temperature

    tokenizers = ModuleType("tokenizers")

    class FakeTokenizer:
        @staticmethod
        def from_file(_path):
            return SimpleNamespace(
                encode=lambda *_args, **_kwargs: SimpleNamespace(ids=[]),
                get_vocab_size=lambda with_added_tokens=True: 32000,
            )

    tokenizers.Tokenizer = FakeTokenizer
    numpy = ModuleType("numpy")
    numpy.asarray = lambda value: value

    monkeypatch.setitem(sys.modules, "decider", decider)
    monkeypatch.setitem(sys.modules, "decider.prompt", prompt)
    monkeypatch.setitem(sys.modules, "decider.temperature", temperature)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)
    monkeypatch.setitem(sys.modules, "numpy", numpy)

    gguf = tmp_path / "model.gguf"
    gguf.write_text("fake")
    (tmp_path / "decider_config.json").write_text("{}")
    (tmp_path / "tokenizer.json").write_text("{}")
    client = server.LlamaServerClient(base_url="http://127.0.0.1:1", gguf=str(gguf), ctx_tokens=4)
    client._post = lambda path, body: calls.append((path, dict(body))) or {}

    with pytest.raises(decision_engine.DecisionInputError) as excinfo:
        client.score_options(decision_engine.ScoreContext(state="x", question="q", options=("no", "yes"), kind="noul", index=0))

    assert excinfo.value.code == "context_length_exceeded"
    assert calls == []


@pytest.mark.parametrize("placement", ["cuda:", "cuda:device"])
def test_placement_rejects_missing_or_non_numeric_device(placement):
    with pytest.raises(RuntimeError, match=f"^invalid DECIDER_PLACEMENT: {placement}$"):
        server._placement_attempts(placement)


def test_placement_rejects_an_unknown_backend():
    with pytest.raises(RuntimeError, match="^invalid DECIDER_PLACEMENT: unknown-device$"):
        server._placement_attempts("unknown-device")


class FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if not self.terminated and not self.killed else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        ("cpu", (("cpu", "cpu", "", "0"),)),
        ("cuda:2", (("gpu", "cuda:2", "2", "99"), ("cpu", "cpu", "", "0"))),
    ],
    ids=["explicit-cpu-stays-cpu", "explicit-gpu-precedes-cpu-fallback"],
)
def test_placement_plan_preserves_explicit_device_and_fallback_order(placement, expected):
    assert server._placement_attempts(placement) == expected


def test_cuda_startup_failure_falls_back_to_cpu(monkeypatch):
    popen_calls: list[tuple[list[str], dict]] = []
    procs = [FakeProc(), FakeProc()]

    def fake_popen(cmd, env=None, stdout=None, stderr=None):
        popen_calls.append((list(cmd), dict(env or {})))
        return procs[len(popen_calls) - 1]

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    health = iter([False, True])
    monkeypatch.setattr(server, "wait_health", lambda _port, _timeout, *, expected_model=None, process=None: next(health))
    current = {"process": None}
    args = SimpleNamespace(
        placement="cuda:0",
        llama_server="llama-server",
        model="/models/decider.gguf",
        model_id="decider-test",
        ctx=4096,
        threads=8,
        llama_port=18024,
        startup_timeout_s=0.01,
    )

    result = server.start_llama(args, current_process=current)

    assert result == {"process": procs[1], "engine": "cpu", "placement": "cpu"}
    assert popen_calls[0][1]["CUDA_VISIBLE_DEVICES"] == "0"
    assert popen_calls[1][1]["CUDA_VISIBLE_DEVICES"] == ""
    assert popen_calls[0][0][popen_calls[0][0].index("-ngl") + 1] == "99"
    assert popen_calls[1][0][popen_calls[1][0].index("-ngl") + 1] == "0"
    assert procs[0].terminated and procs[0].wait_calls == 1 and not procs[0].killed
    assert current["process"] is procs[1]
    assert all("systemctl" not in cmd for cmd, _env in popen_calls)


@pytest.mark.parametrize("placement", ["accelerator", "cuda:device"])
def test_startup_rejects_unusable_placement_before_starting_child(monkeypatch, placement):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid placement must not probe ports or start a child")

    monkeypatch.setattr(server, "_port_is_available", unexpected)
    monkeypatch.setattr(server.subprocess, "Popen", unexpected)
    current_process = {"process": None}
    with pytest.raises(RuntimeError) as error:
        server.start_llama(SimpleNamespace(placement=placement), current_process=current_process)
    assert str(error.value) == f"invalid DECIDER_PLACEMENT: {placement}"
    assert current_process == {"process": None}


def test_sigterm_during_startup_cleans_owned_child_without_systemctl(monkeypatch):
    captured_handlers = {}
    proc = FakeProc()

    def fake_signal(signum, handler):
        captured_handlers[signum] = handler

    def fake_start_llama(_args, *, current_process=None):
        current_process["process"] = proc
        captured_handlers[signal.SIGTERM](signal.SIGTERM, None)
        raise AssertionError("SIGTERM handler should exit before startup returns")

    monkeypatch.setattr(server, "parse_args", lambda: SimpleNamespace(port=0, llama_port=0, model="/fake", model_id="decider", ctx=4, residency="on_demand"))
    monkeypatch.setattr(server.signal, "signal", fake_signal)
    monkeypatch.setattr(server, "start_llama", fake_start_llama)

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert excinfo.value.code == 0
    assert signal.SIGTERM in captured_handlers
    assert proc.terminated and proc.wait_calls == 1 and not proc.killed


def test_prepare_request_validates_all_contexts_before_prime_post(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []

    prompt = ModuleType("decider.prompt")
    prompt.MAX_OPTIONS = 255
    prompt.letter_ids = lambda _tokenizer: [101, 102]
    build_calls = []

    def fake_build(*_args, **_kwargs):
        build_calls.append(len(build_calls))
        if len(build_calls) == 1:
            return {"ids": [1, 2, 3], "slots": [2], "nopts": [2]}
        return {"ids": list(range(9)), "slots": [8], "nopts": [2]}

    prompt.build = fake_build
    temperature = ModuleType("decider.temperature")
    temperature.from_config = lambda _cfg: ((1.0, {}), None)
    temperature.for_types = lambda _t, _by_type, kinds: [1.0 for _kind in kinds]
    decider = ModuleType("decider")
    decider.temperature = temperature

    tokenizers = ModuleType("tokenizers")

    class FakeTokenizer:
        @staticmethod
        def from_file(_path):
            return SimpleNamespace(
                encode=lambda *_args, **_kwargs: SimpleNamespace(ids=[]),
                get_vocab_size=lambda with_added_tokens=True: 32000,
            )

    tokenizers.Tokenizer = FakeTokenizer
    numpy = ModuleType("numpy")
    numpy.asarray = lambda value: value

    monkeypatch.setitem(sys.modules, "decider", decider)
    monkeypatch.setitem(sys.modules, "decider.prompt", prompt)
    monkeypatch.setitem(sys.modules, "decider.temperature", temperature)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)
    monkeypatch.setitem(sys.modules, "numpy", numpy)

    gguf = tmp_path / "model.gguf"
    gguf.write_text("fake")
    (tmp_path / "decider_config.json").write_text("{}")
    (tmp_path / "tokenizer.json").write_text("{}")
    client = server.LlamaServerClient(base_url="http://127.0.0.1:1", gguf=str(gguf), ctx_tokens=4)
    client._post = lambda path, body: calls.append((path, dict(body))) or {}

    contexts = [
        decision_engine.ScoreContext(state="x", question="q1", options=("no", "yes"), kind="noul", index=0),
        decision_engine.ScoreContext(state="x", question="q2", options=("no", "yes"), kind="noul", index=1),
    ]
    with pytest.raises(decision_engine.DecisionInputError) as excinfo:
        client.prepare_request(contexts)

    assert excinfo.value.code == "context_length_exceeded"
    assert calls == []
    assert len(build_calls) == 2


def test_prompt_builder_preserves_caller_option_order_and_stops_at_answer_slot() -> None:
    # The caller's option sequence is the source of truth; the prompt builder
    # receives a copy in that order, rather than another ordering table.
    options = ("Later", "Now", "Maybe")
    tokenizer = object()

    def build(example, given_tokenizer, order, *, max_options, max_ctx_tokens):
        assert given_tokenizer is tokenizer
        assert (max_options, max_ctx_tokens) == (12, 4)
        assert example.context == "A choice"
        assert len(example.qs) == 1
        question = example.qs[0]
        assert question.text == "When?"
        order.shuffle(question.options)
        assert question.options == list(options)
        return {"ids": [9, 8, 7, 6, 5], "slots": [2], "nopts": [3]}

    client = object.__new__(server.LlamaServerClient)
    client._build = build
    client.tokenizer = tokenizer
    client._max_options = 12
    client.ctx_tokens = 4
    context = decision_engine.ScoreContext(
        state="A choice", question="When?", options=options, kind="noul", index=0,
    )
    assert client._prompt_for_context(context) == ([9, 8, 7], 3)


# Client-side socket timeout only: it turns a hung server into a failure. The
# oracle is the server's own response (504 deadline_exceeded, 503 unhealthy);
# a client timeout shorter than a scheduler stall would replace it with a
# URLError on a correct run.
_TRANSPORT_WATCHDOG_S = 30.0


def _post_json_with_headers(base_url: str, path: str, body: dict, *, headers: dict[str, str], timeout: float = _TRANSPORT_WATCHDOG_S) -> tuple[int, dict, dict]:
    request_headers = {"content-type": "application/json", **headers}
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read())


def test_deadline_expires_while_waiting_for_request_lock_without_scoring():
    scorer = UsageScorer()
    handler = _handler_for(scorer)
    handler.request_lock.acquire()
    with _ThreadedServer(handler) as base_url:
        try:
            status, _headers, body = _post_json_with_headers(
                base_url,
                "/v1/systemone",
                _systemone_body("deadline"),
                headers={"x-decision-deadline-ms": "50"},
            )
        finally:
            handler.request_lock.release()

    assert status == 504
    assert body["error"]["code"] == "deadline_exceeded"
    assert scorer.total_calls == 0


def test_health_reports_unhealthy_when_owned_child_has_exited():
    scorer = UsageScorer()
    handler = _handler_for(scorer)
    child = FakeProc()
    child.terminate()
    handler.llama_process = child

    with _ThreadedServer(handler) as base_url:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=_TRANSPORT_WATCHDOG_S) as resp:
                status = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read())

    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["reason"] == "llama_child_exited"


def test_startup_rejects_health_from_unowned_server_after_child_exits(monkeypatch):
    proc = FakeProc()
    proc.terminate()
    popen_calls: list[list[str]] = []

    def fake_popen(cmd, env=None, stdout=None, stderr=None):
        popen_calls.append(list(cmd))
        return proc

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "wait_health", lambda _port, _timeout, *, expected_model=None, process=None: True)
    args = SimpleNamespace(
        placement="cpu",
        llama_server="llama-server",
        model="/models/decider.gguf",
        model_id="decider-test",
        ctx=4096,
        threads=8,
        llama_port=18024,
        startup_timeout_s=0.01,
    )

    with pytest.raises(RuntimeError, match="occupied|owned|exited"):
        server.start_llama(args, current_process={"process": None})

    assert len(popen_calls) == 1


class MultiRowBlockingScorer:
    def __init__(self) -> None:
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.calls: list[int] = []

    def reset_usage(self) -> None:
        return None

    def usage(self) -> dict[str, int]:
        return {"input_tokens": len(self.calls), "output_tokens": len(self.calls)}

    def score_options(self, ctx: decision_engine.ScoreContext):
        self.calls.append(ctx.index)
        if ctx.index == 0:
            self.first_entered.set()
            assert self.release_first.wait(timeout=2)
        return {0: -2.0, 1: -0.1}


def test_batch_prefix_does_not_count_matching_tokens_after_a_divergence():
    assert server._shared_prefix_len([[11, 21, 40], [11, 22, 40], [11, 23, 40]]) == 1


def test_batch_prefix_stops_at_shortest_row_and_handles_empty_input():
    assert server._shared_prefix_len([[11, 21, 40], [11, 21], [11, 21, 50]]) == 2
    assert server._shared_prefix_len([[11, 21], []]) == 0
    assert server._shared_prefix_len([]) == 0


def _multirow_body() -> dict:
    return {
        "state": "state",
        "questions": {
            "first": {"type": "noul", "instructions": "First?"},
            "second": {"type": "noul", "instructions": "Second?"},
        },
    }


def _raw_post_then_close(base_url: str, path: str, body: dict, *, headers: dict[str, str] | None = None) -> None:
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    conn = HTTPConnection(parsed.hostname, parsed.port, timeout=1)
    payload = json.dumps(body).encode("utf-8")
    conn.putrequest("POST", path)
    conn.putheader("Host", parsed.netloc)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(len(payload)))
    for name, value in (headers or {}).items():
        conn.putheader(name, value)
    conn.endheaders(payload)
    conn.close()


def test_disconnected_queued_request_does_not_score_and_admission_is_reusable():
    scorer = UsageScorer()
    handler = _handler_for(scorer)
    handler.request_lock.acquire()
    with _ThreadedServer(handler) as base_url:
        _raw_post_then_close(base_url, "/v1/systemone", _systemone_body("closed"))
        handler.request_lock.release()
        status, _headers, body = _post_json(base_url, "/v1/systemone", _systemone_body("next"))

    assert status == 200
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 1}
    assert scorer.total_calls == 1




def _raw_post_wait_then_close(base_url: str, path: str, body: dict, ready: threading.Event) -> None:
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    conn = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    payload = json.dumps(body).encode("utf-8")
    conn.putrequest("POST", path)
    conn.putheader("Host", parsed.netloc)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(len(payload)))
    conn.endheaders(payload)
    assert ready.wait(timeout=2)
    conn.close()

def test_disconnect_during_first_row_stops_later_rows():
    scorer = MultiRowBlockingScorer()
    handler = _handler_for(scorer)
    with _ThreadedServer(handler) as base_url:
        worker = threading.Thread(target=_raw_post_wait_then_close, args=(base_url, "/v1/systemone", _multirow_body(), scorer.first_entered))
        worker.start()
        assert scorer.first_entered.wait(timeout=2)
        worker.join(timeout=2)
        scorer.release_first.set()

    assert scorer.calls == [0]


class _SteppedClock:
    """A monotonic clock that moves only when the test advances it."""

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def test_expired_budget_during_multirow_stops_before_second_row(monkeypatch):
    clock = _SteppedClock()
    monkeypatch.setattr(server, "time", clock)
    monkeypatch.setattr(decision_engine, "time", clock)
    scorer = MultiRowBlockingScorer()
    handler = _handler_for(scorer)
    with _ThreadedServer(handler) as base_url:
        worker = threading.Thread(
            target=_post_json_with_headers,
            args=(base_url, "/v1/systemone", _multirow_body()),
            kwargs={"headers": {"x-decision-deadline-ms": "50"}, "timeout": 5},
        )
        worker.start()
        assert scorer.first_entered.wait(timeout=5)
        clock.now += 1.0  # the 50 ms budget ends while the first row is scoring
        scorer.release_first.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert scorer.calls == [0]


def test_expired_decision_reports_deadline_before_checking_disconnect(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.0)

    def disconnected():
        pytest.fail("the expired request should not inspect the connection")

    with pytest.raises(server.DecisionDeadlineExceeded, match="decision deadline exceeded"):
        server._raise_if_stopped(10.0, disconnected)


def test_live_deadline_reports_a_disconnected_caller(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 10.0)
    with pytest.raises(server.DecisionCancelled, match="client disconnected"):
        server._raise_if_stopped(11.0, lambda: True)


def test_remaining_timeout_bounds_explicit_waits_without_changing_absent_deadline(monkeypatch):
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: 10.0))
    assert server._remaining_timeout(None) is None
    assert server._remaining_timeout(10.25) == 0.25
    assert server._remaining_timeout(70.0) == server.DEFAULT_REQUEST_TIMEOUT_S
    assert server._remaining_timeout(10.0005) == server.MIN_UPSTREAM_TIMEOUT_S


def test_invalid_or_nonfinite_deadline_header_does_not_disable_default_cap():
    scorer = UsageScorer()
    handler = _handler_for(scorer)
    handler.request_lock.acquire()
    with _ThreadedServer(handler) as base_url:
        try:
            status, _headers, body = _post_json_with_headers(
                base_url,
                "/v1/systemone",
                _systemone_body("bad_deadline"),
                headers={"x-decision-deadline-ms": "NaN"},
            )
        finally:
            handler.request_lock.release()

    assert status == 504
    assert body["error"]["code"] == "deadline_exceeded"
    assert scorer.total_calls == 0


def test_deadline_wins_without_calling_disconnect_probe(monkeypatch):
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: 10.0))

    def disconnected():
        pytest.fail("an expired deadline must not probe the connection")

    with pytest.raises(server.DecisionDeadlineExceeded, match="decision deadline exceeded"):
        server._raise_if_stopped(10.0, disconnected)


def test_cancelled_busy_lock_never_enters_timed_acquire(monkeypatch):
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: 10.0))
    attempts = []

    def acquire(**kwargs):
        attempts.append(kwargs)
        return False

    with pytest.raises(server.DecisionCancelled, match="client disconnected"):
        server._acquire_until(SimpleNamespace(acquire=acquire), 11.0, lambda: True)
    assert attempts == [{"blocking": False}]


def test_post_timeout_uses_remaining_budget_for_prime_rows_and_retry(monkeypatch, tmp_path):
    prompt = ModuleType("decider.prompt")
    prompt.MAX_OPTIONS = 255
    prompt.letter_ids = lambda _tokenizer: [101, 102]
    prompt.build = lambda *_args, **_kwargs: {"ids": [1, 2, 3], "slots": [2], "nopts": [2]}
    temperature = ModuleType("decider.temperature")
    temperature.from_config = lambda _cfg: ((1.0, {}), None)
    temperature.for_types = lambda _t, _by_type, kinds: [1.0 for _kind in kinds]
    decider = ModuleType("decider")
    decider.temperature = temperature
    tokenizers = ModuleType("tokenizers")

    class FakeTokenizer:
        @staticmethod
        def from_file(_path):
            return SimpleNamespace(
                encode=lambda *_args, **_kwargs: SimpleNamespace(ids=[]),
                get_vocab_size=lambda with_added_tokens=True: 32000,
            )

    tokenizers.Tokenizer = FakeTokenizer
    numpy = ModuleType("numpy")
    numpy.asarray = lambda value: value
    monkeypatch.setitem(sys.modules, "decider", decider)
    monkeypatch.setitem(sys.modules, "decider.prompt", prompt)
    monkeypatch.setitem(sys.modules, "decider.temperature", temperature)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)
    monkeypatch.setitem(sys.modules, "numpy", numpy)

    gguf = tmp_path / "model.gguf"
    gguf.write_text("fake")
    (tmp_path / "decider_config.json").write_text("{}")
    (tmp_path / "tokenizer.json").write_text("{}")
    client = server.LlamaServerClient(base_url="http://127.0.0.1:1", gguf=str(gguf), ctx_tokens=4)
    observed: list[float] = []

    def fake_post(_path, _body, *, timeout):
        observed.append(timeout)
        return {"completion_probabilities": [{"top_logprobs": [{"id": 101, "logprob": -1.0}]}]}

    client._post = fake_post
    clock = _SteppedClock()  # the 1 s budget cannot be spent by scheduler delay
    monkeypatch.setattr(server, "time", clock)
    deadline = clock.now + 1.0
    ctx = decision_engine.ScoreContext(state="x", question="q", options=("no", "yes"), kind="noul", index=0)

    client.prepare_request([ctx, ctx], deadline=deadline)
    with pytest.raises(decision_engine.DecisionReadoutError):
        client.score_options(ctx, deadline=deadline)

    assert len(observed) == 3
    assert all(0 < timeout <= 1.0 for timeout in observed)


@pytest.mark.parametrize("option_count, expected_limits", [(2, [100, 1000]), (101, [1000])])
def test_incomplete_scores_report_missing_option_without_booking_usage(
    option_count, expected_limits
) -> None:
    client = object.__new__(server.LlamaServerClient)
    client._lock = threading.Lock()
    client.letter_ids = list(range(200, 200 + option_count))
    client.vocab_size = 1000
    client.input_tokens = 7
    client.output_tokens = 3
    client._prompt_for_context = lambda _ctx: ([2, 3], option_count)
    limits = []

    def transport(_path, body):
        limits.append(body["n_probs"])
        return {"completion_probabilities": [{"top_logprobs": [{"id": 200, "logprob": -1.0}]}]}

    client._post = transport
    with pytest.raises(decision_engine.DecisionReadoutError, match="^missing logprob for option index 1$"):
        client.score_options(SimpleNamespace(kind="fictional"))

    assert limits == expected_limits
    assert client.usage() == {"input_tokens": 7, "output_tokens": 3}


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"completion_probabilities": []},
        {"completion_probabilities": [{}]},
        {"completion_probabilities": [{"top_logprobs": "not-a-list"}]},
    ],
)
def test_score_prompt_rejects_empty_or_malformed_score_rows(response) -> None:
    client = object.__new__(server.LlamaServerClient)
    client.vocab_size = 1000
    client.letter_ids = [11, 12]
    client._post = lambda _path, _body, **_controls: response

    with pytest.raises(decision_engine.DecisionReadoutError, match="no score row"):
        client._score_prompt([1, 2], 2, full_vocab=False)


def test_score_prompt_rejects_invalid_score_entries() -> None:
    client = object.__new__(server.LlamaServerClient)
    client.vocab_size = 1000
    client.letter_ids = [11, 12]
    client._post = lambda _path, _body, **_controls: {
        "completion_probabilities": [{"top_logprobs": [{"id": 11}]}]
    }

    with pytest.raises(decision_engine.DecisionReadoutError, match="invalid score entry"):
        client._score_prompt([1, 2], 2, full_vocab=False)


def test_score_prompt_returns_logprobs_for_valid_score_row() -> None:
    client = object.__new__(server.LlamaServerClient)
    client.vocab_size = 1000
    client.letter_ids = [11, 12]
    client._post = lambda _path, _body, **_controls: {
        "completion_probabilities": [
            {"top_logprobs": [{"id": 11, "logprob": -0.5}, {"id": 12, "logprob": -1.5}]}
        ]
    }

    assert client._score_prompt([1, 2], 2, full_vocab=False) == {11: -0.5, 12: -1.5}

def test_main_exits_nonzero_when_owned_child_exits_after_ready(monkeypatch):
    proc = FakeProc()

    class FakeHTTPServer:
        def __init__(self, _addr, _handler):
            return None

        def serve_forever(self):
            proc.terminate()
            return None

        def shutdown(self):
            return None

    monkeypatch.setattr(server, "parse_args", lambda: SimpleNamespace(port=0, llama_port=0, model="/fake", model_id="decider", ctx=4, residency="on_demand"))
    monkeypatch.setattr(server, "start_llama", lambda _args, current_process=None: {"process": proc, "engine": "cpu", "placement": "cpu"})
    monkeypatch.setattr(server, "LlamaServerClient", lambda **_kwargs: UsageScorer())
    monkeypatch.setattr(server, "DecisionThreadingHTTPServer", FakeHTTPServer)

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert excinfo.value.code != 0


def test_startup_checks_child_port_before_popen(monkeypatch):
    monkeypatch.setattr(server, "_port_is_available", lambda _host, _port: False, raising=False)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")))
    args = SimpleNamespace(
        placement="cpu",
        llama_server="llama-server",
        model="/models/decider.gguf",
        model_id="decider-test",
        ctx=4096,
        threads=8,
        llama_port=18024,
        startup_timeout_s=0.01,
    )

    with pytest.raises(RuntimeError, match="port|occupied"):
        server.start_llama(args, current_process={"process": None})


@pytest.mark.parametrize("model_given", [False, True])
def test_parse_args_reports_missing_model_before_startup(monkeypatch, tmp_path, capsys, model_given):
    for name in ("DECIDER_MODEL", "DECIDER_PORT", "DECIDER_LLAMA_PORT", "DECIDER_THREADS",
                 "DECIDER_CTX", "DECIDER_STARTUP_TIMEOUT_S", "DECIDER_RESIDENCY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DECIDER_MODEL_ID", "decider-test")
    missing = tmp_path / "absent.gguf"
    argv = ["decision-engine", "--model", str(missing)] if model_given else ["decision-engine"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        server.parse_args()
    assert exc.value.code == 2
    expected = f"model path does not exist: {missing}" if model_given else "--model or DECIDER_MODEL is required"
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [False, True], ids=["environment", "cli-overrides"])
def test_parse_args_selects_model_and_port_without_starting_engine(tmp_path, monkeypatch, explicit):
    env_model = tmp_path / "environment.gguf"
    cli_model = tmp_path / "command-line.gguf"
    env_model.touch()
    cli_model.touch()
    for name in ("DECIDER_LLAMA_PORT", "DECIDER_THREADS", "DECIDER_CTX", "DECIDER_STARTUP_TIMEOUT_S", "DECIDER_RESIDENCY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DECIDER_MODEL", str(env_model))
    monkeypatch.setenv("DECIDER_MODEL_ID", "decider-test")
    monkeypatch.setenv("DECIDER_PORT", "8101")
    argv = ["decision-engine"]
    if explicit:
        argv += ["--model", str(cli_model), "--port", "8102"]
    monkeypatch.setattr(sys, "argv", argv)

    args = server.parse_args()

    assert (args.model, args.port) == ((str(cli_model), 8102) if explicit else (str(env_model), 8101))


@pytest.mark.parametrize("source", ["environment", "cli", "none"])
def test_model_identity_comes_only_from_configuration(monkeypatch, tmp_path, capsys, source):
    """The engine names no model of its own: the id is DECIDER_MODEL_ID or --model-id, else startup refuses."""
    model = tmp_path / "synthetic.gguf"
    model.touch()
    monkeypatch.delenv("DECIDER_MODEL_ID", raising=False)
    argv = ["decision-engine", "--model", str(model)]
    if source == "environment":
        monkeypatch.setenv("DECIDER_MODEL_ID", "from-environment")
    if source == "cli":
        argv += ["--model-id", "from-cli"]
    monkeypatch.setattr(sys, "argv", argv)
    if source == "none":
        with pytest.raises(SystemExit) as exc:
            server.parse_args()
        assert exc.value.code == 2
        assert "--model-id or DECIDER_MODEL_ID is required" in capsys.readouterr().err
    else:
        assert server.parse_args().model_id == f"from-{source}"


def test_explicit_threads_overrides_invalid_environment_default(monkeypatch, tmp_path):
    model = tmp_path / "synthetic.gguf"
    model.touch()
    monkeypatch.setenv("DECIDER_THREADS", "not-an-integer")
    monkeypatch.setenv("DECIDER_MODEL_ID", "decider-test")
    monkeypatch.setattr(sys, "argv", [
        "decision-engine", "--model", str(model), "--threads", "4",
    ])

    assert server.parse_args().threads == 4


def test_numeric_environment_defaults_are_converted_when_used(monkeypatch, tmp_path):
    model = tmp_path / "synthetic.gguf"
    model.touch()
    monkeypatch.setenv("DECIDER_THREADS", "3")
    monkeypatch.setenv("DECIDER_STARTUP_TIMEOUT_S", "2.5")
    monkeypatch.setenv("DECIDER_MODEL_ID", "decider-test")
    monkeypatch.setattr(sys, "argv", ["decision-engine", "--model", str(model)])

    args = server.parse_args()
    assert (args.threads, args.startup_timeout_s) == (3, 2.5)


def test_wait_health_requires_expected_model_identity(monkeypatch):
    responses = iter([{"status": "ok", "model": "wrong-model"}, {"status": "ok", "model": "decider-test"}])

    class FakeResponse:
        status = 200

        def __enter__(self):
            self.body = next(responses)
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return None

        def read(self):
            return json.dumps(self.body).encode("utf-8")

    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    # The scripted responses end the poll, not the clock: the budget is only a
    # watchdog and must not be a scheduler-speed limit between the two replies.
    assert server.wait_health(18024, 30, expected_model="decider-test") is True
    assert next(responses, None) is None, "the wrong-model reply was skipped, not examined"


def test_cancellable_post_decodes_chunked_json_response():
    response = b'{"ok": true}'
    seen_request = threading.Event()

    def serve(listener):
        conn, _addr = listener.accept()
        with conn:
            conn.recv(4096)
            seen_request.set()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Content-Type: application/json\r\n"
                b"\r\n"
                b"4\r\n{\"ok\r\n"
                b"8\r\n\": true}\r\n"
                b"0\r\n\r\n"
            )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    thread = threading.Thread(target=serve, args=(listener,), daemon=True)
    thread.start()
    try:
        port = listener.getsockname()[1]
        body = server._post_json_cancellable(f"http://127.0.0.1:{port}", "/completion", {}, deadline=time.monotonic() + 2, cancel_check=None)
    finally:
        listener.close()
        thread.join(timeout=2)

    assert body == {"ok": True}
    assert seen_request.is_set()


def test_cancellable_post_closes_upstream_socket_on_cancel_before_response():
    got_eof = threading.Event()
    accepted = threading.Event()
    cancel = threading.Event()

    def serve(listener):
        conn, _addr = listener.accept()
        accepted.set()
        with conn:
            conn.recv(4096)
            cancel.set()
            conn.settimeout(2)
            while True:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    return
                if data == b"":
                    got_eof.set()
                    return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    thread = threading.Thread(target=serve, args=(listener,), daemon=True)
    thread.start()
    try:
        port = listener.getsockname()[1]
        with pytest.raises(Exception):
            server._post_json_cancellable(
                f"http://127.0.0.1:{port}",
                "/completion",
                {},
                deadline=time.monotonic() + 15,
                cancel_check=cancel.is_set,
            )
    finally:
        listener.close()
        thread.join(timeout=2)

    assert accepted.is_set()
    assert got_eof.is_set()


def test_startup_rejects_foreign_same_model_listener_even_with_live_child(monkeypatch):
    proc = FakeProc()
    proc.pid = 99999999
    monkeypatch.setattr(server, "_port_is_available", lambda _host, _port: True)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(server, "_matches_expected_model", lambda _port, _body, _expected_model: True)
    monkeypatch.setattr(server, "_owned_process_listens", lambda _process, _port: False)

    responses = iter([{"status": "ok"}])

    class FakeResponse:
        status = 200

        def __enter__(self):
            self.body = next(responses)
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return None

        def read(self):
            return json.dumps(self.body).encode("utf-8")

    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    args = SimpleNamespace(
        placement="cpu",
        llama_server="llama-server",
        model="/models/decider.gguf",
        model_id="decider-test",
        ctx=4096,
        threads=8,
        llama_port=18024,
        startup_timeout_s=0.01,
    )

    with pytest.raises(RuntimeError, match="healthy|owned|listener"):
        server.start_llama(args, current_process={"process": None})


def test_cancellable_post_closes_close_response_socket_during_drip_body():
    got_eof = threading.Event()
    sent_headers = threading.Event()
    cancel = threading.Event()

    def serve(listener):
        conn, _addr = listener.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 1000000\r\n"
                b"Content-Type: application/json\r\n"
                b"\r\n"
            )
            sent_headers.set()
            cancel.set()
            conn.settimeout(2)
            try:
                while True:
                    conn.sendall(b" ")
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        return
                    if data == b"":
                        got_eof.set()
                        return
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, OSError):
                got_eof.set()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    thread = threading.Thread(target=serve, args=(listener,), daemon=True)
    thread.start()
    try:
        port = listener.getsockname()[1]
        with pytest.raises(server.DecisionCancelled):
            server._post_json_cancellable(
                f"http://127.0.0.1:{port}",
                "/completion",
                {},
                deadline=time.monotonic() + 15,
                cancel_check=cancel.is_set,
            )
    finally:
        listener.close()
        thread.join(timeout=2)

    assert sent_headers.is_set()
    assert got_eof.is_set()


def test_cancellable_post_deadline_closes_close_response_socket_during_drip_body(monkeypatch):
    clock = _SteppedClock()
    monkeypatch.setattr(server, "time", clock)
    got_eof = threading.Event()
    body_started = threading.Event()
    connections: list[socket.socket] = []

    class DeadlineResponse(server.http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            # Only the stepped deadline should end this real socket read.
            sock.settimeout(None)
            super().__init__(sock, *args, **kwargs)

        def read(self, amt=None):
            if body_started.is_set() or amt == 0:
                return super().read(amt)
            first = super().read(1)
            assert first == b" "
            # Receiving a body byte proves headers have been consumed and the
            # Connection: close response owns the socket, unlike peer sendall.
            clock.now += 1000
            body_started.set()
            remaining = amt if amt is None or amt < 0 else amt - 1
            return first + super().read(remaining)

    class DeadlineConnection(server.http.client.HTTPConnection):
        response_class = DeadlineResponse

        def __init__(self, *args, **kwargs):
            # A frozen logical 50 ms budget is not a real-time socket timeout.
            kwargs["timeout"] = None
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(server.http.client, "HTTPConnection", DeadlineConnection)

    def serve(listener):
        conn, _addr = listener.accept()
        connections.append(conn)
        with conn:
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 1000000\r\n"
                b"Content-Type: application/json\r\n"
                b"\r\n"
            )
            try:
                conn.sendall(b" ")
                while True:
                    # Stay open until the client closes; a peer-side timeout
                    # must not manufacture EOF before the deadline is observed.
                    data = conn.recv(4096)
                    if data == b"":
                        got_eof.set()
                        return
            except (BrokenPipeError, ConnectionResetError, OSError):
                got_eof.set()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    thread = threading.Thread(target=serve, args=(listener,), daemon=True)
    thread.start()
    outcome: list = []

    def call():
        try:
            server._post_json_cancellable(
                f"http://127.0.0.1:{listener.getsockname()[1]}",
                "/completion",
                {},
                deadline=clock.now + 0.05,
                cancel_check=None,
            )
        except BaseException as exc:  # noqa: BLE001 - the test inspects the type
            outcome.append(exc)

    caller = threading.Thread(target=call, daemon=True)
    try:
        caller.start()
        # These are failure watchdogs, not events that cause the deadline.
        assert body_started.wait(timeout=5)
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], server.DecisionDeadlineExceeded), outcome
        assert got_eof.wait(timeout=2)
    finally:
        listener.close()
        # Clean up even if the deadline watchdog fails. Assertions above must
        # observe client-initiated EOF, never EOF manufactured by this teardown.
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        thread.join(timeout=2)
        caller.join(timeout=5)

    assert not thread.is_alive()


def test_decision_http_server_uses_daemon_request_threads() -> None:
    assert server.DecisionThreadingHTTPServer.daemon_threads is True


def test_pid_owns_listener_detects_current_process_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        assert server._pid_owns_listener(os.getpid(), port) is True
        assert server._pid_owns_listener(99999999, port) is False
    finally:
        listener.close()


def test_cancellable_post_rechecks_cancel_after_connect_before_post(monkeypatch) -> None:
    cancel = threading.Event()
    sent: list[str] = []

    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False
            self.shutdown_called = False

        def shutdown(self, _how):
            self.shutdown_called = True

        def close(self):
            self.closed = True

    fake_socket = FakeSocket()

    class FakeConnection:
        def __init__(self, _host, _port, timeout=None):
            self.sock = None

        def connect(self):
            self.sock = fake_socket
            cancel.set()

        def putrequest(self, method, path):
            sent.append(f"{method} {path}")

        def close(self):
            fake_socket.close()

    monkeypatch.setattr(server.http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(server.DecisionCancelled):
        server._post_json_cancellable(
            "http://127.0.0.1:18024",
            "/completion",
            {},
            deadline=time.monotonic() + 15,
            cancel_check=cancel.is_set,
        )

    assert sent == []
    assert fake_socket.shutdown_called
    assert fake_socket.closed


@pytest.mark.parametrize("deadline_header", [None, "500"])
def test_incomplete_bodies_release_all_admission_slots(monkeypatch, deadline_header):
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    monkeypatch.setattr(server, "DEFAULT_REQUEST_TIMEOUT_S", 0.5)
    class Admission(_CountingSemaphore):
        def __init__(self):
            super().__init__(5, target=5)
            self.released = 0
            self.all_released = threading.Event()

        def release(self, n=1):
            super().release(n)
            with self._count_lock:
                self._held -= n
                self.released += n
                if self.released == 5:
                    self.all_released.set()

    admission = Admission()
    scorer = UsageScorer()
    connections = []
    with _ThreadedServer(_handler_for(scorer, admission)) as base_url:
        parsed = urlparse(base_url)
        try:
            for _ in range(5):
                conn = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
                connections.append(conn)
                conn.putrequest("POST", "/v1/systemone")
                conn.putheader("Content-Length", "100")
                if deadline_header is not None:
                    conn.putheader("x-decision-deadline-ms", deadline_header)
                conn.endheaders()
            assert admission.at_target.wait(timeout=2)
            assert admission.all_released.wait(timeout=2)
            assert scorer.total_calls == 0
            for conn in connections:
                response = conn.getresponse()
                assert response.status == 504
                assert json.loads(response.read())["error"]["code"] == "deadline_exceeded"
            status, _, _ = _post_json(base_url, "/v1/systemone", _systemone_body("after"))
            assert status == 200
            assert scorer.total_calls == 1
        finally:
            for conn in connections:
                conn.close()


def test_trickled_body_cannot_extend_absolute_deadline(monkeypatch):
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    scorer = UsageScorer()
    received, advance = threading.Event(), threading.Event()
    now = [100.0]
    # Replace only this module's clock object, not the shared time module.
    clock = SimpleNamespace(**vars(time))
    clock.monotonic = lambda: now[0]
    monkeypatch.setattr(server, "time", clock)
    handler = _handler_for(scorer)
    original = handler._read_request_body
    chunks = []

    class ObservedReader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read1(self, size):
            chunk = self.stream.read1(size)
            if chunk:
                chunks.append(chunk)
                received.set()
                assert advance.wait(timeout=3), "test did not advance the clock"
            return chunk

    def observed_read(self, deadline, length):
        stream = self.rfile
        self.rfile = ObservedReader(stream)
        try:
            return original(self, deadline, length)
        finally:
            self.rfile = stream

    monkeypatch.setattr(handler, "_read_request_body", observed_read)
    with _ThreadedServer(handler) as base_url:
        parsed = urlparse(base_url)
        conn = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            conn.putrequest("POST", "/v1/systemone")
            conn.putheader("Content-Length", "1000")
            conn.putheader("x-decision-deadline-ms", "200")
            conn.endheaders(b" ")
            assert received.wait(timeout=3), "handler never returned a body chunk"
            assert chunks == [b" "]
            # More traffic is queued before the reader resumes past its deadline.
            conn.send(b" ")
            now[0] += 0.201
            advance.set()
            response = conn.getresponse()
            assert response.status == 504
            assert json.loads(response.read())["error"]["code"] == "deadline_exceeded"
            assert scorer.total_calls == 0
        finally:
            advance.set()
            conn.close()


@pytest.mark.parametrize(("length", "status", "code"), [
    ("-1", 400, "invalid_request"), ("bad", 400, "invalid_request"),
    ("17", 413, "request_too_large"), ("1, 2", 400, "invalid_request"),
])
def test_invalid_or_oversize_body_length_is_rejected(length, status, code):
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    scorer = UsageScorer()
    handler = _handler_for(scorer)
    handler.max_request_body_bytes = 16
    with _ThreadedServer(handler) as base_url:
        parsed = urlparse(base_url)
        conn = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            conn.putrequest("POST", "/v1/systemone")
            conn.putheader("Content-Length", length)
            conn.endheaders()
            response = conn.getresponse()
            assert response.status == status
            assert json.loads(response.read())["error"]["code"] == code
            assert scorer.total_calls == 0
        finally:
            conn.close()


@pytest.mark.parametrize("framing", ["conflicting", "transfer-encoding"])
def test_ambiguous_body_framing_is_rejected_and_connection_closed(framing):
    from http.client import HTTPResponse
    from urllib.parse import urlparse

    scorer = UsageScorer()
    body = json.dumps(_systemone_body()).encode("utf-8")
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        parsed = urlparse(base_url)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as conn:
            headers = [
                "POST /v1/systemone HTTP/1.1",
                "Host: localhost",
                "Connection: keep-alive",
                f"Content-Length: {len(body)}",
            ]
            if framing == "conflicting":
                headers.append(f"Content-Length: {len(body) + 1}")
            else:
                headers.append("Transfer-Encoding: chunked")
            conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
            response = HTTPResponse(conn)
            try:
                response.begin()
                assert response.status == 400
                assert json.loads(response.read())["error"]["code"] == "invalid_request"
                assert conn.recv(1) == b"", "rejected framing must close the same connection"
                assert scorer.total_calls == 0
            finally:
                response.close()

def _post_length_fields(base_url: str, lengths: list[str], payload: bytes = b"") -> tuple[int, dict]:
    from http.client import HTTPResponse
    from urllib.parse import urlparse

    address = urlparse(base_url)
    with socket.create_connection((address.hostname, address.port), timeout=5) as conn:
        headers = [
            "POST /v1/systemone HTTP/1.0",
            "Host: localhost",
            "Content-Type: application/json",
            *[f"Content-Length: {value}" for value in lengths],
        ]
        # Keep the write side open: neither EOF nor teardown may unblock the read.
        conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + payload)
        response = HTTPResponse(conn)
        try:
            response.begin()
            return response.status, json.loads(response.read())
        finally:
            response.close()


@pytest.mark.parametrize("lengths", [
    pytest.param(["1", "-1"], id="negative-second"),
    pytest.param(["-1", "1"], id="negative-first"),
    pytest.param(["1", str(1024 * 1024 + 1)], id="oversized-second"),
    pytest.param(["1", "2"], id="different-valid"),
    pytest.param(["1, -1"], id="comma-negative"),
    pytest.param(["1, 2"], id="comma-conflict"),
    pytest.param(["1", ""], id="empty-second"),
    pytest.param(["1,"], id="empty-comma-token"),
])
def test_handler_refuses_conflicting_length_fields_before_body_and_releases_slot(lengths):
    scorer = UsageScorer()
    admission = _ReleasedAdmission()
    with _ThreadedServer(_handler_for(scorer, admission)) as base_url:
        status, body = _post_length_fields(base_url, lengths)
        assert status == 400
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["message"] == "Content-Length must be a non-negative integer."
        assert scorer.total_calls == 0
        assert admission.released.wait(timeout=5)
        assert admission.acquire(blocking=False)
        admission.release()
        assert _post_json(base_url, "/v1/systemone", _systemone_body())[0] == 200
    assert scorer.total_calls == 1


@pytest.mark.parametrize("form", ["separate", "comma", "mixed", "zero-padded"])
def test_handler_accepts_identical_length_fields(form):
    scorer = UsageScorer()
    payload = json.dumps(_systemone_body()).encode("utf-8")
    length = str(len(payload))
    fields = {
        "separate": [length, length],
        "comma": [f"{length}, {length}"],
        "mixed": [length, f"{length}, {length}"],
        "zero-padded": ["00" + length, length],
    }
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        status, body = _post_length_fields(base_url, fields[form], payload)
    assert status == 200
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 1}
    assert scorer.total_calls == 1


def test_scoring_failure_preserves_previous_usage_and_releases_lock():
    client = object.__new__(server.LlamaServerClient)
    client._lock = threading.Lock()
    client.letter_ids = [11, 12]
    client.input_tokens, client.output_tokens = 7, 2
    client._prompt_for_context = lambda _ctx: ([1, 2, 3], 2)
    calls = []

    def score(_prompt, _count, *, full_vocab, **_controls):
        calls.append(full_vocab)
        return {11: -1.0}

    client._score_prompt = score
    with pytest.raises(decision_engine.DecisionReadoutError, match="missing logprob for option index 1"):
        client.score_options(None)
    assert calls == [False, True]
    assert client.usage() == {"input_tokens": 7, "output_tokens": 2}
    assert not client._lock.locked()


def test_scoring_backend_error_propagates_without_accounting_or_retry():
    client = object.__new__(server.LlamaServerClient)
    client._lock = threading.Lock()
    client.letter_ids = [11, 12]
    client.input_tokens, client.output_tokens = 7, 2
    client._prompt_for_context = lambda _ctx: ([1, 2, 3], 2)
    failure = RuntimeError("fixture scoring unavailable")
    calls = []

    def score(_prompt, _count, *, full_vocab, **_controls):
        calls.append(full_vocab)
        raise failure

    client._score_prompt = score
    with pytest.raises(RuntimeError, match="fixture scoring unavailable") as caught:
        client.score_options(None)
    assert caught.value is failure
    assert calls == [False]
    assert client.usage() == {"input_tokens": 7, "output_tokens": 2}
    assert not client._lock.locked()


def test_handler_identical_two_byte_lengths_preserve_request_validation():
    scorer = UsageScorer()
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        expected_status, _headers, expected = _post_raw(base_url, "/v1/systemone", b"{}")
        status, body = _post_length_fields(base_url, ["2", "2"], b"{}")
        assert status == expected_status == 422
        assert body == expected
        assert _post_json(base_url, "/v1/systemone", _systemone_body())[0] == 200
    assert scorer.total_calls == 1


def test_shared_prompt_prefix_ends_at_first_difference_or_shortest_row() -> None:
    assert server._shared_prefix_len([[2, 5, 8], [2, 5], [2, 5, 9]]) == 2
    assert server._shared_prefix_len([[2, 5, 8], [2, 7, 8], [2, 5, 8]]) == 1


def test_shared_prompt_prefix_is_zero_for_empty_or_first_element_mismatch():
    assert server._shared_prefix_len([]) == 0
    assert server._shared_prefix_len([[1, 2], []]) == 0
    assert server._shared_prefix_len([[1, 2], [2, 2]]) == 0


def test_structured_state_indexes_long_nested_lists_only() -> None:
    rows = [{"label": "café"}] + list(range(1, 8))
    rendered = server._render_state({"rows": rows, "short": ["keep"]})

    assert json.loads(rendered) == {
        "rows": [{"_index": 0, "label": "café"}]
        + [{"_index": i, "value": i} for i in range(1, 8)],
        "short": ["keep"],
    }
    assert "café" in rendered
    assert server._render_state("already rendered: café") == "already rendered: café"


def test_shared_prompt_prefix_has_no_tokens_for_empty_batches_or_rows() -> None:
    assert server._shared_prefix_len([]) == 0
    assert server._shared_prefix_len([[4, 8], [], [4, 8]]) == 0


def test_render_state_indexes_separate_nested_lists_without_mutating_input() -> None:
    child = list(range(8))
    value = {"groups": [{"rows": child}, {"rows": [9]}]}
    rendered = json.loads(server._render_state(value))

    assert rendered == {
        "groups": [
            {"rows": [{"_index": i, "value": i} for i in range(8)]},
            {"rows": [9]},
        ],
    }
    assert child == list(range(8))
    assert value == {"groups": [{"rows": list(range(8))}, {"rows": [9]}]}
