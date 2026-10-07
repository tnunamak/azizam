"""Run paired real-gateway/direct-engine measurements without TCP or live state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

from .fixtures import CHUNK_MS, DELAY_MS, MODELS, TOKENS, wav_bytes
from .grader import CorrectnessError, grade, require
from .stats import summarize, summarize_blocks

ROOT = Path(__file__).resolve().parents[1]
CONCURRENCY = [1, 8, 32]
PROTOCOL = "unix-paired-v5"


def prepare(state: Path):
    import yaml
    for folder in ("home", "images", "workflows", "catalog", "tmp", "object_info"):
        (state / folder).mkdir()
    workflow = {"_meta": {"name": "bench-image", "prompt_node": "1", "size_node": "2"},
                "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "fixture"}},
                "2": {"class_type": "EmptyLatentImage", "inputs": {"width": 32, "height": 32, "batch_size": 1}},
                "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "bench"}}}
    for model in ("bench-image", "bench-image-heldout"):
        (state / "workflows" / f"{model}.json").write_text(json.dumps(workflow))
    config = {
        "proxy": {"host": "127.0.0.1", "port": 18991}, "auth": {"enabled": True},
        "registry": {"catalog_dir": str(state / "catalog")},
        "backends": {
            **{name: {"enabled": False, "host": "127.0.0.1", "port": 18990}
               for name in ("tabby", "kobold", "embeddings")},
            "litellm": {"enabled": True, "host": "127.0.0.1", "port": 18990,
                        "base_url": "http://127.0.0.1:18990", "capabilities": ["chat"],
                        "model_policy_schema_version": 1,
                        "models": [{"id": m, "model_group": m, "upstream_model": m} for m in MODELS]},
            "tts": {"enabled": True, "host": "127.0.0.1", "port": 18990, "capabilities": ["audio", "speech"]},
            "stt": {"enabled": True, "host": "127.0.0.1", "port": 18990, "capabilities": ["audio", "transcriptions"],
                    "default_model": "bench-stt", "chunking": {"enabled": False, "spool_dir": str(state / "spool"), "global_concurrency": 64}},
            "comfyui": {"enabled": True, "host": "127.0.0.1", "port": 18990, "capabilities": ["images"],
                        "workflows_dir": str(state / "workflows"), "default_model": "bench-image"},
        },
        "model_aliases": {
            "speech": {"engines": {"bench-tts": {"service": "tts", "model": "bench-tts", "voice_transform": "",
                                                      "voice_map": {"alloy": "alloy", "nova": "nova"}}}, "stable": {"tts": "bench-tts"}},
            "transcriptions": {"engines": {"bench-stt": {"service": "stt", "model": "bench-stt"}}, "stable": {"stt": "bench-stt"}},
        },
        "images": {"cache_dir": str(state / "images"), "vram_estimates_file": str(state / "vram.json"),
                   "background_removal": {"enabled": False}},
        "voices": {"dir": str(state / "voices")},
        "model_routing": {"model_dir": str(state / "models"), "gguf_dir": str(state / "models")},
    }
    for backend in config["backends"].values():
        if backend.get("enabled"):
            backend["health_endpoint"] = "/health"
    (state / "config.yml").write_text(yaml.safe_dump(config))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(state / "home"),
           "PYTHONPATH": str(ROOT), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": str(state / "tmp"), "XDG_CACHE_HOME": str(state / "home" / ".cache"),
           "XDG_CONFIG_HOME": str(state / "home" / ".config"),
           "AZIZAM_CONFIG_PATH": str(state / "config.yml"), "AZIZAM_LITELLM_BASE_URL": "http://127.0.0.1:18990",
           "AZIZAM_KEY_ACTIVITY_LOG": str(state / "key_activity.jsonl"),
           "AZIZAM_HOST_TELEMETRY_DISABLED": "1", "AZIZAM_IMAGE_CACHE_DIR": str(state / "images"),
           "AZIZAM_ADMIN_SESSION_KEY": "benchmark-session-fixture-only",
           "AZIZAM_PROVIDER_LEDGER_DB": str(state / "provider_spend.db")}
    for name, filename in {"ADMIN": "admin", "METRICS": "metrics", "RESPONSES": "responses",
                           "RECEIPTS": "receipts", "MEDIA_JOBS": "media_jobs"}.items():
        env[f"AZIZAM_{name}_DB"] = str(state / f"{filename}.db")
    return env


def load_mix(name):
    path = ROOT / "bench" / "mixes" / f"{name}.json"
    raw = path.read_bytes()
    manifest = json.loads((path.parent / "manifest.json").read_text())
    require(hashlib.sha256(raw).hexdigest() == manifest[name], "mix changed: version the evaluation before measuring")
    cases = json.loads(raw)["cases"]
    require(len({c["id"] for c in cases}) == len(cases), "duplicate mix case IDs")
    required = {"chat", "chat_stream", "models", "responses", "responses_stream", "image", "tts", "stt", "status", "health"}
    require({c["id"] for c in cases} == required, "mix must cover every required request shape")
    return cases, hashlib.sha256(raw).hexdigest()


async def request_one(client, case, direct, serial):
    kind = case["kind"]
    body = dict(case.get("body", {}))
    stream = body.get("stream", False)
    if kind in {"chat", "responses"}:
        body["user"] = serial
    if direct and kind == "responses":
        body["messages"] = [{"role": "user", "content": body.pop("input")}]
        body.pop("store", None)
    path = case["path"]
    if direct:
        path = {"responses": "/v1/chat/completions", "image": "/__bench/image", "status": "/__bench/status"}.get(kind, path)
    kwargs = {"json": body} if case["method"] == "POST" else {}
    if kind == "stt":
        # Fixed multipart boundary is independent of HTTPX's random boundary.
        boundary = "bench-fixed-boundary"
        fields = [("model", body["model"]), ("response_format", "json")]
        content = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
        content += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                    + wav_bytes(case["audio_frames"]) + f'\r\n--{boundary}--\r\n'.encode())
        kwargs = {"content": content, "headers": {"content-type": f"multipart/form-data; boundary={boundary}"}}
    started = time.monotonic_ns()
    if stream:
        chunks = []
        async with client.stream(case["method"], path, **kwargs) as response:
            async for raw in response.aiter_raw():
                chunks.append((time.monotonic_ns(), raw))
            finished = time.monotonic_ns()
        return {"latency": (finished - started) / 1e6, "started": started, "finished": finished,
                "chunks": chunks, "response": response, "serial": serial}
    response = await client.request(case["method"], path, **kwargs)
    finished = time.monotonic_ns()
    return {"latency": (finished - started) / 1e6, "finished": finished, "response": response, "serial": serial}


async def batch(client, engine, case, direct, concurrency, serial):
    started = time.monotonic_ns()
    results = await asyncio.gather(*[request_one(client, case, direct, f"{serial}-{i}") for i in range(concurrency)])
    seconds = (max(r["finished"] for r in results) - started) / 1e9
    # Parsing and grading happen only after ALL network timing has finished.
    for result in results:
        response = result.pop("response")
        events, arrivals = [], []
        if "chunks" in result:
            chunks = result.pop("chunks")
            require(bool(chunks), "empty event stream")
            result["ttfb"] = (chunks[0][0] - result["started"]) / 1e6
            buffer = b""
            for now, raw in chunks:
                buffer += raw
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip().decode()
                    events.append(data)
                    if data != "[DONE]":
                        payload = json.loads(data)
                        token = (payload.get("choices") or [{}])[0].get("delta", {}).get("content")
                        if token is not None or payload.get("type") == "response.output_text.delta":
                            arrivals.append(now)
            require(len(arrivals) == len(TOKENS), f"{case['id']}: missing streaming arrivals")
            result["arrivals"] = arrivals
            result["ttft"] = (arrivals[0] - result["started"]) / 1e6
        grade(case, response.status_code, response.headers, b"" if events else response.content, events, direct)
        if case["kind"] == "responses" and not direct:
            payload = json.loads(events[-1])["response"] if events else response.json()
            result["response_id"] = payload["id"]
    if case.get("body", {}).get("stream"):
        reply = await engine.post("/__bench/emissions", json=[r["serial"] for r in results])
        require(reply.status_code == 200, "fixture lost stream emission timestamps")
        emissions = reply.json()
        for result in results:
            stamps = emissions[result["serial"]]
            require(len(stamps) == len(result["arrivals"]), "fixture emission count mismatch")
            result["chunk_relay"] = [(arrival - stamp) / 1e6 for arrival, stamp in zip(result["arrivals"], stamps)]
            require(all(v >= 0 for v in result["chunk_relay"]), "monotonic clocks disagree")
    return results, concurrency / seconds


async def wait_ready(client, child, state, kind):
    for _ in range(300):
        if child.poll() is not None:
            raise RuntimeError(f"{kind} failed to start: {(state / (kind + '.log')).read_text()[-3500:]}")
        try:
            response = await client.get("/health")
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError(f"{kind} startup timed out")


def add_metrics(metrics, mix, case, concurrency, name, unit, blocks, statistics=("p50", "p95", "p99"), reference_blocks=None):
    values = [v for block in blocks for v in block]
    require(bool(values), f"empty measurement: {name}")
    for statistic in statistics:
        metrics.append({"mix": mix, "case": case, "concurrency": concurrency, "metric": name, "unit": unit,
                        "statistic": statistic, "samples": values, "sample_blocks": blocks,
                        "summary": summarize_blocks(blocks, statistic, reference_blocks=reference_blocks)})
        if reference_blocks is not None:
            metrics[-1]["reference_blocks"] = reference_blocks
            metrics[-1]["reference_samples"] = [v for block in reference_blocks for v in block]


async def measure(args, state, children, report):
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(state / "engine.sock")), base_url="http://bench", timeout=30) as engine, \
            httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(state / "gateway.sock")), base_url="http://bench", timeout=60) as gateway:
        await wait_ready(engine, children[0], state, "engine")
        await wait_ready(gateway, children[1], state, "gateway")
        keys = json.loads((state / "auth.json").read_text())
        gateway.headers["authorization"] = f"Bearer {keys['app']}"
        # Negative controls are untimed and must pass before any score can be 1.
        denied = await gateway.post("/v1/chat/completions", json={"model": MODELS[0], "messages": [{"role": "user", "content": "auth control"}]},
                                    headers={"authorization": "Bearer invalid-benchmark-key"})
        require(denied.status_code == 401, "bad credentials were not rejected")
        denied = await gateway.get("/status/raw")
        require(denied.status_code in {401, 403}, "App credential gained admin status access")
        async def gateway_batch(case, concurrency, serial, probe):
            backend_before = (await engine.get("/__bench/counts")).json()
            reply = await gateway.post("/__bench/control", json={"action": "start", "path": case["path"], "probe": probe})
            require(reply.status_code == 200, "gateway control start failed")
            observations, throughput = await batch(gateway, engine, case, False, concurrency, serial)
            reply = await gateway.post("/__bench/control", json={"action": "stop"})
            require(reply.status_code == 200, "gateway control stop failed")
            control = reply.json()
            backend_after = (await engine.get("/__bench/counts")).json()
            backend_path = {"chat": "/v1/chat/completions", "responses": "/v1/chat/completions", "image": "/prompt",
                            "tts": "/v1/audio/speech", "stt": "/v1/audio/transcriptions"}.get(case["kind"])
            if backend_path:
                require(backend_after.get(backend_path, 0) - backend_before.get(backend_path, 0) == concurrency,
                        f"{case['id']}: skipped/duplicated backend execution")
            expected_metrics = 0 if case["kind"] == "health" else concurrency
            require(control["work_delta"]["metrics"] == expected_metrics,
                    f"{case['id']}: lost request metrics {control['work_delta']}")
            if case["kind"] in {"chat", "image", "tts", "stt"}:
                require(control["work_delta"]["receipts"] == concurrency, f"{case['id']}: lost successful receipts {control['work_delta']}")
            if case["kind"] == "responses":
                require(control["work_delta"]["responses"] == concurrency, "Responses store:true did not persist every response")
            if case["kind"] == "status":
                require(control["status_errors"] == {}, f"status build degraded: {control['status_errors']}")
                require(len(control["status_sections"]) >= 40 and {"litellm", "tts", "stt", "comfyui"} <= set(control["status_backends"]),
                        "status builder dropped required sections/backends")
            for result in observations:
                if "response_id" in result:
                    stored = await gateway.get("/v1/responses/" + result["response_id"])
                    require(stored.status_code == 200 and stored.json()["status"] == "completed" and
                            stored.json()["model"] == case["body"]["model"], "stored Responses retrieval failed")
            return observations, throughput, control

        for mix in report["metadata"]["mix_hashes"]:
            cases, _ = load_mix(mix)
            if args.only:
                cases = [c for c in cases if c["id"] in args.only.split(",")]
                require(bool(cases), "--only selected no cases")
            for case in cases:
                gateway.headers["authorization"] = f"Bearer {keys['admin' if case['kind'] == 'status' else 'app']}"
                # Fixed shape warm-up primes handler caches and persistence.
                # Do not duplicate the entire 32-client workload as warm-up.
                for warm in range(args.warmup):
                    for direct in (True, False):
                        await batch(engine if direct else gateway, engine, case, direct, 1,
                                    f"warm-{mix}-{case['id']}-{warm}-{direct}")
                for concurrency in report["metadata"]["concurrency"]:
                    overhead, gateway_latency, direct_latency, ttfb, relay = [], [], [], [], []
                    rps, ttft = [], []
                    stream_blocks = {direct: {field: [] for field in ("ttfb", "ttft", "chunk_relay", "all_chunk_relay")} for direct in (True, False)}
                    blocks = {name: [] for name in ("overhead", "gateway_latency", "direct_latency", "loop_lag", "cpu_per_request",
                                                    "ttfb_overhead", "ttft_overhead", "mean_chunk_relay_overhead", "throughput")}
                    for repetition in range(math.ceil(args.reps / concurrency)):
                        pair = {}
                        for direct in ((True, False) if repetition % 2 == 0 else (False, True)):
                            serial = f"{mix}-{case['id']}-{concurrency}-{repetition}-{direct}"
                            if direct:
                                observations, throughput = await batch(engine, engine, case, True, concurrency, serial)
                            else:
                                observations, throughput, control = await gateway_batch(case, concurrency, serial, True)
                                rps.append(throughput)
                                if control["loop_lag_ms"]:
                                    blocks["loop_lag"].append(control["loop_lag_ms"])
                                blocks["throughput"].append([throughput])
                            pair[direct] = observations
                        if "ttfb" in pair[False][0]:
                            for direct in (True, False):
                                stream_blocks[direct]["all_chunk_relay"].append([
                                    delay for request in pair[direct] for delay in request["chunk_relay"]])
                                for field in ("ttfb", "ttft", "chunk_relay"):
                                    stream_blocks[direct][field].append([
                                        sum(r[field]) / len(TOKENS) if field == "chunk_relay" else r[field]
                                        for r in pair[direct]])
                        for real, fake in zip(pair[False], pair[True]):
                            overhead.append(real["latency"] - fake["latency"])
                            gateway_latency.append(real["latency"])
                            direct_latency.append(fake["latency"])
                            if "ttfb" in real:
                                ttfb.append(real["ttfb"] - fake["ttfb"])
                                ttft.append(real["ttft"] - fake["ttft"])
                                relay.append(sum(real["chunk_relay"]) / len(TOKENS) - sum(fake["chunk_relay"]) / len(TOKENS))
                        for name, values in (("overhead", overhead), ("gateway_latency", gateway_latency), ("direct_latency", direct_latency),
                                             ("ttfb_overhead", ttfb), ("ttft_overhead", ttft), ("mean_chunk_relay_overhead", relay)):
                            if values:
                                blocks[name].append(values[-concurrency:])
                    # Separate unprobed CPU burst. Empty-window subtraction
                    # removes control/flush setup; raw values remain in JSON.
                    reply = await gateway.post("/__bench/control", json={"action": "start", "path": case["path"], "probe": False})
                    require(reply.status_code == 200, "CPU calibration start failed")
                    reply = await gateway.post("/__bench/control", json={"action": "stop"})
                    require(reply.status_code == 200, "CPU calibration stop failed")
                    floor = reply.json()["cpu_ms"]
                    _, _, control = await gateway_batch(case, concurrency, f"cpu-{mix}-{case['id']}-{concurrency}", False)
                    blocks["cpu_per_request"] = [[(control["cpu_ms"] - floor) / concurrency]]
                    blocks["cpu_control_floor"] = [[floor / concurrency]]
                    blocks["cpu_raw_per_request"] = [[control["cpu_ms"] / concurrency]]
                    identity = (report["metrics"], mix, case["id"], concurrency)
                    references = {"overhead": blocks["direct_latency"]}
                    blocks["overhead"] = blocks["gateway_latency"]
                    for metric, field in (("ttfb_overhead", "ttfb"), ("ttft_overhead", "ttft"),
                                          ("mean_chunk_relay_overhead", "chunk_relay"), ("chunk_relay_overhead", "all_chunk_relay")):
                        if stream_blocks[False][field]:
                            blocks[metric] = stream_blocks[False][field]
                            references[metric] = stream_blocks[True][field]
                    for name, samples in blocks.items():
                        if samples and name != "throughput":
                            add_metrics(*identity, name, "ms", samples, reference_blocks=references.get(name))
                    add_metrics(*identity, "throughput", "req/s", blocks["throughput"], ("mean",))
                    p50 = summarize_blocks(blocks["overhead"], reference_blocks=references["overhead"])["value"]
                    p95 = summarize_blocks(blocks["overhead"], "p95", reference_blocks=references["overhead"])["value"]
                    noise = summarize_blocks(blocks["overhead"], reference_blocks=references["overhead"])["noise_half_width"]
                    noise_label = f"{noise:9.3f}" if len(blocks["overhead"]) >= 2 else "      n/a"
                    p99 = summarize_blocks(blocks["overhead"], "p99", reference_blocks=references["overhead"])["value"]
                    print(f"{mix:5} {case['id']:17} {concurrency:2} {p50:10.3f} {p95:10.3f} {p99:10.3f} {noise_label} {sum(rps)/len(rps):9.1f}", flush=True)
        if args.profile:
            reply = await gateway.post("/__bench/control", json={"action": "profile_dump"})
            require(reply.status_code == 200 and reply.json().get("saved"), "gateway profile could not be saved")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mix", choices=["train", "test", "all"], default="train")
    parser.add_argument("--reps", type=int, default=32, help="request repetitions per case/concurrency, rounded up to a whole concurrent batch")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--json", type=Path, default=Path("bench-output.json"))
    parser.add_argument("--cpu", help="optional Linux CPU affinity, e.g. 2,3,4")
    parser.add_argument("--only", help="diagnostics only: comma-separated case IDs")
    parser.add_argument("--concurrency", default="1,8,32", help="diagnostics only: choose 1,8,32")
    parser.add_argument("--profile", type=Path, help="write gateway cProfile stats; never compare profiled numbers")
    parser.add_argument("--state-dir", type=Path, default=Path("/dev/shm"), help="private small database/media state parent; use tmpfs to control disk noise")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        parser.error("benchmark requires Python 3.12")
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    installed = {normalize(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()}
    locked = {normalize(name): version for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)",
              (ROOT / "tests" / "requirements-hermetic.lock").read_text(), re.MULTILINE)}
    if installed != locked:
        parser.error("installed distributions must exactly match tests/requirements-hermetic.lock")
    namespace = os.environ.get("BENCH_NAMESPACE") == "1"
    if namespace and (not os.environ.get("BENCH_HOST_NETNS") or os.readlink("/proc/self/ns/net") == os.environ["BENCH_HOST_NETNS"]):
        parser.error("namespace wrapper did not isolate networking")
    concurrency = [int(c) for c in args.concurrency.split(",")]
    if not concurrency or any(c not in CONCURRENCY for c in concurrency) or len(set(concurrency)) != len(concurrency):
        parser.error("concurrency must select distinct values from 1,8,32")
    if args.reps < 2 or args.warmup < 1:
        parser.error("reps must be >=2 and warmup >=1")
    affinity = [int(c) for c in args.cpu.split(",")] if args.cpu else None
    if affinity is not None:
        if not hasattr(os, "sched_setaffinity"):
            parser.error("CPU pinning needs Linux")
        if len(affinity) != 3 or len(set(affinity)) != 3:
            parser.error("--cpu requires three distinct cores: client, gateway, engine")
        os.sched_setaffinity(0, [affinity[0]])
    mixes = ["train", "test"] if args.mix == "all" else [args.mix]
    report = {"schema_version": 1, "correctness": {"passed": False, "score": 0}, "metrics": [],
              "metadata": {"protocol_version": PROTOCOL, "mix_hashes": {m: load_mix(m)[1] for m in mixes},
                           "concurrency": concurrency, "reps": args.reps, "warmup": args.warmup,
                           "backend_delay_ms": DELAY_MS, "chunk_interval_ms": CHUNK_MS, "cpu_affinity": affinity,
                           "python": platform.python_version(), "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
                           "profile": args.profile is not None}}
    state_base = args.state_dir.resolve()
    state_base.mkdir(parents=True, exist_ok=True)
    mount = max((line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()
                 if state_base.is_relative_to(line.split()[4])), key=lambda fields: len(fields[4]))
    report["provenance"] = {
        "harness_sha256": hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in
                           sorted(list((ROOT / "bench").glob("*.py")) + [ROOT / "scripts/ui/fake_engine.py"]))).hexdigest(),
        "lock_sha256": hashlib.sha256((ROOT / "tests" / "requirements-hermetic.lock").read_bytes()).hexdigest(),
        "kernel": platform.release(), "machine": platform.machine(),
        "network_namespace": namespace,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "state_filesystem": mount[mount.index("-") + 1],
        "state_dir": str(state_base),
        "load_average_before": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "code_revision": os.environ.get("BENCH_CODE_SHA", "unknown"),
        "source_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(list(ROOT.glob("*.py")) + list((ROOT / "gateway").rglob("*.py"))))).hexdigest(),
    }
    try:
        report["provenance"]["cpu_model"] = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name"))
    except (OSError, StopIteration):
        report["provenance"]["cpu_model"] = platform.processor()
    args.json = args.json.resolve()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    if args.profile:
        args.profile = args.profile.resolve()
    started = time.monotonic()
    children, logs = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="azizam-bench-", dir=state_base) as folder:
            state = Path(folder)
            if len(str(state / "gateway.sock").encode()) >= 104:
                raise RuntimeError("private state path too long for Unix sockets; use a shorter HOME")
            env = prepare(state)
            if args.profile:
                env["BENCH_PROFILE"] = "1"
            try:
                for kind in ("engine", "gateway"):
                    log = (state / f"{kind}.log").open("w")
                    logs.append(log)
                    child_env = dict(env)
                    if namespace:
                        child_env["BENCH_OS_BOUNDARY"] = "1"
                    if affinity:
                        child_env["BENCH_CPU"] = str(affinity[2 if kind == "engine" else 1])
                    children.append(subprocess.Popen([sys.executable, "-m", "bench.server", kind, str(state)], cwd=ROOT,
                                                     env=child_env, stdout=log, stderr=log))
                print("mix   case               c  overhead p50   p95 (ms)    p99 (ms)  CIhalf50      req/s", flush=True)
                asyncio.run(measure(args, state, children, report))
                report["correctness"] = {"passed": True, "score": 1}
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                for log in logs:
                    log.close()
            if args.profile:
                args.profile.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(state / "gateway.pstats", args.profile)
    except Exception as exc:
        report["correctness"] = {"passed": False, "score": 0, "error": str(exc)}
        print(f"correctness/infrastructure failure: {exc}", file=sys.stderr)
    report["elapsed_s"] = time.monotonic() - started
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(f"score={report['correctness']['score']} elapsed={report['elapsed_s']:.1f}s report={args.json}", flush=True)
    return 0 if report["correctness"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
