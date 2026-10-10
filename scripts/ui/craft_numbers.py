#!/usr/bin/env python3
"""Measure the product against DESIGN.md's "System numbers v2" and fail on drift.

Usage: scripts/ui/craft_numbers.py [--fail] [--routes home,status] [--widths 1440,390]
                                   [--manifest PATH] [--keep DIR]

It runs docs/design/craft-system/azizam/_scripts/measure.mjs (Playwright computed
styles) against the household fixture and checks each measured route at 1440 and
390 (DESIGN.md, "Gates for these numbers"):

  controls  desktop control heights in {32, 40} (media tiles, textareas, switches
            and link-styled buttons excepted; a tab or segment counts as its group)
  type      every text size is in the type table, and a page uses at most 4 sizes
            plus badge text (10 only for a person mark's initial)
  rows      ListRows and table body rows are 36, 40 or 60 on desktop and 48 or 64
            on a phone; setting rows are at least 64
  geometry  page padding 32 at 1440 and 20 at 390; sections 32 apart
  cards     cards have radius 8 and no shadow
  colour    at 1440, coloured pixels outside media and charts are at most 2 % of
            the screen and 10 % of the ink (the method of tools/colorshare.py)

Every check is reported for every measured route. With --fail it exits 1 when a
check that scripts/ui/craft_numbers.json enforces for that route fails. Without
--routes it measures the enforced routes (all routes with --all).

Without --manifest it starts scripts/ui/serve.py with the household seed on
UI_E2E_PORT (default 18180; Python from UI_E2E_PYTHON, else ./venv, else this
interpreter) and stops it afterwards. With --manifest it measures a household
server that is already running. Measurements go to a temporary directory that is
removed unless --keep names one.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).with_suffix(".json")
MEASURE = REPO / "docs/design/craft-system/azizam/_scripts/measure.mjs"
CHECKS = ("controls", "type", "rows", "geometry", "cards", "colour")


def arg(name: str) -> str | None:
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return None


def near(value: float, targets: list[float], tolerance: float) -> bool:
    return any(abs(value - target) <= tolerance for target in targets)


def px(value: str) -> float:
    try:
        return float(str(value).split()[0].replace("px", "") or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- checks


def check_controls(data: dict, width: int, cfg: dict) -> list[str]:
    rule = cfg["controls"]
    if width != cfg["desktop_width"]:
        return []
    problems = []
    for control in data["controls"]:
        if control["type"] in rule["exempt_types"] or control.get("variant") in rule["exempt_variants"]:
            continue
        height = control["height"]
        if control["type"] == "button" and height > rule["tile_min"]:
            continue
        # A tab or a Segmented option is measured by its group (32 group, 28 segments).
        if (control["type"] == "tab/segment" or control.get("pressed")) and control.get("groupHeight"):
            height = control["groupHeight"]
        if not near(height, rule["desktop_heights"], rule["tolerance"]):
            problems.append(f"{control['type']} {control['text'][:30]!r} is {height:g} tall")
    return problems


def check_type(data: dict, width: int, cfg: dict) -> list[str]:
    rule = cfg["type"]
    problems = []
    sizes = Counter()
    for text in data["text"]:
        size = text["fontSize"]
        if size == rule["mark_size"] and text.get("inMark"):
            continue
        if size not in rule["sizes"]:
            problems.append(f"{size:g} px text ({text['role']}) is not in the type table")
        if text["role"] == "badge" and size == rule["badge_size"]:
            continue
        sizes[size] += 1
    if len(sizes) > rule["max_sizes_per_page"]:
        listed = ", ".join(f"{size:g}" for size in sorted(sizes))
        problems.append(f"{len(sizes)} text sizes ({listed}); at most {rule['max_sizes_per_page']} plus badge text")
    return problems


def check_rows(data: dict, width: int, cfg: dict) -> list[str]:
    rule = cfg["rows"]
    allowed = rule["desktop"] if width == cfg["desktop_width"] else rule["phone"]
    problems = []
    for row in data.get("listRows", []):
        if row["kind"] == "setting-row":
            if row["height"] + rule["tolerance"] < rule["setting_min"]:
                problems.append(f"setting row {row['text'][:30]!r} is {row['height']:g} tall")
        elif not near(row["height"], allowed, rule["tolerance"]):
            problems.append(f"{row['kind']} {row['text'][:30]!r} is {row['height']:g} tall")
    return problems


def check_geometry(data: dict, width: int, cfg: dict) -> list[str]:
    rule = cfg["geometry"]
    layout = data.get("layout") or {}
    problems = []
    want = rule["page_pad"].get(str(width))
    padding = (layout.get("main") or {}).get("padding", "")
    parts = padding.split()
    left = px(parts[3]) if len(parts) == 4 else None
    if want is not None and left is not None and abs(left - want) > rule["tolerance"]:
        problems.append(f"page padding is {left:g}, not {want}")
    for gap in layout.get("sectionGaps") or []:
        if abs(gap - rule["section_gap"]) > rule["tolerance"]:
            problems.append(f"a section gap is {gap:g}, not {rule['section_gap']}")
    return problems


def check_cards(data: dict, width: int, cfg: dict) -> list[str]:
    rule = cfg["cards"]
    problems = []
    for card in data["cards"]:
        box = card["box"]
        if box["w"] < rule["min_width"] or box["h"] < rule["min_height"] or not card["hasBorder"]:
            continue
        if any(name in card["classes"].split() for name in rule["exempt_classes"]):
            continue
        radius = px(card["borderRadius"])
        if abs(radius - rule["radius"]) > 0.5:
            problems.append(f"card {card['firstText'][:30]!r} has radius {radius:g}")
        if card["hasShadow"]:
            problems.append(f"card {card['firstText'][:30]!r} has a shadow")
    return problems


def colour_share(png: Path, masks: list[dict]) -> tuple[float, float]:
    """tools/colorshare.py's measure, with media and chart regions left out."""
    import numpy as np
    from PIL import Image

    image = Image.open(png).convert("RGB")
    full_width = image.width
    image.thumbnail((720, 720))
    scale = image.width / full_width
    hsv = np.asarray(image.convert("HSV")).astype(float) / 255
    coloured = (hsv[..., 1] >= 0.15) & (hsv[..., 2] > 0.2)
    rgb = np.asarray(image).astype(int)
    values, counts = np.unique(rgb.reshape(-1, 3), axis=0, return_counts=True)
    background = values[counts.argmax()]
    ink = np.abs(rgb - background).sum(-1) > 24
    keep = np.ones(coloured.shape, dtype=bool)
    for mask in masks:
        x0, y0 = int(mask["x"] * scale), int(mask["y"] * scale)
        x1, y1 = int((mask["x"] + mask["w"]) * scale + 1), int((mask["y"] + mask["h"]) * scale + 1)
        keep[max(y0, 0) : max(y1, 0), max(x0, 0) : max(x1, 0)] = False
    screen = float((coloured & keep).sum()) / coloured.size
    ink_kept = ink & keep
    of_ink = float((coloured & ink_kept).sum()) / max(int(ink_kept.sum()), 1)
    return screen, of_ink


