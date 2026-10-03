#!/usr/bin/env python3
"""Serve TypeSafe ``/v1/systemone`` over a stock llama-server.

The wrapper keeps decider prompt/readout knowledge outside gateway core. It can
run the default 2B model on GPU0 and fall back to CPU when startup health never
arrives. The scoring loop follows the measured bench path: build the model's
single-token answer prompts, call llama-server ``/completion`` with
``n_probs``, then calibrate option probabilities locally.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import decision_engine


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
        self.letter_ids = [int(x) for x in np.asarray(letter_ids(self.tokenizer))]
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0

    def reset_usage(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def usage(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    def score_options(self, ctx: decision_engine.ScoreContext) -> Mapping[int, float]:
        with self._lock:
            item = self._build(
                PromptExample(_render_state(ctx.state), [PromptQuestion(ctx.question, list(ctx.options), 0)]),
                self.tokenizer,
                NoShuffle(),
                max_options=self._max_options,
                max_ctx_tokens=self.ctx_tokens,
            )
            slot = item["slots"][0]
            option_count = item["nopts"][0]
            prompt_ids = [int(token_id) for token_id in item["ids"][: slot + 1]]
            if len(prompt_ids) > self.ctx_tokens:
                raise decision_engine.DecisionInputError(
                    f"decision prompt has {len(prompt_ids)} tokens; runtime context is {self.ctx_tokens}",
                    code="context_length_exceeded",
                    param="state",
                )
            payload = {
                "prompt": prompt_ids,
                "n_predict": 1,
                "n_probs": max(100, min(255, option_count)),
                "logit_bias": [[self.letter_ids[index], 100.0] for index in range(option_count)],
                "cache_prompt": True,
                "temperature": 0,
                "post_sampling_probs": False,
            }
            raw = self._post("/completion", payload)
            self.input_tokens += len(prompt_ids)
            self.output_tokens += 1
        top_logprobs = raw["completion_probabilities"][0]["top_logprobs"]
        by_token = {int(entry["id"]): float(entry["logprob"]) for entry in top_logprobs}
        temperature = self._decider_temperature.for_types(self.T, self.T_by_type, [ctx.kind])[0]
        out: dict[int, float] = {}
        for index, option in enumerate(ctx.options[:option_count]):
            token_id = self.letter_ids[index]
            if token_id in by_token:
                out[index] = by_token[token_id] / float(temperature)
        return out

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())


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


class Handler(BaseHTTPRequestHandler):
    scorer: decision_engine.OptionScorer
    model_id: str
    engine: str
    placement: str
    residency: str
    request_lock = threading.Lock()
    admission = threading.BoundedSemaphore(5)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
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
            body = json.loads(self.rfile.read(length) or b"{}")
            req = decision_engine.parse_request(body)
            started = time.perf_counter()
            with self.request_lock:
                out = decision_engine.build_systemone_response(
                    req,
                    self.scorer,
                    model=self.model_id,
                    engine=self.engine,
                    placement=self.placement,
                    residency=self.residency,
                    start_time=started,
                )
            self._json(200, out)
        except decision_engine.DecisionInputError as exc:
            self._json(422, decision_engine.error_body(str(exc), code=exc.code, param=exc.param))
        except Exception as exc:
            self._json(502, decision_engine.error_body(str(exc), code="decision_engine_error", kind="server_error"))
        finally:
            self.admission.release()

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
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    args = parse_args()
    server: ThreadingHTTPServer | None = None
    current_process: dict[str, subprocess.Popen[Any] | None] = {"process": None}

    def stop(_signum: int, _frame: Any) -> None:
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
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(json.dumps({"event": "decision-engine-ready", "port": args.port, "engine": llama["engine"]}), flush=True)
        server.serve_forever()
    finally:
        llama["process"].terminate()
        try:
            llama["process"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            llama["process"].kill()
    return 0


def start_llama(args: argparse.Namespace, *, current_process: dict[str, subprocess.Popen[Any] | None] | None = None) -> dict[str, Any]:
    attempts = _placement_attempts(args.placement)
    last_error = ""
    for engine, placement, cuda_visible, ngl in attempts:
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
        ]
        proc = subprocess.Popen(cmd, env=env, stdout=sys.stderr, stderr=sys.stderr)
        if current_process is not None:
            current_process["process"] = proc
        if wait_health(args.llama_port, args.startup_timeout_s):
            return {"process": proc, "engine": engine, "placement": placement}
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


def wait_health(port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                body = json.loads(resp.read())
                if resp.status == 200 and body.get("status") == "ok":
                    return True
        except Exception:
            time.sleep(0.5)
    return False


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
