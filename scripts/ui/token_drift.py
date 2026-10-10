#!/usr/bin/env python3
"""Token-drift lint: no raw colour, font size, radius or spacing outside the tokens.

Usage: scripts/ui/token_drift.py [--all] [--fail] [--warnings] [paths...]

Default scope is the redesigned surfaces and their primitives (docs/design/DESIGN.md,
"Token drift"). --all scans every file under web/src and reports legacy drift too.
Only web/src/styles/tokens.css may hold raw values. Data-driven inline widths
(style={{ width: `${share}%` }}) and viewport-relative layout sizes (vh, dvh, vw, %)
are allowed: they are measurements of data or the screen, not design values.

Craft-system rules (DESIGN.md "Gates for these numbers"): an unprefixed min-h-11 or
h-11 (44 px belongs below 640 only, under max-sm:), rounded-lg or rounded-xl, a
kit shadow (shadow-xs, shadow-sm, shadow-md) outside an overlay, and text-[Npx].
They fail in files already moved to the system (SYSTEM_FILES; each surface PR adds
its route) and are counted as warnings everywhere else (--warnings lists them). A
line that carries, or follows, a "craft-allow" comment is exempt, with its reason.
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
DEFAULT_SCOPE += [
    "components/ui/card.tsx",
    "components/ui/table.tsx",
    "components/ui/badge.tsx",
    "components/ui/textarea.tsx",
]
# Files moved to the craft system (docs/design/craft-system/PLAN.md). PR 1: the
# shared parts. Each surface PR adds its route's files.
SYSTEM_FILES = [
    "components/ui/badge.tsx",
    "components/ui/button.tsx",
    "components/ui/card.tsx",
    "components/ui/charts.tsx",
    "components/ui/compact-touch-targets.tsx",
    "components/ui/dialog.tsx",
    "components/ui/dropdown-menu.tsx",
    "components/ui/input.tsx",
    "components/ui/popover.tsx",
    "components/ui/select.tsx",
    "components/ui/sheet.tsx",
    "components/ui/skeleton.tsx",
    "components/ui/switch.tsx",
    "components/ui/table.tsx",
    "components/ui/tabs.tsx",
    "components/ui/textarea.tsx",
    "components/product/empty-state.tsx",
    "components/product/error-state.tsx",
    "components/product/filter-toggle.tsx",
    "components/product/key-value.tsx",
    "components/product/list-card.tsx",
    "components/product/page-header.tsx",
    "components/product/person-mark.tsx",
    "components/product/readiness.tsx",
    "components/product/segmented.tsx",
    "components/product/setting-row.tsx",
    "components/product/stat-strip.tsx",
    "components/product/status-ui.tsx",
    # PR 2: Settings
    "app/settings/page.tsx",
    # PR 3: People, invites and plans; Apps and keys
    "app/people/page.tsx",
    "app/people/plans.tsx",
    "app/apps/page.tsx",
    "components/product/secret-reveal.tsx",
]
# Overlays may lift with a shadow (menus, popovers, dialogs, sheets, toasts).
OVERLAY_FILES = {
    "components/ui/command.tsx",
    "components/ui/dialog.tsx",
    "components/ui/dropdown-menu.tsx",
    "components/ui/popover.tsx",
    "components/ui/select.tsx",
    "components/ui/sheet.tsx",
    "components/ui/sonner.tsx",
    "components/ui/tooltip.tsx",
}
SYSTEM_RULES = [
    ("44 px off the phone", re.compile(r"(?<![\w:/\[-])(?:min-h-11|h-11)(?![\w-])")),
    ("44 px off the phone", re.compile(r"(?<![\w-])(?:sm|md|lg|xl|2xl|min-\[[^\]]+\]):(?:min-h-11|h-11)(?![\w-])")),
    ("kit radius", re.compile(r"(?<![\w-])(?:[\w-]+:)*rounded-(?:lg|xl)(?![\w-])")),
    ("raw font size", re.compile(r"\btext-\[\d[\d.]*px\]")),
]
KIT_SHADOW = ("kit shadow", re.compile(r"(?<![\w-])(?:[\w-]+:)*shadow-(?:xs|sm|md)(?![\w-])"))
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


def code_lines(path: Path):
    """(line number, code) with comments removed; craft-allow lines dropped."""
    in_block_comment = False
    allow_next = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        allowed = allow_next or "craft-allow" in line
        # A craft-allow comment on its own line exempts the line after it.
        allow_next = "craft-allow" in line and line.strip().startswith(("//", "/*", "*", "{/*"))
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
        yield number, code, allowed


def scan_system(path: Path) -> list[tuple[int, str, str]]:
    rel = path.relative_to(ROOT).as_posix()
    rules = SYSTEM_RULES + ([] if rel in OVERLAY_FILES else [KIT_SHADOW])
    hits = []
    for number, code, allowed in code_lines(path):
        if allowed:
            continue
        for name, pattern in rules:
            for match in pattern.finditer(code):
                hits.append((number, name, match.group(0)))
    return hits


def scan(path: Path) -> list[tuple[int, str, str]]:
    hits = []
    for number, code, _allowed in code_lines(path):
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

    # Craft-system rules: errors in moved files, warnings elsewhere.
    system = {(ROOT / item).resolve() for item in SYSTEM_FILES}
    errors = 0
    warnings: dict[str, int] = {}
    for path in files(["."]):
        hits = scan_system(path)
        if not hits:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if path.resolve() in system:
            errors += len(hits)
            print(rel)
            for number, name, text in hits:
                print(f"  {number}: {name}: {text}")
        else:
            warnings[rel] = len(hits)
            if "--warnings" in sys.argv:
                print(f"{rel} (warning)")
                for number, name, text in hits:
                    print(f"  {number}: {name}: {text}")
    print(f"{errors} craft-system findings in moved files; "
          f"{sum(warnings.values())} warnings in {len(warnings)} files not yet moved"
          + ("" if "--warnings" in sys.argv else " (--warnings lists them)"))
    return 1 if (total or errors) and "--fail" in sys.argv else 0


if __name__ == "__main__":
    sys.exit(main())