def check_colour(data: dict, width: int, cfg: dict, png: Path) -> list[str]:
    rule = cfg["colour"]
    if width != cfg["desktop_width"]:
        return []
    if not png.exists():
        return ["no screenshot to measure"]
    try:
        screen, of_ink = colour_share(png, data.get("masks", []))
    except ImportError:
        return ["numpy and Pillow are needed for the colour check"]
    problems = []
    if screen > rule["screen_max"]:
        problems.append(f"coloured pixels are {screen:.1%} of the screen (max {rule['screen_max']:.0%})")
    if of_ink > rule["ink_max"]:
        problems.append(f"coloured pixels are {of_ink:.1%} of the ink (max {rule['ink_max']:.0%})")
    return problems


# ---------------------------------------------------------------- fixture


def python_for_fixture() -> str:
    if os.environ.get("UI_E2E_PYTHON"):
        return os.environ["UI_E2E_PYTHON"]
    local = REPO / "venv/bin/python"
    return str(local) if local.exists() else sys.executable


def start_fixture(work: Path) -> tuple[subprocess.Popen, Path]:
    port = int(os.environ.get("UI_E2E_PORT", "18180"))
    manifest = work / "manifest.json"
    env = {**os.environ, "UI_E2E_PORT": str(port), "UI_SEED_PROFILE": "household",
           "UI_HOUSEHOLD_MANIFEST_PATH": str(manifest), "UI_E2E_STATE_ROOT": str(work / "state")}
    log = (work / "serve.log").open("w")
    server = subprocess.Popen([python_for_fixture(), str(REPO / "scripts/ui/serve.py")], cwd=REPO,
                              env=env, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"the household fixture exited early; see {work / 'serve.log'}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
            if manifest.exists():
                return server, manifest
        except OSError:
            pass
        time.sleep(0.5)
    server.terminate()
    raise SystemExit("the household fixture did not become ready")


def stop_fixture(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


# ---------------------------------------------------------------- main


def main() -> int:
    cfg = json.loads(CONFIG.read_text())
    fail = "--fail" in sys.argv
    routes = [r for r in (arg("--routes") or "").split(",") if r] or (
        list(cfg["routes"]) if "--all" in sys.argv else list(cfg["enforce"])
    )
    unknown = [r for r in routes if r not in cfg["routes"]]
    if unknown:
        raise SystemExit(f"unknown routes: {', '.join(unknown)}")
    widths = [int(w) for w in (arg("--widths") or f"{cfg['desktop_width']},{cfg['phone_width']}").split(",")]
    keep = arg("--keep")
    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="craft-numbers-"))
    work.mkdir(parents=True, exist_ok=True)
    server = None
    try:
        manifest = Path(arg("--manifest")) if arg("--manifest") else None
        if manifest is None:
            server, manifest = start_fixture(work)
        out = work / "measure"
        env = {**os.environ, "MEASURE_SURFACES": ",".join(routes),
               "MEASURE_WIDTHS": ",".join(map(str, widths)), "MEASURE_SHOTS": "viewport"}
        subprocess.run(["node", str(MEASURE), str(manifest), str(out)], check=True, env=env,
                       stdout=subprocess.DEVNULL)
        enforced_failures = 0
        for route in routes:
            enforced = set(cfg["enforce"].get(route, []))
            for width in widths:
                data = json.loads((out / f"{route}-{width}.json").read_text())
                png = out / f"{route}-{width}.png"
                results = {
                    "controls": check_controls(data, width, cfg),
                    "type": check_type(data, width, cfg),
                    "rows": check_rows(data, width, cfg),
                    "geometry": check_geometry(data, width, cfg),
                    "cards": check_cards(data, width, cfg),
                    "colour": check_colour(data, width, cfg, png),
                }
                heights = sorted({c["height"] for c in data["controls"]
                                  if not (c["type"] == "button" and c["height"] > cfg["controls"]["tile_min"])})
                sizes = sorted({t["fontSize"] for t in data["text"]})
                print(f"== {route} @ {width}: control heights {', '.join(f'{h:g}' for h in heights) or '-'}; "
                      f"text sizes {', '.join(f'{s:g}' for s in sizes)}")
                for check in CHECKS:
                    problems = results[check]
                    gated = check in enforced
                    status = "ok" if not problems else ("FAIL" if gated else "report")
                    suffix = " (not enforced)" if problems and not gated else ""
                    print(f"  {check:9} {status}{suffix}")
                    counted = Counter(problems)
                    for problem, count in list(counted.items())[:12]:
                        print(f"    - {problem}{f' (x{count})' if count > 1 else ''}")
                    if len(counted) > 12:
                        print(f"    - and {len(counted) - 12} more")
                    if problems and gated:
                        enforced_failures += 1
        print(f"\n{enforced_failures} enforced check(s) failed")
        return 1 if fail and enforced_failures else 0
    finally:
        if server is not None:
            stop_fixture(server)
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
