#!/usr/bin/env python3
"""Offline audit: can every catalog workflow embed its ComfyUI UI graph?

Read-only. Fetches ``GET <host>/object_info`` (no GPU, no job submitted),
derives the UI workflow for every catalog workflow the way the gateway does,
and reports every workflow whose graph is missing, incomplete or whose save
node would not embed it.

    scripts/audit_comfy_workflow_metadata.py [--host http://127.0.0.1:7890]
    scripts/audit_comfy_workflow_metadata.py --write-fixture   # refresh the test fixture
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.engine_protocol import node_catalog as catalog  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "comfy_object_info_catalog.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="http://127.0.0.1:7890")
    ap.add_argument("--write-fixture", action="store_true")
    args = ap.parse_args()
    with urllib.request.urlopen(f"{args.host}/object_info", timeout=120) as resp:
        full = json.load(resp)
    paths = catalog.catalog_workflow_paths(ROOT, "comfyui")
    if args.write_fixture:
        classes: set[str] = set()
        for p in paths:
            classes |= catalog.class_types(json.loads(p.read_text()))
        FIXTURE.write_text(json.dumps(catalog.trim_object_info(full, classes), indent=1, sort_keys=True) + "\n")
        print(f"wrote {FIXTURE.relative_to(ROOT)}")
    failed = 0
    for p in paths:
        problems = catalog.audit_workflow(p, full.get)
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'ok  '} {p.relative_to(ROOT)}")
        for problem in problems:
            print(f"       {problem}")
    print(f"{len(paths) - failed}/{len(paths)} workflows ok")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
