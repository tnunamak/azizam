#!/usr/bin/env python3
"""Measure offline STT preparation at exact revisions, with disposable noise MP3s.

Run with ./venv/bin/python scripts/benchmark-stt-workspace.py REVISION [...].
No HTTP requests are made. Peak logical workspace bytes include the compressed
input and are sampled every 10 ms. Fixtures are deleted on exit.
"""
import asyncio
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def measure(module, revision, source, workspace, seconds, rate, channels):
    execution = module.SttExecutionConfig.from_service(service_id="benchmark", config_directory=ROOT,
        service_config={"chunking": {"spool_dir": str(workspace.parent), "target_seconds": 480,
            "threshold_seconds": 600, "max_segment_seconds": 600, "search_seconds": 10,
            "max_segments": 64 if seconds == 14400 else 16,
            "deadline_seconds": 3600, "backend_timeout_seconds": 3600}})
    gate = module.SttResourceGate()
    lease = gate.acquire(request_id="benchmark", config=execution)
    if hasattr(lease, "workspace"):
        lease.workspace = workspace
    lease_options = {"resource_lease": lease} if "resource_lease" in inspect.signature(module.execute_chunking).parameters else {}
    peak = 0
    peak_reservation = gate._reserved_bytes
    finished = False
    async def monitor():
        nonlocal peak, peak_reservation
        while not finished:
            size = 0
            for path in workspace.iterdir():
                try:
                    size += path.stat().st_size
                except FileNotFoundError:
                    pass
            peak = max(peak, size)
            peak_reservation = max(peak_reservation, gate._reserved_bytes)
            await asyncio.sleep(0.01)
    posts = 0
    async def post(_path, _filename):
        nonlocal posts
        posts += 1
        return module.UpstreamResponse(200, b'{"text":"piece"}', {})
    task = asyncio.create_task(monitor())
    started = time.monotonic()
    try:
        outcome = await module.execute_chunking(source_path=source, source_stem="noise", fields=[],
            config=execution, post_file=post, cancellation_waiters=(),
            deadline_at=started + 3600, on_cancel=None, **lease_options)
        status = outcome.mode
    except module.SttExecutionError as error:
        status = type(error).__name__
    finally:
        elapsed = time.monotonic() - started
        finished = True
        await task
        lease.release()
    print(json.dumps(dict(revision=revision, hours=seconds/3600, rate=rate, channels=channels,
        seconds=round(elapsed, 3), peak_workspace_bytes=peak, input_bytes=source.stat().st_size,
        status=status, segments=posts, peak_reserved_bytes=peak_reservation)), flush=True)


def main():
    modules = []
    for index, revision in enumerate(sys.argv[1:] or ["HEAD"]):
        code = subprocess.check_output(["git", "show", f"{revision}:gateway/stt_chunking.py"], cwd=ROOT)
        module = types.ModuleType(f"stt_benchmark_{index}")
        module.__file__ = str(ROOT / "gateway/stt_chunking.py")
        sys.modules[module.__name__] = module
        exec(compile(code, module.__file__, "exec"), module.__dict__)
        modules.append((subprocess.check_output(["git", "rev-parse", revision], cwd=ROOT, text=True).strip(), module))
    scratch = Path.home() / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="d47-noise-", dir=scratch) as directory:
        root = Path(directory)
        for seconds, rate, channels in [(7200, 44100, 1), (7200, 48000, 2), (14400, 48000, 2)]:
            source = root / "noise.mp3"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"anoisesrc=r={rate}:d={seconds}:seed=47", "-ac", str(channels),
                "-c:a", "libmp3lame", "-q:a", "4", str(source)], check=True)
            for revision, module in modules:
                with tempfile.TemporaryDirectory(prefix="workspace-", dir=root) as workspace:
                    # The implementation stores working media beside the source.
                    input_path = Path(workspace) / "noise.mp3"
                    input_path.hardlink_to(source)
                    asyncio.run(measure(module, revision, input_path, Path(workspace), seconds, rate, channels))
            source.unlink()


if __name__ == "__main__":
    main()
