#!/usr/bin/env python3
"""Count text-size, height and radius utility usage across web/src. Usage: utility_counts.py <web/src> -> markdown on stdout."""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SRC = Path(sys.argv[1])
FILES = [p for p in SRC.rglob("*") if p.suffix in (".tsx", ".ts", ".css") and p.is_file()]
# utility, optionally preceded by variant prefixes like sm: max-sm: [&_svg]: hover:
PATTERNS = {
    "text size": r"text-(?:2xs|xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|\[[0-9.]+(?:px|rem|em)\])",
    "height": r"(?:min-h|max-h|h|size)-(?:[0-9]+(?:\.5)?|\[[^\]\s]+\])",
    "radius": r"rounded(?:-(?:t|b|l|r|tl|tr|bl|br|s|e))?(?:-(?:none|xs|sm|md|lg|xl|2xl|3xl|full|control|panel|media|\[[^\]\s]+\]))?",
}
rx = {k: re.compile(r"(?<![\w-])((?:[\w\[\]&>*=:'\"().-]*:)?)(" + v + r")(?![\w-])") for k, v in PATTERNS.items()}

counts = {k: Counter() for k in PATTERNS}
by_file = {k: defaultdict(Counter) for k in PATTERNS}
for f in FILES:
    text = f.read_text(errors="ignore")
    rel = str(f.relative_to(SRC))
    for k, r in rx.items():
        for m in r.finditer(text):
            u = m.group(2)
            counts[k][u] += 1
            by_file[k][u][rel] += 1

out = [f"Scanned {len(FILES)} .tsx/.ts/.css files under web/src (variant prefixes such as sm:, max-sm:, [&_svg]: are folded into the base utility; test files included).", ""]
for k in PATTERNS:
    out += [f"### {k} utilities", "", "| Utility | Count | Files | Top files |", "|---|---|---|---|"]
    for u, n in sorted(counts[k].items(), key=lambda kv: -kv[1]):
        top = ", ".join(f"{fn} ({c})" for fn, c in by_file[k][u].most_common(3))
        out.append(f"| `{u}` | {n} | {len(by_file[k][u])} | {top} |")
    out.append("")
print("\n".join(out))
