"""Event-loop lag while each spawn site and the status snapshot build run, at production-like resident memory.

    python scripts/perf/spawn_lag.py --repo . --out lag.json [--runs 8] [--ballast-mb 600] [--admin-seed PATH]

Run it under the interpreter that has the gateway's dependencies (the hermetic venv). It runs under uvloop, as
production does, and holds a ballast of touched memory so resident size is production-like: a fork copies the
page tables of all of it, so the loop stall of a spawn on the loop thread grows with it.

Each site is the real gateway function with a stand-in binary on PATH (a shell script), so the spawn is real and
no host service is touched. A probe task sleeps 2 ms in a loop and records how late it wakes. The largest
lateness during one call is that call's loop lag. Output is a JSON file, because importing the gateway prints to
stdout. --admin-seed copies a seeded admin.db (and its receipts.db) into scratch, so the snapshot build reads a
real schema.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import uvloop


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def stand_in(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


async def measure(call, runs: int) -> dict:
    loop = asyncio.get_running_loop()
    maxes, medians, durations = [], [], []
    for _ in range(runs):
        lags: list[float] = []
        stop = asyncio.Event()

        async def probe() -> None:
            while not stop.is_set():
                started = loop.time()
                await asyncio.sleep(0.002)
                lags.append((loop.time() - started - 0.002) * 1000)

        task = asyncio.ensure_future(probe())
        await asyncio.sleep(0.05)
        lags.clear()
        begin = time.perf_counter()
        await call()
        durations.append((time.perf_counter() - begin) * 1000)
        await asyncio.sleep(0.05)
        stop.set()
        await task
        maxes.append(max(lags) if lags else 0.0)
        medians.append(statistics.median(lags) if lags else 0.0)
    return {
        "loop_lag_max_ms_median": round(statistics.median(maxes), 2),
        "loop_lag_max_ms_worst": round(max(maxes), 2),
        "loop_lag_p50_ms_median": round(statistics.median(medians), 3),
        "call_ms_median": round(statistics.median(durations), 2),
    }


def build_sites(names: list[str], work: Path, repo: Path) -> dict:
    sites: dict = {}
    if "systemctl" in names:
        from gateway import lifecycle_drivers

        async def systemctl() -> None:
            assert await lifecycle_drivers.SystemdDriver()._run_systemctl(["systemctl", "--user", "restart", "bench.service"])

        sites["systemctl"] = systemctl
    if "speech" in names:
        from gateway import speech_stream

        async def speech() -> None:
            async def chunks():
                yield b"\x00\x01" * 2400
            async for _ in speech_stream.transcode_pcm_stream(chunks(), src_rate=24000, response_format="mp3"):
                pass

        sites["speech"] = speech
    if "stt_media" in names:
        from gateway import stt_chunking

        async def stt_media() -> None:
            await stt_chunking.run_media_command(
                ["/bin/true"], timeout_seconds=10, cancellation_waiters=(),
                deadline_at=time.monotonic() + 10, on_cancel=None)

        sites["stt_media"] = stt_media
    if "stt_measure" in names:
        from gateway import stt_measurement

        interpreter = stand_in(work, "stand-in-python", 'mkdir -p "$5"; printf "[]" > "$5/reference.json"')
        measurements = stt_measurement.Measurements(SimpleNamespace(path=work / "store.db"), None)
        measurements._save = lambda job: None
        counter = {"n": 0}

        async def stt_measure() -> None:
            counter["n"] += 1
            real = sys.executable
            sys.executable = str(interpreter)
            try:
                await measurements._prepare({"id": "job", "service_id": "svc"}, SimpleNamespace(id="sample"),
                                            work / f"ws-{counter['n']}", {"max_download_bytes": 1000})
            finally:
                sys.executable = real

        sites["stt_measure"] = stt_measure
    return sites


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--ballast-mb", type=int, default=600)
    parser.add_argument("--scratch", type=Path, default=Path.home() / ".tmp", help="disk-backed scratch (not /tmp)")
    parser.add_argument("--admin-seed", type=Path, help="a seeded admin.db; its receipts.db is copied beside it")
    parser.add_argument("--sites", default="systemctl,speech,stt_media,stt_measure,drift,health,snapshot")
    args = parser.parse_args()

    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    args.scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="spawn-lag-", dir=args.scratch))
    os.environ["AZIZAM_ADMIN_DB"] = str(work / "admin.db")  # the import must not touch live data
    os.environ["AZIZAM_RECEIPTS_DB"] = str(work / "receipts.db")
    if args.admin_seed:
        shutil.copy(args.admin_seed, work / "admin.db")
        shutil.copy(args.admin_seed.with_name("receipts.db"), work / "receipts.db")
    os.environ["PATH"] = f"{work}:{os.environ['PATH']}"
    stand_in(work, "systemctl", "exit 0")
    stand_in(work, "ffmpeg", "cat")

    ballast = bytearray(args.ballast_mb << 20)
    for offset in range(0, len(ballast), 4096):
        ballast[offset] = 1  # touch every page: resident memory is what a fork copies
    rss_at_start = rss_mb()

    wanted = args.sites.split(",")
    sites = build_sites(wanted, work, repo)
    proxy = importlib.import_module("proxy") if any(name in wanted for name in ("drift", "health", "snapshot")) else None

    if "drift" in wanted:
        script = work / "drift.py"
        script.write_text("print('no drift')\n", encoding="utf-8")
        proxy._DRIFT_SCRIPT_PATH = script

        async def drift() -> None:
            assert (await proxy._run_workflow_drift_once())["status"] == "ok"

        sites["drift"] = drift
    if "health" in wanted:
        async def health() -> None:
            await proxy.health()

        sites["health"] = health
    if "snapshot" in wanted:
        async def snapshot() -> None:
            await proxy._build_status_snapshot()

        sites["snapshot"] = snapshot

    async def run() -> dict:
        out = {}
        for name in wanted:
            if name not in sites:
                continue
            await sites[name]()  # warm: the first call pays one-time imports
            out[name] = await measure(sites[name], args.runs)
        return out

    results = asyncio.run(run(), loop_factory=uvloop.new_event_loop)
    args.out.write_text(json.dumps({
        "repo": str(repo), "runs": args.runs, "ballast_mb": args.ballast_mb,
        "rss_mb_at_start": round(rss_at_start, 1), "rss_mb_at_end": round(rss_mb(), 1),
        "admin_seed": str(args.admin_seed) if args.admin_seed else None, "sites": results,
    }, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
