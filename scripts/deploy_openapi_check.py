#!/usr/bin/env python3
"""Deploy post-check: the live OpenAPI document moves only where the gate's pin moved.

The gate pins the full OpenAPI document in tests/fixtures/headless_openapi.json (tests/test_headless_access.py fails
when the app's document differs from it). A deploy snapshots the live document before and after the build switch.
This check reads the pin at the commit that was live before the deploy and at the deployed commit, so an intended
change the gate re-pinned passes here too, with no second list of allowed moves to keep in step.

Each entry is one operation ("path method") or "components". For each entry:

- live matched the old pin before the deploy: live must match the new pin after it (the build changed exactly what
  the gate pinned, and nothing else);
- live did not match the old pin (live data, such as an owner's workflow, shapes the entry): the entry must not move,
  unless the pin moved it too; then its new value is reported and accepted.

Snapshots are either an OpenAPI document or the deploy kit's snapshot ({"openapi_code": 200, "openapi": ["<path>
<method> <hash>", ..., "components <hash>"]}). Hashes are the first 16 hex digits of sha256 over the entry's JSON with
sorted keys, the form the deploy kit has always written. Prints entry names and hashes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Mapping

PIN = "tests/fixtures/headless_openapi.json"
REPO = Path(__file__).resolve().parents[1]
ANY = "*"  # an entry that live data shapes and the pin moved: any new value passes


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def entry_hashes(document: Mapping) -> dict[str, str]:
    """One hash per operation and one for components, keyed the way the deploy kit keys them."""
    out = {f"{path} {method}": _hash(operation)
           for path, operations in (document.get("paths") or {}).items() for method, operation in operations.items()}
    out["components"] = _hash(document.get("components"))
    return out


def snapshot_hashes(snapshot: Mapping) -> dict[str, str]:
    if "paths" in snapshot:
        return entry_hashes(snapshot)
    if snapshot.get("openapi_code") != 200:
        raise ValueError(f"the snapshot has no OpenAPI document (status {snapshot.get('openapi_code')})")
    rows = [row.rsplit(" ", 1) for row in snapshot["openapi"]]
    out = dict(rows)
    if len(out) != len(rows):
        raise ValueError("the snapshot lists an entry twice")
    return out


def expected_after(live_before: Mapping[str, str], pin_before: Mapping[str, str],
                   pin_after: Mapping[str, str]) -> dict[str, str | None]:
    """The value each entry must have after the deploy (None: absent; ANY: any value)."""
    expected: dict[str, str | None] = {}
    for key in set(live_before) | set(pin_before) | set(pin_after):
        before = live_before.get(key)
        if before == pin_before.get(key):
            expected[key] = pin_after.get(key)
        elif pin_before.get(key) == pin_after.get(key):
            expected[key] = before
        else:
            expected[key] = ANY
    return expected


def compare(live_before: Mapping[str, str], live_after: Mapping[str, str], pin_before: Mapping[str, str],
            pin_after: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return (failures, notes); each line names one entry."""
    failures: list[str] = []
    notes: list[str] = []
    for key, want in sorted(expected_after(live_before, pin_before, pin_after).items()):
        got = live_after.get(key)
        if pin_before.get(key) != pin_after.get(key):
            notes.append(f"pin moved {key}: {pin_before.get(key)} -> {pin_after.get(key)}; live {live_before.get(key)} -> {got}")
        if want != ANY and got != want:
            failures.append(f"{key}: live {live_before.get(key)} -> {got}, expected {want}")
    return failures, notes


def pin_at(commit: str, repo: Path) -> dict[str, str]:
    text = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{PIN}"],
                          capture_output=True, check=True, text=True).stdout
    return entry_hashes(json.loads(text))


def _load(path: str) -> Mapping:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pre", required=True, help="live snapshot taken before the build switch")
    parser.add_argument("--post", required=True, help="live snapshot taken after the build switch")
    pins = parser.add_mutually_exclusive_group(required=True)
    pins.add_argument("--commits", nargs=2, metavar=("BEFORE", "AFTER"),
                      help="the commit live ran before the deploy and the deployed commit; the pin is read at each")
    pins.add_argument("--pins", nargs=2, metavar=("BEFORE", "AFTER"), help="the two pinned OpenAPI documents as files")
    parser.add_argument("--repo", default=str(REPO), help="the repository the commits are read from")
    args = parser.parse_args(argv)
    try:
        live_before, live_after = snapshot_hashes(_load(args.pre)), snapshot_hashes(_load(args.post))
        if args.commits:
            pin_before, pin_after = (pin_at(commit, Path(args.repo)) for commit in args.commits)
        else:
            pin_before, pin_after = (entry_hashes(_load(path)) for path in args.pins)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"OPENAPI PIN FAIL: cannot read the inputs ({type(error).__name__}: {error})")
        return 1
    failures, notes = compare(live_before, live_after, pin_before, pin_after)
    for line in notes:
        print("INFO " + line)
    for line in failures:
        print("FAIL " + line)
    tracked = sum(1 for key in live_before if live_before[key] == pin_before.get(key))
    print(f"OPENAPI PIN {'FAIL' if failures else 'PASS'}: {len(live_after)} entries live, {tracked} tracked the pin "
          f"before, {len(notes)} moved in the pin, {len(failures)} unexpected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
