#!/usr/bin/env python3
"""Emit parity captures from a committed hermetic checkout without writing fixtures.

Run ``scripts/run-hermetic-pytest.sh --python-script scripts/generate_workflow_parity_fixtures.py [name ...]``.
Each WORKFLOW_PARITY_JSON line contains a name and data from the documented
``tests/fixtures/c2d_parity/capture.py`` capture functions. Review the emitted
data before saving it with that module's canonical serializer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys


def main() -> None:
    if os.environ.get("HERMETIC_TEST_NAMESPACE") != "1":
        raise SystemExit("Use scripts/run-hermetic-pytest.sh --python-script; live stores must not be imported.")
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "tests")]
    import conftest  # noqa: F401
    from tests.support.c2d_parity import jsonable

    if sys.argv[1:] == ["--legacy-wire"]:
        import pytest
        from gateway.litellm_content import profile
        from tests.test_engine_protocol_legacy_wire import snapshot

        with pytest.MonkeyPatch.context() as patch:
            result = snapshot(patch)
        for name in ("compiled", "seeded_compiled"):
            assert result[name].pop("router_settings") == profile()["router_settings"]
            for section in ("general_settings", "litellm_settings"):
                for key, value in profile()[section].items():
                    assert result[name][section].pop(key) == value
        print("LEGACY_WIRE_JSON=" + json.dumps(result, sort_keys=True))
        return
    if sys.argv[1:] == ["--readers"]:
        from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRegistry
        from tests.workflow_reader_outputs import read_all

        print("WORKFLOW_READERS_JSON=" + json.dumps(read_all(WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR).records()), sort_keys=True))
        return
    captures = runpy.run_path(str(root / "tests/fixtures/c2d_parity/capture.py"))["CAPTURES"]
    names = sys.argv[1:] or sorted(captures)
    unknown = set(names) - captures.keys()
    if unknown:
        raise SystemExit(f"Unknown captures: {sorted(unknown)}")
    for name in names:
        print("WORKFLOW_PARITY_JSON=" + json.dumps({"name": name, "data": jsonable(captures[name]())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
