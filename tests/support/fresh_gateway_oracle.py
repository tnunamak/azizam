"""Fresh-interpreter oracle: a gateway with an empty registry contacts no engine (S8 acceptance).

Run as ``python tests/support/fresh_gateway_oracle.py <scratch-dir> [--inject KIND]``. The DNS and outbound-socket
guards are installed before anything from the application is imported, so an import-time connection attempt is seen
even when the code under test swallows the error. The last stdout line is a JSON report.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ATTEMPTS: list[dict] = []


def _install_guards() -> None:
    def forbid(name, target=None):
        def guard(*args, **kwargs):
            where = ""
            if name in {"connect", "connect_ex", "sendto"}:
                where = repr(args[-1] if args else "")
            elif name in {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"}:
                where = repr(args[:2])
            elif name == "sendmsg":
                addr = args[3] if len(args) > 3 else kwargs.get("address")
                where = repr(addr)
            ATTEMPTS.append({"call": name, "to": where})
            raise OSError(f"network use is forbidden here: {name}")
        return guard

    for method in ("connect", "connect_ex", "sendto", "sendmsg"):
        setattr(socket.socket, method, forbid(method))
    for function in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"):
        setattr(socket, function, forbid(function))


def _inject(kind: str) -> None:
    """Control: an import-time attempt whose error is swallowed. The guard must still have recorded it."""
    attempt = {
        "connect": lambda: socket.socket().connect(("203.0.113.1", 80)),
        "dns": lambda: socket.getaddrinfo("engine.invalid", 80),
        "gethostbyname": lambda: socket.gethostbyname("engine.invalid"),
        "connect_ex": lambda: socket.socket().connect_ex(("203.0.113.1", 80)),
        "gethostbyname_ex": lambda: socket.gethostbyname_ex("engine.invalid"),
        "sendmsg": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, ("203.0.113.1", 53)),
        "udp": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("203.0.113.1", 53)),
    }[kind]
    try:
        attempt()
    except OSError:
        pass



async def _path_case(scratch: Path, path: str, state: str) -> dict:
    """Scoped STT/status contracts, with real HTTP messages to strict in-process fakes.

    This is not the public transcription route. Only media utilities, the final
    history writer, and the HTTP transport are replaced; no socket is permitted.
    """
    before = set(asyncio.all_tasks())
    import re
    from dataclasses import replace
    from unittest.mock import patch

    import httpx
    from gateway import metrics_writer, stt_chunking as stt
    import status_endpoint as status
    from tests.support.fake_upstream.app import FakeUpstream
    from tests.support.fake_upstream.protocols import PROTOCOLS, Protocol
    from tests.support.fake_upstream.scenario import scenario_from_data

    base = "http://127.0.0.1:18991"
    is_stt = path in {"stt-direct", "stt-chunked"}
    endpoint = "/v1/audio/transcriptions" if is_stt else "/health"
    count = {"stt-direct": 1, "stt-chunked": 3, "grid": 1, "history": 3}[path]
    exchanges = []
    if state == "enabled":
        exchanges = [{
            "id": "engine", "match": {"method": "POST" if is_stt else "GET", "path": endpoint},
            "times": count, "response": {"json": {"text": "heard"} if is_stt else {"status": "ready"}},
        }]
    protocol = "fresh-egress-paths"
    PROTOCOLS[protocol] = Protocol(protocol, ("/health", "/v1/audio/transcriptions"), "none")
    scenario = scenario_from_data({
        "protocol": protocol, "name": f"{path}-{state}",
        "provenance": {"source": "hand-written", "reference": "S8a scoped no-engine-egress paths"},
        "exchanges": exchanges,  # Never times=0: unexpected calls must latch violations.
    })

    class Clock:
        def __init__(self):
            self.value = 1000.0
            self.waiting = asyncio.Queue()
            self.resume = asyncio.Queue()

        def now(self):
            return self.value

        async def sleep(self, seconds):
            await self.waiting.put((self.value, seconds))
            await self.resume.get()
            self.value += seconds

    clock = Clock()
    fake = FakeUpstream(scenario, clock, scenario.name, base_url=base)
    dispatches, wrong_origins = [], []

    class OnlyFake(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=fake)

        async def handle_async_request(self, request):
            target = str(request.url)
            dispatches.append(target)
            if (request.url.scheme, request.url.host, request.url.port) != ("http", "127.0.0.1", 18991):
                wrong_origins.append(target)
                raise OSError("oracle refused a non-fixture HTTP destination")
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    def client(**kwargs):
        # Keep the real HTTPX request/response machinery; replace the transport only.
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=OnlyFake(), trust_env=False, **kwargs)

    record = {"host": "127.0.0.1", "port": 18991, "enabled": state != "disabled",
              "health_endpoint": "/health"}
    if state == "partial":
        record.pop("port")  # No alternative address is supplied.
    config = {"backends": {} if state == "absent" else {"stt": record}}
    report = {"path": path, "state": state, "rows": [], "history": [], "writer_calls": 0,
              "mode": None, "refused_at": None, "ticks": [], "sampler_joined": True}
    async def plain_http():
        return False

    class Writer:
        def record_samples(self, samples):
            report["writer_calls"] += 1
            report["history"].extend(samples)
            return True

        def flush(self):
            pass

    async def write_samples(rows):
        status._record_backend_health_samples(rows)

    try:
        if is_stt:
            raw = dict(config["backends"].get("stt") or {})
            raw["chunking"] = {
                # Configuration-only path: never created. Media fixtures are relocated
                # below after validation, without changing the captured address.
                "spool_dir": "/s8-oracle-configuration-only",
                "enabled": path == "stt-chunked", "threshold_seconds": 2,
                "target_seconds": 1, "search_seconds": 0, "max_segment_seconds": 1,
                "setup_failure_policy": "error",
            }
            stage = "config"
            try:
                execution = stt.SttExecutionConfig.from_service(
                    service_id="stt", service_config=raw, config_directory=scratch)
                stage = "sender"
                # The direct route is out of scope. Its scoped caller contract is:
                # an empty execution address refuses before constructing a request.
                if not execution.host:
                    raise stt.SttNotConfigured("No STT execution address")
                execution = replace(execution, spool_dir=scratch.resolve())
                workspace = scratch / "request"
                workspace.mkdir(parents=True, exist_ok=True)
                source = workspace / "source.wav"
                source.write_bytes(b"fixture audio")

                async def post_file(audio, filename):
                    async with client() as http:
                        response = await http.post(
                            execution.host.rstrip("/") + "/v1/audio/transcriptions",
                            files={"file": (filename, audio.read_bytes(), "audio/wav")},
                            data={"model": "fixture-stt"})
                    return stt.UpstreamResponse(response.status_code, response.content, response.headers)

                if path == "stt-direct":
                    response = await post_file(source, source.name)
                    report.update(mode="direct", response_status=response.status_code,
                                  text=json.loads(response.body).get("text"))
                else:
                    async def probe(*args, **kwargs):
                        return stt.ProbedAudio(3.0, 16000, 1)

                    async def decode(_source, destination, **kwargs):
                        destination.write_bytes(b"\0\0" * 48000)

                    async def extract(_source, destination, **kwargs):
                        destination.write_bytes(b"fixture segment")

                    # Exercise real segment planning, callback dispatch, merge and cleanup.
                    # These seams replace ffprobe/ffmpeg only, not address or enabled logic.
                    with patch.object(stt, "probe_audio", probe), \
                         patch.object(stt, "decode_planning_pcm", decode), \
                         patch.object(stt, "extract_segment", extract):
                        outcome = await stt.execute_chunking(
                            source_path=source, source_stem="source", fields=[("model", "fixture-stt")],
                            config=execution, post_file=post_file, cancellation_waiters=[],
                            deadline_at=asyncio.get_running_loop().time() + 5, on_cancel=None)
                    report.update(mode=outcome.mode, chunk_attempts=outcome.attempts_started,
                                  text=(outcome.merged_payload or {}).get("text"))
            except stt.SttNotConfigured as error:
                report.update(refused_at=stage, refusal=str(error))
        else:
            with patch.object(status.engine_egress, "async_client", client), \
                 patch.object(status, "_shared_probe_ssl_context", plain_http), \
                 patch.object(status, "_now", clock.now), \
                 patch.object(status, "_sleep", clock.sleep), \
                 patch.object(status, "_write_backend_samples", write_samples), \
                 patch.object(metrics_writer, "get_writer", lambda: Writer()):
                report["rows"] = await status._backend_grid(config)
                if path == "history":
                    task = None
                    try:
                        status.start_backend_samples(lambda: config)
                        task = status._SAMPLE_TASK
                        report["ticks"].append(await clock.waiting.get())
                        await clock.resume.put(None)
                        report["ticks"].append(await clock.waiting.get())
                    finally:
                        await status.stop_backend_samples()
                        report["sampler_joined"] = (
                            task is not None and task.done() and task.cancelled()
                            and status._SAMPLE_TASK is None and status._SAMPLE_WRITE is None)
    finally:
        # Cancel and join any leaked task before emitting a report; still fail the test
        # for the leak instead of hiding it by successful cleanup.
        pending = [task for task in asyncio.all_tasks() - before if not task.done()]
        report["leaked_tasks"] = len(pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    report.update(
        requests=[{"method": r.method, "path": r.path,
                   "filenames": re.findall(r'filename="([^"]+)"', r.body.decode("latin-1"))}
                  for r in fake.requests],
        violations=list(fake.violations), problems=fake.problems(),
        dispatches=dispatches, wrong_origins=wrong_origins)
    return report


def _path_main(scratch: Path, path: str, state: str, preloaded: list[str]) -> int:
    # main installed the guards before importing this branch's application modules.
    report = asyncio.run(asyncio.wait_for(_path_case(scratch, path, state), timeout=10))
    report.update(attempts=ATTEMPTS, preloaded_at_guard=preloaded)
    print(json.dumps(report))
    return 0


def main(argv: list[str]) -> int:
    scratch = Path(argv[1])
    inject = argv[argv.index("--inject") + 1] if "--inject" in argv else None
    preloaded = sorted(m for m in sys.modules if m.split(".")[0] in {"proxy", "gateway", "status_endpoint", "tests"})
    _install_guards()
    if inject:
        _inject(inject)
    sys.path.insert(0, str(ROOT))
    if "--path" in argv:
        return _path_main(scratch, argv[argv.index("--path") + 1], argv[argv.index("--state") + 1], preloaded)
    import proxy  # noqa: PLC0415 - must follow the guards
    from gateway import service_registry  # noqa: PLC0415
    from tests.support.adapter_differential_rig import GatewayRig  # noqa: PLC0415

    requests = [
        ("POST", "/v1/audio/speech", {"model": "tts-1", "input": "hi", "voice": "alloy"}),
        ("POST", "/v1/audio/generate", {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}),
        ("POST", "/v1/images/generations", {"prompt": "a cat", "model": "image-fast"}),
        ("POST", "/v1/embeddings", {"model": "nomic-embed-text-v1.5", "input": "hi"}),
    ]
    hosts = ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST")
    flags = ("TTS_ENABLED", "STT_ENABLED", "EMBEDDINGS_ENABLED", "COMFY_ENABLED")
    empty = proxy._derive_backend_constants({"backends": {}})
    report: dict = {"derived": {k: empty[k] for k in (*hosts, *flags)}, "statuses": [], "sent": []}
    with GatewayRig("fresh-no-egress", scratch) as rig:
        for key in (*hosts, *flags):
            rig.monkeypatch.setitem(proxy.runtime().derived, key, empty[key])
        for method, path, body in requests:
            response = asyncio.run(rig.call(method, path, label=f"fresh {path}", json_body=body))
            report["statuses"].append([path, response.status_code])
        report["sent"] = [call["to"] for call in rig.recorder.engine]
    report["released"] = service_registry.origin_of(
        service_registry.release_registry()["services"]["litellm"]["config"]["default_base_url"])
    report["attempts"] = ATTEMPTS
    report["preloaded_at_guard"] = preloaded
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
