#!/usr/bin/env python3
"""Capture the headless OpenAPI fixture from a committed hermetic checkout.

Run with ``scripts/run-hermetic-pytest.sh --python-script scripts/generate_headless_fixtures.py``.
The one HEADLESS_OPENAPI_JSON line is JSON; boot diagnostics may precede it.
This script does not infer authorization metadata or add routes to the reviewed inventory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def main() -> None:
    if os.environ.get("HERMETIC_TEST_NAMESPACE") != "1":
        raise SystemExit("Use scripts/run-hermetic-pytest.sh --python-script; live stores must not be imported.")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tests"))
    # Install the same test-owned store paths collection uses, before importing the gateway.
    import conftest  # noqa: F401
    import test_headless_access

    test_headless_access.test_inventory_covers_registered_routes_and_documentation()
    test_headless_access.proxy.app.openapi_schema = None
    print("HEADLESS_OPENAPI_JSON=" + json.dumps(test_headless_access.proxy.app.openapi(), sort_keys=True))


if __name__ == "__main__":
    main()
