#!/usr/bin/env python3
"""Serve TypeSafe ``/v1/systemone`` over a stock llama-server.

The wrapper keeps decider prompt/readout knowledge outside gateway core. It can
run the configured 2B or 4B model on CPU by default, or on a configured CUDA
placement with CPU fallback when startup health never arrives. The scoring loop
follows the measured bench path: build the model's
single-token answer prompts, call llama-server ``/completion`` with
``n_probs``, then calibrate option probabilities locally.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from urllib.parse import urlparse
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import decision_engine

DEFAULT_REQUEST_TIMEOUT_S = 15.0
MIN_UPSTREAM_TIMEOUT_S = 0.001


class DecisionThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@dataclass(frozen=True)
class PromptQuestion:
    text: str
    options: list[str]
    gold: int = 0


@dataclass(frozen=True)
class PromptExample:
    context: str
    qs: list[PromptQuestion]


class NoShuffle:
    def shuffle(self, _items: list[Any]) -> None:
        return None


class LlamaServerClient(decision_engine.OptionScorer):
    def __init__(self, *, base_url: str, gguf: str, ctx_tokens: int) -> None:
        try:
            from decider.prompt import MAX_OPTIONS, build, letter_ids
            from decider import temperature as decider_temperature
        except Exception as exc:  # pragma: no cover - depends on operator package
            raise RuntimeError("decider-ai is required for live prompt assembly") from exc
        try:
            from tokenizers import Tokenizer
        except Exception as exc:  # pragma: no cover - depends on operator package
            raise RuntimeError("tokenizers is required for live prompt assembly") from exc
        try:
            import numpy as np
        except Exception as exc:  # pragma: no cover - depends on operator package
            raise RuntimeError("numpy is required for live probability readout") from exc

        self.base_url = base_url.rstrip("/")
        self.ctx_tokens = ctx_tokens
        self._decider_temperature = decider_temperature
        self._build = build
        self._max_options = MAX_OPTIONS
        self._np = np
        folder = os.path.dirname(os.path.abspath(gguf))
        with open(os.path.join(folder, "decider_config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        (self.T, self.T_by_type), _ = decider_temperature.from_config(cfg)
        self.tokenizer = TokenizerAdapter(Tokenizer.from_file(os.path.join(folder, "tokenizer.json")))
        self.vocab_size = self.tokenizer.vocab_size()
        self.letter_ids = [int(x) for x in np.asarray(letter_ids(self.tokenizer))]
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0

    def reset_usage(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def usage(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    def prepare_request(
        self,
        contexts: Sequence[decision_engine.ScoreContext],
        *,
        deadline: float | None = None,
        cancel_check: Any | None = None,
    ) -> None:
        if len(contexts) <= 1:
            return
        with self._lock:
            prepared = [self._prompt_for_context(ctx) for ctx in contexts]
            rows = [prompt_ids for prompt_ids, _option_count in prepared]
            prefix_len = _shared_prefix_len(rows)
            if prefix_len <= 0:
                return
            prefix_ids = [int(token_id) for token_id in rows[0][:prefix_len]]
            self._post_with_controls(
                "/completion",
                {"prompt": prefix_ids, "n_predict": 0, "cache_prompt": True},
                deadline=deadline,
                cancel_check=cancel_check,
            )

    def score_options(
        self,
        ctx: decision_engine.ScoreContext,
        *,
        deadline: float | None = None,
        cancel_check: Any | None = None,
    ) -> Mapping[int, float]:
        with self._lock:
            prompt_ids, option_count = self._prompt_for_context(ctx)
            by_token = self._score_prompt(prompt_ids, option_count, full_vocab=option_count > 100, deadline=deadline, cancel_check=cancel_check)
            missing = [index for index in range(option_count) if self.letter_ids[index] not in by_token]
            if missing and option_count <= 100:
                by_token = self._score_prompt(prompt_ids, option_count, full_vocab=True, deadline=deadline, cancel_check=cancel_check)
                missing = [index for index in range(option_count) if self.letter_ids[index] not in by_token]
            if missing:
                raise decision_engine.DecisionReadoutError(f"missing logprob for option index {missing[0]}")
            self.input_tokens += len(prompt_ids)
            self.output_tokens += 1
        temperature = self._decider_temperature.for_types(self.T, self.T_by_type, [ctx.kind])[0]
        out: dict[int, float] = {}
        for index in range(option_count):
            token_id = self.letter_ids[index]
            if token_id in by_token:
                out[index] = by_token[token_id] / float(temperature)
        return out

    def _prompt_for_context(self, ctx: decision_engine.ScoreContext) -> tuple[list[int], int]:
        item = self._build(
            PromptExample(_render_state(ctx.state), [PromptQuestion(ctx.question, list(ctx.options), 0)]),
            self.tokenizer,
            NoShuffle(),
            max_options=self._max_options,
            max_ctx_tokens=self.ctx_tokens,
        )
        slot = item["slots"][0]
        prompt_ids = [int(token_id) for token_id in item["ids"][: slot + 1]]
        if len(prompt_ids) > self.ctx_tokens:
            raise decision_engine.DecisionInputError(
                f"decision prompt has {len(prompt_ids)} tokens; runtime context is {self.ctx_tokens}",
                code="context_length_exceeded",
                param="state",
            )
        return prompt_ids, int(item["nopts"][0])

    def _score_prompt(
        self,
        prompt_ids: list[int],
        option_count: int,
        *,
        full_vocab: bool,
        deadline: float | None = None,
        cancel_check: Any | None = None,
    ) -> dict[int, float]:
        payload = {
            "prompt": prompt_ids,
            "n_predict": 1,
            "n_probs": self.vocab_size if full_vocab else max(100, option_count),
            "logit_bias": [[self.letter_ids[index], 100.0] for index in range(option_count)],
            "cache_prompt": True,
            "temperature": 0,
            "post_sampling_probs": False,
        }
        raw = self._post_with_controls("/completion", payload, deadline=deadline, cancel_check=cancel_check)
        top_logprobs = raw["completion_probabilities"][0]["top_logprobs"]
        return {int(entry["id"]): float(entry["logprob"]) for entry in top_logprobs}

    def _post_with_controls(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        deadline: float | None = None,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        import inspect

        try:
            params = inspect.signature(self._post).parameters
        except (TypeError, ValueError):
            params = {}
        if "deadline" in params or "cancel_check" in params:
            return self._post(path, body, deadline=deadline, cancel_check=cancel_check)
        if "timeout" in params:
            return self._post(path, body, timeout=_remaining_timeout(deadline))
        return self._post(path, body)

    def _post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        deadline: float | None = None,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        return _post_json_cancellable(self.base_url, path, body, deadline=deadline, cancel_check=cancel_check)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise decision_engine.DecisionReadoutError("decision deadline exceeded")
    return max(MIN_UPSTREAM_TIMEOUT_S, min(remaining, DEFAULT_REQUEST_TIMEOUT_S))


def _post_json_cancellable(
    base_url: str,
    path: str,
    body: Mapping[str, Any],
    *,
    deadline: float | None,
    cancel_check: Any | None,
) -> dict[str, Any]:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        raise RuntimeError(f"unsupported llama-server URL: {base_url}")
    effective_deadline = deadline if deadline is not None else time.monotonic() + DEFAULT_REQUEST_TIMEOUT_S
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=_remaining_timeout(effective_deadline))
    stop = threading.Event()
    owned_socket: dict[str, socket.socket | None] = {"sock": None}

    def close_owned_socket() -> None:
        sock = owned_socket.get("sock") or conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass

    def cancel_watcher() -> None:
        while not stop.wait(0.01):
            try:
                _raise_if_stopped(effective_deadline, cancel_check)
            except (DecisionDeadlineExceeded, DecisionCancelled):
                close_owned_socket()
                return

    watcher = threading.Thread(target=cancel_watcher, daemon=True)
    watcher.start()
    response: http.client.HTTPResponse | None = None
    try:
        _raise_if_stopped(effective_deadline, cancel_check)
        conn.connect()
        owned_socket["sock"] = conn.sock
        _raise_if_stopped(effective_deadline, cancel_check)
        conn.putrequest("POST", path)
        conn.putheader("Host", f"{parsed.hostname}:{parsed.port}")
        conn.putheader("Content-Type", "application/json")
        payload = json.dumps(body).encode("utf-8")
        conn.putheader("Content-Length", str(len(payload)))
        conn.putheader("Connection", "close")
        conn.endheaders(payload)
        response = conn.getresponse()
        data = response.read()
        _raise_if_stopped(effective_deadline, cancel_check)
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"llama-server returned HTTP {response.status}")
        return json.loads(data)
    except (DecisionDeadlineExceeded, DecisionCancelled):
        close_owned_socket()
        raise
    except (http.client.IncompleteRead, BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
        try:
            _raise_if_stopped(effective_deadline, cancel_check)
        except (DecisionDeadlineExceeded, DecisionCancelled):
            close_owned_socket()
            raise
        raise RuntimeError(f"llama-server request failed: {exc}") from exc
    finally:
        stop.set()
        close_owned_socket()
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        watcher.join(timeout=0.2)


def _shared_prefix_len(rows: Sequence[Sequence[int]]) -> int:
    if not rows:
        return 0
    prefix_len = 0
    while all(len(row) > prefix_len and row[prefix_len] == rows[0][prefix_len] for row in rows):
        prefix_len += 1
    return prefix_len


def _annotate_indices(value: Any, min_len: int = 8) -> Any:
    if isinstance(value, list):
        if len(value) >= min_len:
            return [
                {"_index": index, **_annotate_indices(item, min_len)}
                if isinstance(item, dict)
                else {"_index": index, "value": _annotate_indices(item, min_len)}
                for index, item in enumerate(value)
            ]
        return [_annotate_indices(item, min_len) for item in value]
    if isinstance(value, dict):
        return {key: _annotate_indices(item, min_len) for key, item in value.items()}
    return value


def _render_state(state: Any) -> str:
    if isinstance(state, str):
        return state
    return json.dumps(_annotate_indices(state), ensure_ascii=False)


class TokenizerAdapter:
    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer
        self.chat_template = None

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return list(self._tokenizer.encode(text, add_special_tokens=add_special_tokens).ids)

    def vocab_size(self) -> int:
        return int(self._tokenizer.get_vocab_size(with_added_tokens=True))


class Handler(BaseHTTPRequestHandler):
    scorer: decision_engine.OptionScorer
    model_id: str
    engine: str
    placement: str
    residency: str
    llama_process: subprocess.Popen[Any] | None = None
    request_lock = threading.Lock()
    admission = threading.BoundedSemaphore(5)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            proc = self.llama_process
            if proc is not None and proc.poll() is not None:
                self._json(
                    503,
                    {
                        "status": "unhealthy",
                        "reason": "llama_child_exited",
                        "engine": self.engine,
                        "placement": self.placement,
                        "residency": self.residency,
                        "model": self.model_id,
                    },
                )
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "engine": self.engine,
                    "placement": self.placement,
                    "residency": self.residency,
                    "model": self.model_id,
                },
            )
            return
        if self.path in {"/v1/models", "/typesafe/v1/models"}:
            self._json(200, {"object": "list", "data": [{"id": self.model_id, "object": "model", "owned_by": "local"}]})
            return
        self._json(404, decision_engine.error_body("Not found.", code="not_found"))

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/v1/systemone", "/typesafe/v1/systemone"}:
            self._json(404, decision_engine.error_body("Not found.", code="not_found"))
            return
        admitted = self.admission.acquire(blocking=False)
        if not admitted:
            self._json(
                529,
                decision_engine.error_body("Decision engine queue is full.", code="overloaded", kind="server_error"),
                headers={"Retry-After": "1"},
            )
            return
        try:
            length = int(self.headers.get("content-length") or "0")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise decision_engine.DecisionInputError("Request body must be valid JSON.", code="invalid_json")
            req = decision_engine.parse_request(body)
            started = time.perf_counter()
            deadline = _request_deadline(self.headers.get("x-decision-deadline-ms"))
            if not _acquire_until(self.request_lock, deadline, self._client_disconnected):
                raise DecisionDeadlineExceeded("decision deadline exceeded")
            try:
                out = decision_engine.build_systemone_response(
                    req,
                    self.scorer,
                    model=self.model_id,
                    engine=self.engine,
                    placement=self.placement,
                    residency=self.residency,
                    start_time=started,
                    deadline=deadline,
                    cancel_check=self._client_disconnected,
                )
            finally:
                self.request_lock.release()
            self._json(200, out)
        except decision_engine.DecisionInputError as exc:
            self._json(422, decision_engine.error_body(str(exc), code=exc.code, param=exc.param))
        except DecisionDeadlineExceeded as exc:
            self._json(504, decision_engine.error_body(str(exc), code="deadline_exceeded", kind="server_error"))
        except DecisionCancelled:
            return
        except decision_engine.DecisionReadoutError as exc:
            message = str(exc)
            if "deadline" in message:
                self._json(504, decision_engine.error_body(message, code="deadline_exceeded", kind="server_error"))
            elif "cancel" in message or "disconnect" in message:
                return
            else:
                self._json(502, decision_engine.error_body(message, code="decision_engine_error", kind="server_error"))
        except Exception as exc:
            self._json(502, decision_engine.error_body(str(exc), code="decision_engine_error", kind="server_error"))
        finally:
            self.admission.release()

    def _client_disconnected(self) -> bool:
        return _socket_disconnected(self.connection)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def _json(self, status: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-vivid-engine", self.engine)
        self.send_header("x-decision-engine", self.engine)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            raise DecisionCancelled("client disconnected")


class DecisionDeadlineExceeded(TimeoutError):
    pass


class DecisionCancelled(RuntimeError):
    pass


def _request_deadline(header_value: str | None) -> float:
    if header_value is not None:
        try:
            parsed_ms = float(header_value)
        except ValueError:
            return time.monotonic()
        if not (parsed_ms > 0 and parsed_ms < float("inf")):
            return time.monotonic()
        return time.monotonic() + min(DEFAULT_REQUEST_TIMEOUT_S, parsed_ms / 1000.0)
    return time.monotonic() + DEFAULT_REQUEST_TIMEOUT_S


def _raise_if_stopped(deadline: float, disconnected: Any) -> None:
    if time.monotonic() >= deadline:
        raise DecisionDeadlineExceeded("decision deadline exceeded")
    if callable(disconnected) and disconnected():
        raise DecisionCancelled("client disconnected")


def _acquire_until(lock: threading.Lock, deadline: float, disconnected: Any) -> bool:
    if lock.acquire(blocking=False):
        return True
    while True:
        _raise_if_stopped(deadline, disconnected)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DecisionDeadlineExceeded("decision deadline exceeded")
        if lock.acquire(timeout=min(0.05, remaining)):
            return True


def _socket_disconnected(sock: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([sock], [], [], 0.01)
        if not readable:
            return False
        data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return data == b""
    except BlockingIOError:
        return False
    except (OSError, ValueError):
        return True


def main() -> int:
    args = parse_args()
    server: DecisionThreadingHTTPServer | None = None
    current_process: dict[str, subprocess.Popen[Any] | None] = {"process": None}
    stopping = {"value": False}

    def stop(_signum: int, _frame: Any) -> None:
        stopping["value"] = True
        proc = current_process.get("process")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
        else:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    llama = start_llama(args, current_process=current_process)
    try:
        scorer = LlamaServerClient(base_url=f"http://127.0.0.1:{args.llama_port}", gguf=args.model, ctx_tokens=args.ctx)
        Handler.scorer = scorer
        Handler.model_id = args.model_id
        Handler.engine = llama["engine"]
        Handler.placement = llama["placement"]
        Handler.residency = args.residency
        Handler.llama_process = llama["process"]
        server = DecisionThreadingHTTPServer(("127.0.0.1", args.port), Handler)

        def monitor_child() -> None:
            llama["process"].wait()
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        threading.Thread(target=monitor_child, daemon=True).start()
        print(json.dumps({"event": "decision-engine-ready", "port": args.port, "engine": llama["engine"]}), flush=True)
        server.serve_forever()
        if llama["process"].poll() is not None and not stopping["value"]:
            raise SystemExit(1)
    finally:
        if llama["process"].poll() is None:
            llama["process"].terminate()
            try:
                llama["process"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                llama["process"].kill()
    return 0


def start_llama(args: argparse.Namespace, *, current_process: dict[str, subprocess.Popen[Any] | None] | None = None) -> dict[str, Any]:
    model_id = getattr(args, "model_id", decision_engine.DEFAULT_LOCAL_MODEL_ID)
    attempts = _placement_attempts(args.placement)
    last_error = ""
    for engine, placement, cuda_visible, ngl in attempts:
        if not _port_is_available("127.0.0.1", args.llama_port):
            raise RuntimeError(f"llama child port {args.llama_port} is occupied")
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=cuda_visible)
        cmd = [
            args.llama_server,
            "-m",
            args.model,
            "-c",
            str(args.ctx),
            "-np",
            "1",
            "-t",
            str(args.threads),
            "-ngl",
            ngl,
            "--host",
            "127.0.0.1",
            "--port",
            str(args.llama_port),
            "--metrics",
            "--no-webui",
            "--alias",
            model_id,
            "-cms",
            "0",
        ]
        proc = subprocess.Popen(cmd, env=env, stdout=sys.stderr, stderr=sys.stderr)
        if current_process is not None:
            current_process["process"] = proc
        healthy = wait_health(args.llama_port, args.startup_timeout_s, expected_model=model_id, process=proc)
        if healthy:
            if proc.poll() is None:
                return {"process": proc, "engine": engine, "placement": placement}
            last_error = f"{engine} llama-server child exited during startup"
        else:
            last_error = f"{engine} llama-server did not become healthy"
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            if current_process is not None and current_process.get("process") is proc:
                current_process["process"] = None
    raise RuntimeError(last_error)


def _placement_attempts(placement: str) -> tuple[tuple[str, str, str, str], ...]:
    if placement == "gpu":
        placement = "cuda:0"
    if placement == "cpu":
        return (("cpu", "cpu", "", "0"),)
    if placement.startswith("cuda:"):
        device = placement.split(":", 1)[1]
        if not device.isdigit():
            raise RuntimeError(f"invalid DECIDER_PLACEMENT: {placement}")
        return (("gpu", placement, device, "99"), ("cpu", "cpu", "", "0"))
    raise RuntimeError(f"invalid DECIDER_PLACEMENT: {placement}")


def wait_health(
    port: int,
    timeout_s: float,
    *,
    expected_model: str | None = None,
    process: subprocess.Popen[Any] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                body = json.loads(resp.read())
                if (
                    resp.status == 200
                    and body.get("status") == "ok"
                    and _matches_expected_model(port, body, expected_model)
                    and _owned_process_listens(process, port)
                ):
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _owned_process_listens(process: subprocess.Popen[Any] | None, port: int) -> bool:
    if process is None:
        return True
    pid = getattr(process, "pid", None)
    if pid is None:
        return True
    return _pid_owns_listener(int(pid), port)


def _pid_owns_listener(pid: int, port: int) -> bool:
    inodes = _listener_inodes(port)
    if not inodes:
        return False
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target.removeprefix("socket:[").removesuffix("]") in inodes:
                return True
    except OSError:
        return False
    return False


def _listener_inodes(port: int) -> set[str]:
    want = f":{port:04X}"
    out: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[1].endswith(want) and parts[3] == "0A":
                out.add(parts[9])
    return out


def _matches_expected_model(port: int, health_body: Mapping[str, Any], expected_model: str | None) -> bool:
    if expected_model is None:
        return True
    if "model" in health_body:
        return health_body.get("model") == expected_model
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as resp:
            body = json.loads(resp.read())
        ids = {item.get("id") for item in body.get("data", []) if isinstance(item, Mapping)}
        names = {item.get("name") for item in body.get("models", []) if isinstance(item, Mapping)}
        return expected_model in ids or expected_model in names
    except Exception:
        return False


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llama-server", default=os.environ.get("LLAMA_SERVER", "llama-server"))
    parser.add_argument("--model", default=os.environ.get("DECIDER_MODEL", ""))
    parser.add_argument("--model-id", default=os.environ.get("DECIDER_MODEL_ID", "decider-2b-v11"))
    parser.add_argument("--placement", default=os.environ.get("DECIDER_PLACEMENT", os.environ.get("DECIDER_ENGINE", "cpu")))
    parser.add_argument("--residency", choices=("resident", "on_demand"), default=os.environ.get("DECIDER_RESIDENCY", "on_demand"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DECIDER_PORT", "8023")))
    parser.add_argument("--llama-port", type=int, default=int(os.environ.get("DECIDER_LLAMA_PORT", "8024")))
    parser.add_argument("--threads", type=int, default=int(os.environ.get("DECIDER_THREADS", "8")))
    parser.add_argument("--ctx", type=int, default=int(os.environ.get("DECIDER_CTX", "4096")))
    parser.add_argument("--startup-timeout-s", type=float, default=float(os.environ.get("DECIDER_STARTUP_TIMEOUT_S", "45")))
    args = parser.parse_args()
    if not args.model:
        parser.error("--model or DECIDER_MODEL is required")
    if not Path(args.model).exists():
        parser.error(f"model path does not exist: {args.model}")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
