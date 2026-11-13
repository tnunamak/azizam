#!/usr/bin/env python3
"""Deploy post-check: the live OpenAPI document moves only where the gate's pin moved.

The gate pins the full OpenAPI document in tests/fixtures/headless_openapi.json (tests/test_headless_access.py fails
when the app's document differs from it). A deploy saves the live document before and after the build switch. This
check reads the pin at the commit that was live before the deploy and at the deployed commit, so an intended change
the gate re-pinned passes here too, with no second list of allowed moves to keep in step.

Documents are compared entry by entry: one entry per operation ("<path> <method>"), one per component
("components <section>/<name>"), and "document" for everything else at the top level (info, servers, security, tags).
Every entry in any of the four documents is checked:

- live matched the old pin before the deploy (or both lacked the entry): live must match the new pin after it, so the
  build changed exactly what the gate pinned, and nothing else, and every entry the pin adds or removes did so too;
- live differed from the old pin (live data shaped the entry) and the pin did not move it: it must not move;
- live differed from the old pin and the pin moved it: live must now match the new pin. Nothing proves that some other
  new value is only the pinned change applied to live data, so the check fails closed. At train 58 live matched the
  pin in all 330 entries, so no live entry is data-shaped today.

Prints entry names and short hashes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

PIN = "tests/fixtures/headless_openapi.json"
REPO = Path(__file__).resolve().parents[1]
ABSENT = None  # an entry missing from a document; OpenAPI entries are objects, never null


def _short(value: Any) -> str:
    if value is ABSENT:
        return "absent"
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def entries(document: Any) -> dict[str, Any]:
    """Split an OpenAPI document into the entries this check compares. Refuses anything that is not one."""
    if not isinstance(document, Mapping) or not isinstance(document.get("paths"), Mapping):
        raise ValueError("not an OpenAPI document (no paths object)")
    out: dict[str, Any] = {}
    for path, operations in document["paths"].items():
        if not isinstance(operations, Mapping):
            raise ValueError(f"path {path!r} is not an object")
        for method, operation in operations.items():
            out[f"{path} {method}"] = operation
    components = document.get("components") or {}
    if not isinstance(components, Mapping):
        raise ValueError("components is not an object")
    for section, items in components.items():
        if not isinstance(items, Mapping):
            raise ValueError(f"components {section!r} is not an object")
        for name, value in items.items():
            out[f"components {section}/{name}"] = value
    out["document"] = {key: value for key, value in document.items() if key not in ("paths", "components")}
    return out


def expected_after(live_before: Mapping[str, Any], pin_before: Mapping[str, Any],
                   pin_after: Mapping[str, Any], live_after: Mapping[str, Any]) -> dict[str, Any]:
    """The value each entry must have after the deploy (ABSENT: the entry must not exist)."""
    expected: dict[str, Any] = {}
    for key in set(live_before) | set(pin_before) | set(pin_after) | set(live_after):
        before, old_pin, new_pin = live_before.get(key), pin_before.get(key), pin_after.get(key)
        expected[key] = before if before != old_pin and old_pin == new_pin else new_pin
    return expected


def compare(live_before: Mapping[str, Any], live_after: Mapping[str, Any], pin_before: Mapping[str, Any],
            pin_after: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return (failures, notes); each line names one entry."""
    failures: list[str] = []
    notes: list[str] = []
    for key, want in sorted(expected_after(live_before, pin_before, pin_after, live_after).items()):
        got = live_after.get(key)
        moves = (f"live {_short(live_before.get(key))} -> {_short(got)}, "
                 f"pin {_short(pin_before.get(key))} -> {_short(pin_after.get(key))}")
        if pin_before.get(key) != pin_after.get(key):
            notes.append(f"pin moved {key}: {moves}")
        if got != want:
            failures.append(f"{key}: {moves}, expected {_short(want)}")
    return failures, notes


def pin_at(commit: str, repo: Path) -> dict[str, Any]:
    text = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{PIN}"],
                          capture_output=True, check=True, text=True).stdout
    return entries(json.loads(text))


def _load(path: str) -> dict[str, Any]:
    return entries(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pre", required=True, help="the live OpenAPI document saved before the build switch")
    parser.add_argument("--post", required=True, help="the live OpenAPI document saved after the build switch")
    pins = parser.add_mutually_exclusive_group(required=True)
    pins.add_argument("--commits", nargs=2, metavar=("BEFORE", "AFTER"),
                      help="the commit live ran before the deploy and the deployed commit; the pin is read at each")
    pins.add_argument("--pins", nargs=2, metavar=("BEFORE", "AFTER"), help="the two pinned OpenAPI documents as files")
    parser.add_argument("--repo", default=str(REPO), help="the repository the commits are read from")
    args = parser.parse_args(argv)
    try:
        live_before, live_after = _load(args.pre), _load(args.post)
        if args.commits:
            pin_before, pin_after = (pin_at(commit, Path(args.repo)) for commit in args.commits)
        else:
            pin_before, pin_after = (_load(path) for path in args.pins)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"OPENAPI PIN FAIL: cannot read the inputs ({type(error).__name__}: {error})")
        return 1
    failures, notes = compare(live_before, live_after, pin_before, pin_after)
    for line in notes:
        print("INFO " + line)
    for line in failures:
        print("FAIL " + line)
    tracked = sum(1 for key in live_before if live_before[key] == pin_before.get(key))
    print(f"OPENAPI PIN {'FAIL' if failures else 'PASS'}: {len(live_after)} entries live, {tracked} matched the pin "
          f"before, {len(notes)} moved in the pin, {len(failures)} unexpected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
