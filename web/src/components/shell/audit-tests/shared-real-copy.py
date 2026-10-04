"""Boot shared UI checks on copied data with operator connections blocked.

First copy the data directory into .ui-e2e-state/gw-copy-shared-real/data.
Only that copy is changed. Runtime secrets and logs stay in ignored files.
"""
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[5]
PORT = 18420
ENGINE_PORT = 18421
STATE = ROOT / ".ui-e2e-state/gw-copy-shared-real"
module = runpy.run_path(str(ROOT / "web/src/app/home/audit-tests/gate-you-real-copy-boot.py"))
context = module["prepare"].__globals__
context.update(STATE=STATE, DATA=STATE / "data", PRIVATE=STATE / "runtime",
               OUT=STATE / "results", GATEWAY_PORT=PORT, SIDECAR_PORT=ENGINE_PORT,
               _copy_media_cache=lambda: {"copied": 0, "skipped": 0})
manifest = module["prepare"]()
env = module["_env"](Path(manifest["config_path"]))
# fixture_gateway installs a socket/process audit guard before importing proxy.
# Even copied registry endpoints cannot reach a live engine or spawn lifecycle commands.
log = (STATE / "runtime/gateway.log").open("wb")
child = subprocess.Popen([str(ROOT / "venv/bin/python"), "-m", "scripts.ui.fixture_gateway",
                          str(PORT), str(ENGINE_PORT)], cwd=ROOT, env=env,
                         stdout=log, stderr=subprocess.STDOUT)

def stop(*_args):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    for _ in range(120):
        if child.poll() is not None:
            raise RuntimeError("Copied-data gateway exited; logs retained privately")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1).close()
            print(f"Copied-data gateway ready on {PORT}; operator connections blocked", flush=True)
            break
        except OSError:
            time.sleep(0.5)
    else:
        raise RuntimeError("Copied-data gateway readiness timeout")
    for line in sys.stdin:
        if line.strip() == "stop":
            break
except KeyboardInterrupt:
    pass
finally:
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    log.close()
    print("Copied-data gateway stopped", flush=True)
