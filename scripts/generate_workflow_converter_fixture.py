#!/usr/bin/env python3
"""Emit the converter golden from committed test inputs in the hermetic runner.

Run ``scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_converter_fixture.py``.
Parse CONVERTER_GOLDEN_JSON and save it only after reviewing the additive diff.
This does not rewrite the historical production migration snapshot.
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
    sys.path[:0] = [str(root), str(root / "tests")]
    import conftest  # noqa: F401
    from tests.test_workflow_convert_golden import _current

    serialized = json.dumps(_current(), sort_keys=True, indent=1) + "\n"
    print("CONVERTER_GOLDEN_JSON=" + json.dumps(serialized))


if __name__ == "__main__":
    main()
