"""Idle checks the recorder runs before every live request (RP2 owner conditions).

Idle means no ACTIVE inference: every GPU at or under 5% utilization in three
samples about 10 s apart, and the media engine's own queue empty. Resident,
loaded engines do not count as busy. Each check returns None when idle, or a
short reason; anything unknown counts as busy. Nothing here interrupts work.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

import httpx

GPU_UTILIZATION = ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
MAX_UTILIZATION = 5
SAMPLES = 3
SAMPLE_INTERVAL_S = 10


def gpu_idle(run: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep,
             samples: int = SAMPLES, interval: float = SAMPLE_INTERVAL_S) -> str | None:
    for index in range(samples):
        if index:
            sleep(interval)
        try:
            done = run(GPU_UTILIZATION, capture_output=True, text=True, timeout=15, check=False)
            values = [int(line.strip()) for line in done.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError, ValueError):
            return "GPU state unreadable"
        if done.returncode != 0 or not values:
            return "GPU state unreadable"
        if max(values) > MAX_UTILIZATION:
            return f"GPU utilization {max(values)}% in sample {index + 1}"
    return None


def queue_idle(queue_url: str) -> str | None:
    """A read-only GET of the media engine's queue; both lists must be empty."""
    try:
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            response = client.get(queue_url)
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return "media queue unreadable"
    if not isinstance(payload, dict) or not all(isinstance(payload.get(k), list) for k in ("queue_running", "queue_pending")):
        return "media queue unreadable"
    if payload["queue_running"] or payload["queue_pending"]:
        return "media queue is not empty"
    return None


def both(*checks: Callable[[], str | None]) -> Callable[[], str | None]:
    def check() -> str | None:
        for one in checks:
            reason = one()
            if reason:
                return reason
        return None
    return check


def wait_until_idle(check: Callable[[], str | None], *, timeout_s: float = 1800, poll_s: float = 30,
                    sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> str | None:
    """Re-check until idle. Returns None when idle, else the last reason once the wait ran out."""
    deadline = now() + timeout_s
    while True:
        reason = check()
        if reason is None or now() + poll_s > deadline:
            return reason
        sleep(poll_s)
