import json
from pathlib import Path
import proxy

def test_dump():
    proxy.app.openapi_schema = None
    new = json.loads(json.dumps(proxy.app.openapi(), default=str))
    old = json.loads(Path("tests/fixtures/headless_openapi.json").read_text())
    out = []
    def walk(a, b, path=""):
        if type(a) != type(b):
            out.append(f"TYPE {path}"); return
        if isinstance(a, dict):
            for k in sorted(set(a) | set(b)):
                if k not in a: out.append(f"MISSING-new {path}/{k}")
                elif k not in b: out.append(f"EXTRA-new {path}/{k}")
                else: walk(a[k], b[k], f"{path}/{k}")
        elif isinstance(a, list):
            if len(a) != len(b): out.append(f"LEN {path} new={len(a)} old={len(b)}")
            for i, (x, y) in enumerate(zip(a, b)): walk(x, y, f"{path}[{i}]")
        elif a != b: out.append(f"DIFF {path} new={a!r:.100} old={b!r:.100}")
    walk(new, old)
    assert not out, "\n".join(out[:80])
