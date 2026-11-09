import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / "tests"))
import conftest  # noqa: F401
import test_headless_access as t
registered = t._registered_routes(t.proxy.app.routes)
actual = sorted(list(r) for r in registered if not t._inventoried(r[1]))
print("NONCONTROL_JSON=" + json.dumps(actual))
inv = sorted([r[0], r[1], r[2]] for r in registered if t._inventoried(r[1]))
print("INV_JSON=" + json.dumps(inv))
