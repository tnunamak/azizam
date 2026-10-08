#!/usr/bin/env python3
"""Build blind A/B pairs for TARGET.md T-1 and score the judges' verdicts.

  pairs.py build <capture-dir> <out-dir> [--seed N] [--theme light]
  pairs.py score <out-dir> <verdict.json>...

`build` pairs each Azizam screen (populated state, owner view unless the
surface is member-only) with every benchmark for the same job and width. It
writes pair-NNN/A.png and B.png in random order, plus the pair's job, to
<out-dir>. The answer key goes to ~/.local/share/azizam-judging-keys/<name>.key.json,
away from everything the judges can see.

`score` reads verdict files shaped as {"pairs": [{"pair": "pair-001",
"winner": "A"|"B"|"tie", ...}]} and prints the T-1 win-or-tie rate per surface
and overall, using the key.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
KEYS = Path.home() / ".local/share/azizam-judging-keys"

# Azizam capture surface -> benchmark folder, and the job both screens do.
SURFACES = {
    "home": ("home", "Member landing: what can I do, is it ready, connect an app"),
    "status": ("status", "Is it working, what is broken, who is affected, what to do"),
    "library": ("library", "Browse, find and play what the household made"),
    "try": ("try", "Pick a model or workflow, run it, see the result and its cost"),
    "models": ("catalog", "Browse the models and what each can do"),
    "workflows": ("catalog", "Browse the workflows and what each can do"),
    "apps": ("keys", "Create an API key and connect a client"),
    "usage": ("usage", "Spend and requests over time, and failures"),
    "activity": ("usage", "Find a request and read its receipt"),
    "engines": ("engines", "Owner configures backends and sees their health"),
    "providers": ("engines", "Owner configures paid providers, caps and spend"),
    "people": ("people", "Members, invites and access plans"),
    "settings": ("settings", "Account and instance settings"),
    "setup": ("onboarding", "First-run setup and sign-in"),
}

PHONE = re.compile(r"(phone|iphone|ios)", re.I)
CAPTURE = re.compile(
    r"^(?P<role>owner|member)_(?P<surface>[a-z]+)_.*_(?P<state>[a-z0-9-]+)_(?P<theme>light|dark)_(?P<width>\d+)_settled\.png$"
)


def benchmark_width(name: str) -> str:
    return "390" if PHONE.search(name) else "1440"


def to_png(src: Path, dest: Path) -> None:
    if src.suffix.lower() == ".png":
        shutil.copyfile(src, dest)
    else:
        subprocess.run(["convert", str(src), str(dest)], check=True)


def build(capture_dir: Path, out: Path, seed: int, theme: str) -> None:
    rng = random.Random(seed)
    screens: dict[tuple[str, str], Path] = {}
    for png in sorted(capture_dir.glob("*.png")):
        m = CAPTURE.match(png.name)
        if not m or m["theme"] != theme or m["state"] != "page" or m["surface"] not in SURFACES:
            continue
        key = (m["surface"], m["width"])
        # Prefer the owner view; a member-only surface falls back to member.
        if key not in screens or m["role"] == "owner":
            screens[key] = png
    out.mkdir(parents=True, exist_ok=False)
    key_rows = []
    pairs = []
    for (surface, width), shot in sorted(screens.items()):
        folder, job = SURFACES[surface]
        for bench in sorted((BENCH / folder).glob("*.webp")):
            if benchmark_width(bench.name) != width:
                continue
            pairs.append((surface, width, job, shot, bench))
    rng.shuffle(pairs)
    for i, (surface, width, job, shot, bench) in enumerate(pairs, 1):
        pair = out / f"pair-{i:03d}"
        pair.mkdir()
        azizam_is_a = rng.random() < 0.5
        to_png(shot, pair / ("A.png" if azizam_is_a else "B.png"))
        to_png(bench, pair / ("B.png" if azizam_is_a else "A.png"))
        (pair / "job.txt").write_text(f"{job} ({'phone' if width == '390' else 'desktop'})\n")
        key_rows.append({"pair": pair.name, "surface": surface, "width": width,
                         "azizam": "A" if azizam_is_a else "B",
                         "azizam_file": shot.name, "benchmark": f"{bench.parent.name}/{bench.name}"})
    KEYS.mkdir(parents=True, exist_ok=True)
    key = KEYS / f"{out.name}.key.json"
    key.write_text(json.dumps({"seed": seed, "theme": theme, "pairs": key_rows}, indent=1))
    missing = sorted({s for s in SURFACES} - {s for s, _ in screens})
    print(f"{len(key_rows)} pairs in {out}; key {key}")
    if missing:
        print("no populated capture for: " + ", ".join(missing))


def score(out: Path, verdict_files: list[Path]) -> None:
    """Report T-1, T-2 and T-3 for each judge on their own (TARGET.md calibration)."""
    key = {row["pair"]: row for row in
           json.loads((KEYS / f"{out.name}.key.json").read_text())["pairs"]}
    for vf in verdict_files:
        verdict = json.loads(vf.read_text())
        tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        sums = {side: defaultdict(float) for side in ("azizam", "bench")}
        ai_made = holds = n = 0
        for v in verdict["pairs"]:
            row = key[v["pair"]]
            az = row["azizam"]
            bm = "B" if az == "A" else "A"
            ok = v["winner"] in ("tie", az)
            for bucket in (row["surface"], "ALL"):
                tally[bucket][0] += ok
                tally[bucket][1] += 1
            for crit, val in v["scores"][az].items():
                sums["azizam"][crit] += val
            for crit, val in v["scores"][bm].items():
                sums["bench"][crit] += val
            ai_made += bool(v.get("ai_made", {}).get(az))
            holds += bool(v.get("holds_up", {}).get(az))
            n += 1
        print(f"== {verdict.get('judge', vf.stem)}")
        for bucket, (good, total) in sorted(tally.items(), key=lambda kv: kv[0] != "ALL"):
            rate = good / total if total else 0.0
            target = 0.5 if bucket == "ALL" else 1 / 3
            print(f"  T-1 {bucket:10s} {good:3d}/{total:<3d} {rate:5.0%}{'' if rate >= target else '  FAIL'}")
        for crit in sums["azizam"]:
            a, b = sums["azizam"][crit] / n, sums["bench"][crit] / n
            print(f"  T-2 {crit:12s} azizam {a:4.1f} bench {b:4.1f}{'' if a >= b else '  FAIL'}")
        print(f"  T-3 ai_made {ai_made}/{n}{'' if ai_made == 0 else '  FAIL'}   holds_up {holds}/{n}")


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "build":
        seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 1
        theme = argv[argv.index("--theme") + 1] if "--theme" in argv else "light"
        build(Path(argv[1]), Path(argv[2]), seed, theme)
        return 0
    if len(argv) >= 3 and argv[0] == "score":
        score(Path(argv[1]), [Path(p) for p in argv[2:]])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
