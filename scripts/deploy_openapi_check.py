#!/usr/bin/env python3
"""Deploy post-check: the live OpenAPI document moves only where the gate's pin moved.

The gate pins the full OpenAPI document in tests/fixtures/headless_openapi.json (tests/test_headless_access.py fails
when the app's document differs from it). A deploy saves the live document before and after the build switch. This
check reads the pin at the commit that was live before the deploy and at the deployed commit, so an intended change
the gate re-pinned passes here too, with no second list of allowed moves to keep in step.

Documents are compared entry by entry: one entry per operation ("<path> <method>"), one per component
("components <section>/<name>"), one presence entry per path item and per section (so an empty one still counts),
and "document" for everything else at the top level (info, servers, security, tags). Values compare as canonical JSON
text, so true and 1, or null and a missing entry, differ.
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
ABSENT = None  # an entry missing from a document; a present entry is JSON text, so null is "null"


def _short(value: str | None) -> str:
    if value is ABSENT:
        return "absent"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _canonical(value: Any) -> str:
    """JSON text with sorted keys: true and 1, 1 and 1.0, null and a missing entry all stay different."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def entries(document: Any) -> dict[str, str]:
    """Split an OpenAPI document into the entries this check compares, each as canonical JSON text. Besides one entry per
    operation and per component, "paths <path>", "components" and "components <section>" record that a path item or a
    section exists, so an empty one still counts. Refuses anything that is not an
    OpenAPI document, and any document whose entry names would collide."""
    if not isinstance(document, Mapping) or not isinstance(document.get("paths"), Mapping):
        raise ValueError("not an OpenAPI document (no paths object)")
    out: dict[str, str] = {}

    def put(name: str, value: Any) -> None:
        if name in out:
            raise ValueError(f"ambiguous entry name {name!r}")
        out[name] = _canonical(value)

    for path, operations in document["paths"].items():
        if not isinstance(path, str) or not isinstance(operations, Mapping):
            raise ValueError(f"path {path!r} is not a string naming an object")
        if not all(isinstance(method, str) for method in operations):
            raise ValueError(f"path {path!r} has a key that is not a string")
        put(f"paths {path}", "object")
        for method, operation in operations.items():
            put(f"{path} {method}", operation)
    if "components" in document:
        components = document["components"]
        if not isinstance(components, Mapping) or not all(isinstance(k, str) and "/" not in k for k in components):
            raise ValueError("components is not an object of named sections")
        put("components", "object")
        for section, items in components.items():
            if not isinstance(items, Mapping) or not all(isinstance(name, str) for name in items):
                raise ValueError(f"components {section!r} is not an object")
            put(f"components {section}", "object")
            for name, value in items.items():
                put(f"components {section}/{name}", value)
    put("document", {key: value for key, value in document.items() if key not in ("paths", "components")})
    return out


def expected_after(live_before: Mapping[str, str], pin_before: Mapping[str, str],
                   pin_after: Mapping[str, str], live_after: Mapping[str, str]) -> dict[str, str | None]:
    """The value each entry must have after the deploy (ABSENT: the entry must not exist)."""
    expected: dict[str, str | None] = {}
    for key in set(live_before) | set(pin_before) | set(pin_after) | set(live_after):
        before, old_pin, new_pin = live_before.get(key), pin_before.get(key), pin_after.get(key)
        expected[key] = before if before != old_pin and old_pin == new_pin else new_pin
    return expected


def compare(live_before: Mapping[str, str], live_after: Mapping[str, str], pin_before: Mapping[str, str],
            pin_after: Mapping[str, str]) -> tuple[list[str], list[str]]:
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


def pin_at(commit: str, repo: Path) -> dict[str, str]:
    text = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{PIN}"],
                          capture_output=True, check=True, text=True).stdout
    return entries(json.loads(text))


def _load(path: str) -> dict[str, str]:
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
