#!/usr/bin/env python3
"""Reuse the gate harness against a private copy, with live health probes only."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gate_copy", Path(__file__).with_name("gate-you-real-copy-boot.py")
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
harness.STATE = harness.ROOT / ".ui-e2e-state" / "gate-pass2"
harness.DATA = harness.STATE / "data"
harness.PRIVATE = harness.STATE / "runtime"
harness.OUT = Path("/home/tnunamak/.tmp/gw-product/gate/v5")
harness.GATEWAY_PORT = 18531
harness.SIDECAR_PORT = 18532
# Presentation checks need copied registry/receipts, not the original media cache.
harness._copy_media_cache = lambda: {"copied": 0, "skipped": 0}
original_sandbox = harness._bwrap_base

def isolated_sandbox():
    argv = original_sandbox()
    # Hide the live checkout as well as making its filesystem unwritable.
    argv[-1:-1] = ["--tmpfs", str(harness.LIVE_CHECKOUT)]
    return argv

harness._bwrap_base = isolated_sandbox
if __name__ == "__main__":
    harness.main()
