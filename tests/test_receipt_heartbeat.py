"""A receipt whose work outlives the stale limit stays in progress while the work goes on; an abandoned one goes stale."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from gateway import receipt_store
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt

MINUTE_MS = 60_000
NOW_MS = 1_800_000_000_000


def _receipt(request_id: str) -> RouteReceipt:
    return RouteReceipt(protocol="openai.mesh", intent="mesh.generate", path="/v1/meshes/generate", request_id=request_id)


def test_touch_keeps_an_in_progress_receipt_from_reading_as_stale(tmp_path: Path) -> None:
    db = tmp_path / "receipts.db"
    receipt_store.init_db(db)
    receipt_store.begin(_receipt("req-live"), now_ms=NOW_MS, path=db)
    receipt_store.begin(_receipt("req-dead"), now_ms=NOW_MS, path=db)
    later = NOW_MS + 8 * MINUTE_MS

    assert receipt_store.touch(_receipt("req-live"), now_ms=NOW_MS + 7 * MINUTE_MS, path=db) is True

    live = receipt_store.get("req-live", now_ms=later, path=db)
    dead = receipt_store.get("req-dead", now_ms=later, path=db)
    assert live is not None and live["status"] == "in_progress"
    assert dead is not None and dead["status"] == "stale_incomplete"


def test_touch_leaves_a_closed_or_unknown_receipt_alone(tmp_path: Path) -> None:
    db = tmp_path / "receipts.db"
    receipt_store.init_db(db)
    closing = _receipt("req-done")
    receipt_store.begin(closing, now_ms=NOW_MS, path=db)
    closing.finish("succeeded")
    receipt_store.finalize(closing, now_ms=NOW_MS + MINUTE_MS, path=db)

    assert receipt_store.touch(closing, now_ms=NOW_MS + 9 * MINUTE_MS, path=db) is False
    assert receipt_store.touch(_receipt("req-never"), now_ms=NOW_MS, path=db) is False
    done = receipt_store.get("req-done", now_ms=NOW_MS + 20 * MINUTE_MS, path=db)
    assert done is not None and done["status"] == "succeeded" and done["updated_at_ms"] == NOW_MS + MINUTE_MS


def test_a_handed_off_receipt_beats_until_it_is_settled_and_an_abandoned_one_goes_stale(tmp_path: Path) -> None:
    """The receipt began ten minutes ago. While the job's task holds it, the beat keeps it in progress; one nobody holds
    reads as stale. After settle the beat stops and the receipt is closed once."""
    db = tmp_path / "receipts.db"
    receipt_store.init_db(db)
    old = int(time.time() * 1000) - 10 * MINUTE_MS

    def owner(request_id: str) -> ReceiptLifecycleOwner:
        return ReceiptLifecycleOwner(
            _receipt(request_id), begin=lambda r: receipt_store.begin(r, now_ms=old, path=db),
            finalize=lambda r: receipt_store.finalize(r, path=db),
            touch=lambda r: receipt_store.touch(r, path=db), heartbeat_s=0.02, log=lambda m: None)

    async def scenario() -> tuple[str, str, str]:
        running, abandoned = owner("req-running"), owner("req-abandoned")
        async with running:
            running.handoff()
        await asyncio.sleep(0.2)  # several beats
        status_running = receipt_store.get("req-running", path=db)["status"]
        status_abandoned = receipt_store.get("req-abandoned", path=db)["status"]
        running.report(status="succeeded", reason="succeeded")
        await running.settle()
        await asyncio.sleep(0.1)  # a beat after settle would find the receipt closed
        return status_running, status_abandoned, receipt_store.get("req-running", path=db)["status"]

    assert asyncio.run(scenario()) == ("in_progress", "stale_incomplete", "succeeded")
