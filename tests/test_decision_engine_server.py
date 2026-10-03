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
