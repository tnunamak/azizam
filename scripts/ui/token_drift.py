#!/usr/bin/env python3
"""Token-drift lint: no raw colour, font size, radius or spacing outside the tokens.

Usage: scripts/ui/token_drift.py [--all] [--fail] [paths...]

Default scope is the redesigned surfaces and their primitives (docs/design/DESIGN.md,
"Token drift"). --all scans every file under web/src and reports legacy drift too.
Only web/src/styles/tokens.css may hold raw values. Data-driven inline widths
(style={{ width: `${share}%` }}) and viewport-relative layout sizes (vh, dvh, vw, %)
are allowed: they are measurements of data or the screen, not design values.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "web" / "src"
DEFAULT_SCOPE = [
    "app/home/page.tsx",
    "app/home/presentation.ts",
    "app/status",
    "app/library/page.tsx",
    "app/globals.css",
    "app/fonts.css",
    "components/shell/app-shell.tsx",
    "components/product",
    "components/ui/button.tsx",
    "components/ui/input.tsx",
    "components/ui/select.tsx",
    "components/ui/dropdown-menu.tsx",
    "components/ui/tooltip.tsx",
    "components/ui/command.tsx",
    "components/ui/switch.tsx",
    "components/ui/scroll-area.tsx",
    "components/ui/checkbox.tsx",
    "components/ui/tabs.tsx",
    "components/ui/popover.tsx",
    "components/ui/dialog.tsx",
    "components/ui/sheet.tsx",
    "components/ui/skeleton.tsx",
    "components/ui/alert.tsx",
    "components/ui/empty.tsx",
    "components/ui/overflow-menu.tsx",
    "components/ui/relative-time.tsx",
    "components/ui/sonner.tsx",
]
SKIP = re.compile(r"(\.spec\.|\.e2e\.|\.test\.|playwright|vitest|audit-tests|\.py$|\.mjs$)")

SPACING = r"(?:p|px|py|pt|pb|pl|pr|ps|pe|m|mx|my|mt|mb|ml|mr|ms|me|gap|gap-x|gap-y|space-x|space-y|inset|inset-x|inset-y|top|left|right|bottom|size|w|h|min-w|min-h|max-w|max-h|indent|scroll-m|scroll-p)"
RULES = [
    ("raw colour", re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b(?!\])")),
    ("raw colour", re.compile(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(")),
    ("arbitrary colour", re.compile(r"\b(?:bg|text|border|ring|fill|stroke|outline|from|to|via|decoration|shadow)-\[(?:#|rgb|hsl|oklch)")),
    ("raw font size", re.compile(r"\btext-\[\d[\d.]*(?:px|rem|em)\]")),
    ("raw font size", re.compile(r"\bfont-size\s*:\s*\d")),
    ("raw font size", re.compile(r"fontSize\s*:")),
    ("raw radius", re.compile(r"\brounded(?:-[a-z]{1,2})?-\[(?!inherit\])[^\]]+\]")),
    ("raw radius", re.compile(r"\bborder-radius\s*:\s*\d")),
    ("raw radius", re.compile(r"borderRadius\s*:")),
    ("raw spacing", re.compile(r"(?<![\w-])-?" + SPACING + r"-\[(?![^\]]*(?:vh|dvh|svh|vw|dvw|%|calc\(100(?:vw|vh|dvh|%)))[^\]]*\d(?:px|rem|em)[^\]]*\]")),
    ("raw spacing", re.compile(r"\b(?:padding|margin|gap)(?:-[a-z]+)?\s*:\s*\d")),
    ("raw spacing", re.compile(r"\b(?:padding|margin)(?:Top|Bottom|Left|Right)?\s*:\s*\d")),
    ("raw shadow", re.compile(r"\bshadow-\[")),
    ("raw leading or tracking", re.compile(r"\b(?:leading|tracking)-\[")),
]


def files(scope: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in scope:
        path = ROOT / item
        if path.is_dir():
            out += [p for p in path.rglob("*") if p.suffix in {".tsx", ".ts", ".css"}]
        elif path.exists():
            out.append(path)
    return sorted(p for p in set(out) if not SKIP.search(str(p)) and p.name != "tokens.css")


def scan(path: Path) -> list[tuple[int, str, str]]:
    hits = []
    in_block_comment = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line
        if in_block_comment:
            if "*/" not in code:
                continue
            code = code.split("*/", 1)[1]
            in_block_comment = False
        code = re.sub(r"/\*.*?\*/", "", code)
        if "/*" in code:
            code, in_block_comment = code.split("/*", 1)[0], True
        stripped = code.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for name, pattern in RULES:
            for match in pattern.finditer(code):
                hits.append((number, name, match.group(0)))
    return hits


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scope = args or (["."] if "--all" in sys.argv else DEFAULT_SCOPE)
    total = 0
    for path in files(scope):
        hits = scan(path)
        if not hits:
            continue
        total += len(hits)
        print(path.relative_to(ROOT))
        for number, name, text in hits:
            print(f"  {number}: {name}: {text}")
    print(f"\n{total} token-drift findings")
    return 1 if total and "--fail" in sys.argv else 0


if __name__ == "__main__":
    sys.exit(main())
