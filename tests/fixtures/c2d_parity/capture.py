#!/usr/bin/env python
"""Rewrite the code-to-data parity goldens from today's code.

Run it only when a golden must change on purpose, and review the diff. Run it from the repo root:

    ./venv/bin/python tests/fixtures/c2d_parity/capture.py [name ...]

For a hermetic capture, run ``scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_parity_fixtures.py [name ...]`` and review the WORKFLOW_PARITY_JSON lines before saving their data with ``c2d_parity.canonical``.

With no names it rewrites every golden. The parity tests only read goldens; they never write them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.support import c2d_parity  # noqa: E402
from tests.support import c2d_snapshots as snap  # noqa: E402


def _local_capabilities() -> dict:
    from tests.test_local_capabilities import capture_local_capabilities

    return capture_local_capabilities()


CAPTURES = {
    "local_capabilities": _local_capabilities,
    "capability_ids": snap.capture_capability_ids,
    "models_catalog": snap.capture_models_catalog,
    "extension_catalog": snap.capture_extensions,
    "model_metadata": snap.capture_model_metadata,
    "openapi": snap.capture_openapi,
    "workflows_catalog": snap.capture_workflows_catalog,
}


def main(argv: list[str]) -> int:
    names = argv or sorted(CAPTURES)
    unknown = [name for name in names if name not in CAPTURES]
    if unknown:
        print(f"unknown golden(s): {', '.join(unknown)}; known: {', '.join(sorted(CAPTURES))}", file=sys.stderr)
        return 2
    for name in names:
        c2d_parity.write_golden(name, CAPTURES[name]())
        print(f"wrote {c2d_parity.golden_path(name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
