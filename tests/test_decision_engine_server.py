from __future__ import annotations

import json
import signal
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
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, _exc_type, _exc, _tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _handler_for(scorer):
    class TestHandler(server.Handler):
        pass

    TestHandler.scorer = scorer
    TestHandler.model_id = "decider-test"
    TestHandler.engine = "cpu"
    TestHandler.placement = "cpu"
    TestHandler.residency = "on_demand"
    TestHandler.request_lock = threading.Lock()
    TestHandler.admission = threading.BoundedSemaphore(5)
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
    with _ThreadedServer(_handler_for(scorer)) as base_url:
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending = [pool.submit(_post_json, base_url, "/v1/systemone", _systemone_body(str(i))) for i in range(5)]
            assert scorer.entered.wait(timeout=5)
            time.sleep(0.2)

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


def test_cuda_startup_failure_falls_back_to_cpu(monkeypatch):
    popen_calls: list[tuple[list[str], dict]] = []
    procs = [FakeProc(), FakeProc()]

    def fake_popen(cmd, env=None, stdout=None, stderr=None):
        popen_calls.append((list(cmd), dict(env or {})))
        return procs[len(popen_calls) - 1]

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    health = iter([False, True])
    monkeypatch.setattr(server, "wait_health", lambda _port, _timeout: next(health))
    current = {"process": None}
    args = SimpleNamespace(
        placement="cuda:0",
        llama_server="llama-server",
        model="/models/decider.gguf",
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


def _post_json_with_headers(base_url: str, path: str, body: dict, *, headers: dict[str, str], timeout: float = 1.0) -> tuple[int, dict, dict]:
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
                timeout=1,
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
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as resp:
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


def test_disconnect_during_first_row_stops_later_rows():
    scorer = MultiRowBlockingScorer()
    handler = _handler_for(scorer)
    with _ThreadedServer(handler) as base_url:
        worker = threading.Thread(target=_raw_post_then_close, args=(base_url, "/v1/systemone", _multirow_body()))
        worker.start()
        assert scorer.first_entered.wait(timeout=2)
        scorer.release_first.set()
        worker.join(timeout=2)

    assert scorer.calls == [0]


def test_expired_budget_during_multirow_stops_before_second_row():
    scorer = MultiRowBlockingScorer()
    handler = _handler_for(scorer)
    with _ThreadedServer(handler) as base_url:
        worker = threading.Thread(
            target=_post_json_with_headers,
            args=(base_url, "/v1/systemone", _multirow_body()),
            kwargs={"headers": {"x-decision-deadline-ms": "50"}, "timeout": 1},
        )
        worker.start()
        assert scorer.first_entered.wait(timeout=2)
        time.sleep(0.08)
        scorer.release_first.set()
        worker.join(timeout=2)

    assert scorer.calls == [0]


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
                timeout=1,
            )
        finally:
            handler.request_lock.release()

    assert status == 504
    assert body["error"]["code"] == "deadline_exceeded"
    assert scorer.total_calls == 0


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
    deadline = time.monotonic() + 1.0
    ctx = decision_engine.ScoreContext(state="x", question="q", options=("no", "yes"), kind="noul", index=0)

    client.prepare_request([ctx, ctx], deadline=deadline)
    with pytest.raises(decision_engine.DecisionReadoutError):
        client.score_options(ctx, deadline=deadline)

    assert len(observed) == 3
    assert all(0 < timeout <= 1.0 for timeout in observed)


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
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeHTTPServer)

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

    assert server.wait_health(18024, 0.2, expected_model="decider-test") is True
