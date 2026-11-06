"""T1.1 r4: the egress map of a freshly started gateway covers the services held only in the registry.

The map is projected from the config the gateway boots with. Before the fix it was projected from the bootstrap
file alone, so after every restart a service that lives only in the stored registry (Strata) was unmapped and
the send-time lease check did nothing for it until the next registry change or admin reload.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import proxy

PASS = {"enabled": True, "activation": "pass_through"}
DB_ONLY_PORT = 59711
GOOD = {"kind": "local", "enabled": True,
        "config": {"protocol": "openai-compatible", "host": "127.0.0.1", "port": DB_ONLY_PORT,
                   "models": [{"id": "m1"}], "gateway_lifecycle": True,
                   "residency": {"domain": "gpu-boot-test", "transition": PASS}}}
BAD = {"kind": "local", "enabled": True,
       "config": {"host": "10.0.0.9", "port": 59712, "models": ["this is not a model record"]}}


def _boot(tmp_path: Path, services: dict) -> dict:
    """Start the gateway module in a fresh process over a stored registry and report its egress map."""
    repo = Path(proxy.__file__).resolve().parent
    script = textwrap.dedent(f"""
        import json, os
        from pathlib import Path
        from gateway import admin_store
        db = Path(os.environ["AZIZAM_ADMIN_DB"])
        admin_store.init_db(db)
        admin_store.write_service_registry({{"schema_version": 1, "services": {services!r}}},
                                           updated_by="test", expected_generation=0, path=db)
        import proxy
        from gateway import engine_egress
        booted = {{key: list(value) for key, value in engine_egress.mapped_destinations().items()}}
        engine_egress.publish(None)
        engine_egress.publish(proxy.config["backends"])
        expected = {{key: list(value) for key, value in engine_egress.mapped_destinations().items()}}
        print("RESULT" + json.dumps({{"booted": booted, "expected": expected, "backends": sorted(proxy.config["backends"]),
                                      "problem": (proxy.REGISTRY_APPLY_PROBLEM or {{}}).get("mode")}}))
    """)
    env = dict(os.environ)
    for name, filename in [("AZIZAM_ADMIN_DB", "admin.db"), ("AZIZAM_METRICS_DB", "metrics.db"),
                           ("AZIZAM_RECEIPTS_DB", "receipts.db"), ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db"),
                           ("AZIZAM_RESPONSES_DB", "responses.db")]:
        env[name] = str(tmp_path / filename)
    env.update(PYTHONPATH=str(repo), HOME=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(next(line for line in done.stdout.splitlines() if line.startswith("RESULT"))[len("RESULT"):])


def test_a_service_held_only_in_the_registry_is_mapped_right_after_boot(tmp_path) -> None:
    """Fails before the fix: the booted map lacks the registry-only service."""
    result = _boot(tmp_path, {"db-only-engine": GOOD})
    assert result["problem"] is None
    assert "db-only-engine" in result["backends"]
    assert result["booted"] == result["expected"]
    assert result["booted"][f"127.0.0.1:{DB_ONLY_PORT}"] == ["gpu-boot-test"]


def test_the_partial_registry_fallback_publishes_too(tmp_path) -> None:
    """A stored registry with one bad record boots with the part that validates; that part is mapped."""
    result = _boot(tmp_path, {"db-only-engine": GOOD, "bad-engine": BAD})
    if result["problem"] != "partial":  # the record shape above must be the failing kind
        pytest.fail(f"expected the partial boot fallback, got {result['problem']!r}")
    assert result["booted"] == result["expected"]
    assert f"127.0.0.1:{DB_ONLY_PORT}" in result["booted"]
