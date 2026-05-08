#!/usr/bin/env python3
"""Check whether each `<name>.ui.json` workflow still round-trips to its
sibling `<name>.json` API workflow.

Drift means: someone edited the API JSON without regenerating the UI twin (or
vice versa), and outputs will embed a stale workflow that no longer matches
what the proxy actually submits.

The script drives the running ComfyUI frontend (default
http://127.0.0.1:7890) via Playwright. For each `*.ui.json`:

  1. Load the UI workflow into the frontend.
  2. Call `app.graphToPrompt()` to get the API-format equivalent.
  3. Compare against the sibling `<name>.json` (with `_meta` stripped).

Exits 0 if no drift, 1 if any drift detected.

Requires `playwright` (Python). Install once:
  pip install playwright && playwright install chromium

Usage:
  python scripts/check_workflow_drift.py
  python scripts/check_workflow_drift.py --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_DIRS = [
    _REPO_ROOT / "workflows",
    _REPO_ROOT / "workflows" / "images",
]
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:7890")


def find_ui_workflows() -> list[Path]:
    out: list[Path] = []
    for d in REPO_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.ui.json")):
            api = p.with_name(p.name[: -len(".ui.json")] + ".json")
            if api.exists():
                out.append(p)
    return out


# Per-node fields injected by `graphToPrompt()` that don't reflect drift.
# `device` is added by GGUF/MultiGPU loader nodes when their UI widget defaults
# to "default"; the API JSON omits it because the value is optional and not
# user-edited. Drop it from both sides so it doesn't generate false positives.
_BENIGN_INPUT_FIELDS = {"device"}


def normalize_api(wf: dict) -> dict:
    """Strip proxy-private metadata and per-node UI hints for semantic diffing.

    ComfyUI's `graphToPrompt()` adds synthetic per-node `_meta` (node title)
    and may inject default fields like `device` on certain loader nodes. We
    don't want those flagged as drift — only changes to the API graph that
    actually affect generation matter.
    """
    wf = json.loads(json.dumps(wf))  # deep copy
    wf.pop("_meta", None)
    wf.pop("__ui_workflow__", None)
    # Strip per-node `_meta` (title hints injected by graphToPrompt) and benign
    # auto-injected default fields so we compare the executable graph only.
    for node_id, node in list(wf.items()):
        if isinstance(node, dict):
            node.pop("_meta", None)
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                for f in _BENIGN_INPUT_FIELDS:
                    inputs.pop(f, None)
    return wf


def shallow_diff(a: dict, b: dict, path: str = "") -> list[str]:
    """Return human-readable diff lines between two API workflows."""
    diffs: list[str] = []
    a_keys = set(a)
    b_keys = set(b)
    for k in sorted(a_keys - b_keys):
        diffs.append(f"  - missing in roundtrip: {path}{k}")
    for k in sorted(b_keys - a_keys):
        diffs.append(f"  + added by roundtrip:   {path}{k}")
    for k in sorted(a_keys & b_keys):
        av, bv = a[k], b[k]
        if isinstance(av, dict) and isinstance(bv, dict):
            diffs.extend(shallow_diff(av, bv, f"{path}{k}."))
        elif av != bv:
            diffs.append(f"  ~ {path}{k}: {av!r} -> {bv!r}")
    return diffs


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--paths", nargs="*", help="Specific .ui.json paths to check (default: all)"
    )
    args = parser.parse_args(argv)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "playwright not installed; run: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 2

    if args.paths:
        ui_paths = [Path(p).resolve() for p in args.paths]
    else:
        ui_paths = find_ui_workflows()

    if not ui_paths:
        print("no .ui.json files found")
        return 0

    drifted = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(COMFY_URL)
        await page.wait_for_function(
            "typeof window.app !== 'undefined' && window.app && "
            "typeof window.app.loadGraphData === 'function' && "
            "typeof window.app.graphToPrompt === 'function'",
            timeout=60000,
        )
        await asyncio.sleep(2)

        for ui_path in ui_paths:
            api_path = ui_path.with_name(ui_path.name[: -len(".ui.json")] + ".json")
            label = api_path.name
            if not api_path.exists():
                print(f"[skip] {label}: no API sibling")
                continue
            with open(ui_path) as f:
                ui_wf = json.load(f)
            with open(api_path) as f:
                api_disk = json.load(f)
            try:
                round_trip = await page.evaluate(
                    """async (uiWorkflow) => {
                        await window.app.loadGraphData(uiWorkflow, true, true, 'drift-check');
                        await new Promise(r => setTimeout(r, 300));
                        const out = await window.app.graphToPrompt();
                        return {ok: true, prompt: out.output};
                    }""",
                    ui_wf,
                )
            except Exception as e:
                print(f"[fail] {label}: roundtrip raised: {e}")
                drifted += 1
                continue
            if not round_trip.get("ok"):
                print(f"[fail] {label}: {round_trip.get('error')}")
                drifted += 1
                continue
            api_round = round_trip["prompt"]
            disk_norm = normalize_api(api_disk)
            round_norm = normalize_api(api_round)
            diffs = shallow_diff(disk_norm, round_norm)
            if diffs:
                drifted += 1
                print(f"[drift] {label}")
                if args.verbose:
                    for line in diffs[:50]:
                        print(line)
                    if len(diffs) > 50:
                        print(f"  ... ({len(diffs) - 50} more)")
                else:
                    print(f"  {len(diffs)} difference(s); pass --verbose for detail")
            else:
                print(f"[ok]    {label}")

        await browser.close()

    if drifted:
        print(f"\n{drifted} workflow(s) drifted")
        return 1
    print("\nno drift")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
