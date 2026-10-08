#!/usr/bin/env python3
"""Check whether each `<name>.ui.json` workflow still round-trips to its
sibling `<name>.json` API workflow.

Drift means: someone edited the API JSON without regenerating the UI twin (or
vice versa), and outputs will embed a stale workflow that no longer matches
what the proxy actually submits.

Two checks run for each `*.ui.json`:

  1. NAME CHECK (authoritative, offline). Pair every `widgets_values` entry
     with its input name (see `ui_inputs_by_name`) and compare those named
     values and links against the sibling `<name>.json` (`_meta` stripped).
     Pairing by name keeps the result independent of the frontend's widget
     order. ComfyUI frontend 1.53 inserted a `control_after_generate` slot
     after every seed widget and keeps value slots for linked widgets; a
     positional round-trip then shifted every later value (`cfg: 1.0 ->
     'euler'`) in UI twins saved by an older frontend, which reported false
     drift.
  2. FRONTEND LAYOUT (advisory, needs Playwright and a running ComfyUI).
     Load the UI workflow, call `app.graphToPrompt()` and report when the
     result differs from the API JSON. This shows that a person who opens
     the twin in the current frontend sees shifted values. It never changes
     the exit code, because the gateway executes the API JSON.

Exit codes: 0 no drift, 1 drift in the name check, 2 usage or input error.
A missing Playwright or ComfyUI only skips check 2 and prints a notice.

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
_BENIGN_INPUT_FIELDS = {"device", "upload"}  # `upload`: LoadImage button widget


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


_CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}
_SEED_WIDGETS = {"seed", "noise_seed"}


def _layout(widget_names: list[str], linked: set[str], values: list) -> list[str | None]:
    """Return one input name (or None for a control slot) per stored value.

    Candidate layouts, tried in order; the first whose length equals
    ``len(values)`` wins: [linked widgets keep a slot or not] x [seed widgets
    are followed by a control slot or not]. If no length fits, the widgets
    were extended after the file was saved: pair the unlinked widgets in
    order and take control slots from the value (``fixed``/``randomize``...).
    """
    def build(keep_linked: bool, control: bool) -> list[str | None]:
        slots: list[str | None] = []
        for name in widget_names:
            if name in linked and not keep_linked:
                continue
            slots.append(name)
            if control and name in _SEED_WIDGETS:
                slots.append(None)
        return slots

    for keep_linked, control in ((True, True), (True, False), (False, True), (False, False)):
        slots = build(keep_linked, control)
        if len(slots) == len(values):
            return slots
    slots = []
    for name in widget_names:
        if name in linked:
            continue
        slots.append(name)
        if name in _SEED_WIDGETS and len(slots) < len(values) and values[len(slots)] in _CONTROL_VALUES:
            slots.append(None)
    return slots[: len(values)]


def ui_inputs_by_name(ui_wf: dict) -> dict[str, dict]:
    """Map each UI node id to its named inputs, as the API JSON spells them.

    Returns ``{node_id: {input_name: value_or_[origin_id, slot]}}``.

    ``widgets_values`` is positional and its layout depends on the frontend
    that saved the file (see ``_layout``): linked widgets may keep a value
    slot, and seed widgets may be followed by a ``control_after_generate``
    slot. Each value is paired with its input name, so the comparison does
    not depend on position. Dict values (autogrow inputs) are flattened to
    ``name.key`` like the API format does.
    """
    links = {l[0]: l for l in ui_wf.get("links", []) if isinstance(l, list)}
    out: dict[str, dict] = {}
    for node in ui_wf.get("nodes", []):
        named: dict = {}
        widget_names: list[str] = []
        linked: set[str] = set()
        for inp in node.get("inputs", []) or []:
            link = links.get(inp.get("link")) if inp.get("link") is not None else None
            if link is not None:
                named[inp["name"]] = [str(link[1]), link[2]]
                linked.add(inp["name"])
            if inp.get("widget"):
                widget_names.append(inp["name"])
        values = node.get("widgets_values")
        if isinstance(values, list):
            for name, value in zip(_layout(widget_names, linked, values), values):
                if name is None or name in linked:
                    continue
                if isinstance(value, dict):
                    for k, v in value.items():
                        named[f"{name}.{k}"] = v
                else:
                    named[name] = value
        for field in _BENIGN_INPUT_FIELDS:
            named.pop(field, None)
        out[str(node["id"])] = named
    return out


def name_diff(ui_wf: dict, api_wf: dict) -> list[str]:
    """Diff the named inputs of a UI twin against its API JSON.

    Only inputs the UI twin states are compared. API inputs that the UI twin
    has no widget for (for example a dynamic-combo child such as
    ``resize_type.multiplier``) cannot be checked and are skipped.
    """
    api = normalize_api(api_wf)
    diffs: list[str] = []
    ui_nodes = ui_inputs_by_name(ui_wf)
    for node_id in sorted(set(api) - set(ui_nodes), key=str):
        diffs.append(f"  - node {node_id} in API JSON but not in UI twin")
    for node_id, ui_inputs in ui_nodes.items():
        node = api.get(node_id)
        if node is None:
            diffs.append(f"  + node {node_id} in UI twin but not in API JSON")
            continue
        api_inputs = node.get("inputs", {})
        for name, ui_val in sorted(ui_inputs.items()):
            if name not in api_inputs:
                diffs.append(f"  + {node_id}.inputs.{name}: only in UI twin ({ui_val!r})")
            elif api_inputs[name] != ui_val:
                diffs.append(
                    f"  ~ {node_id}.inputs.{name}: api {api_inputs[name]!r} != ui {ui_val!r}"
                )
    return diffs


async def frontend_layout_check(
    entries: list[tuple[str, dict, dict]],
) -> tuple[list[str], str | None]:
    """Advisory check 2. Returns (labels whose UI twin the current frontend
    loads with shifted values, reason the check was skipped or None)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "playwright not installed (pip install -r requirements.txt)"
    shifted: list[str] = []
    try:
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
            for label, ui_wf, api_disk in entries:
                round_trip = await page.evaluate(
                    """async (uiWorkflow) => {
                        await window.app.loadGraphData(uiWorkflow, true, true, 'drift-check');
                        await new Promise(r => setTimeout(r, 300));
                        const out = await window.app.graphToPrompt();
                        return {ok: true, prompt: out.output};
                    }""",
                    ui_wf,
                )
                # Inputs the frontend adds (new optional defaults) are not a
                # layout problem; changed or missing values are.
                lines = shallow_diff(normalize_api(api_disk), normalize_api(round_trip["prompt"]))
                if any(not l.startswith("  + ") for l in lines):
                    shifted.append(label)
            await browser.close()
    except Exception as e:  # noqa: BLE001 - advisory only
        return shifted, f"frontend check failed: {type(e).__name__}: {str(e)[:200]}"
    return shifted, None


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--paths", nargs="*", help="Specific .ui.json paths to check (default: all)"
    )
    parser.add_argument(
        "--no-frontend", action="store_true", help="Skip the advisory frontend check"
    )
    args = parser.parse_args(argv)

    if args.paths:
        ui_paths = [Path(p).resolve() for p in args.paths]
    else:
        ui_paths = find_ui_workflows()

    if not ui_paths:
        print("no .ui.json files found")
        return 0

    drifted = 0
    entries: list[tuple[str, dict, dict]] = []
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
        entries.append((label, ui_wf, api_disk))
        diffs = name_diff(ui_wf, api_disk)
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

    if not args.no_frontend:
        shifted, skipped = await frontend_layout_check(entries)
        for label in shifted:
            print(f"[layout] {label}: the current ComfyUI frontend loads this UI twin with changed values (legacy widget layout)")
        if skipped:
            print(f"[note] frontend layout check skipped: {skipped}", file=sys.stderr)

    if drifted:
        print(f"\n{drifted} workflow(s) drifted")
        return 1
    print("\nno drift")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
