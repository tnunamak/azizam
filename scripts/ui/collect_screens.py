#!/usr/bin/env python3
"""Copy final UI screenshots into the integrated review directory."""

from __future__ import annotations

import argparse
import shutil
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "web" / "test-results" / "screens"

STATIC_NA = {
    "design",
    "design-dialog",
    "design-sheet",
    "gallery",
    "docs",
    "redoc",
    "admin-profiles-p1v-fixture",
    "admin-status-p1v-fixture",
}

THEMES = ("light", "dark")
WIDTHS = ("1440", "390")
STATES = ("seeded", "empty", "error")

ROUTE_COVERAGE = {
    "/": {
        "groups": ("home",),
        "states": STATES,
        "note": "Covered by the Home matrix; the root route redirects to Home.",
    },
    "/activity": {
        "groups": ("activity-audit", "activity-requests"),
        "states": STATES,
        "note": "Activity has separate Audit and Requests tab matrices.",
    },
    "/admin": {"groups": ("admin-overview",), "states": STATES, "note": "Retained legacy admin overview."},
    "/admin/apps": {"groups": ("admin-apps",), "states": STATES, "note": "Retained legacy Apps surface."},
    "/admin/audit": {"groups": ("admin-audit",), "states": STATES, "note": "Retained legacy Audit surface."},
    "/admin/profiles": {"groups": ("admin-profiles",), "states": STATES, "note": "Retained legacy Profiles surface."},
    "/admin/profiles/p1v-fixture": {
        "groups": ("admin-profiles-p1v-fixture",),
        "states": ("n-a",),
        "note": "Static fixture route; data state is N/A.",
    },
    "/admin/services": {"groups": ("admin-services",), "states": STATES, "note": "Retained legacy Services surface."},
    "/admin/settings": {"groups": ("admin-settings",), "states": STATES, "note": "Retained legacy Settings surface."},
    "/admin/status": {"groups": ("admin-status",), "states": STATES, "note": "Retained legacy Status surface."},
    "/admin/status/p1v-fixture": {
        "groups": ("admin-status-p1v-fixture",),
        "states": ("n-a",),
        "note": "Static fixture route; data state is N/A.",
    },
    "/admin/users": {"groups": ("admin-users",), "states": STATES, "note": "Retained legacy Users surface."},
    "/apps": {"groups": ("apps",), "states": STATES, "note": "Canonical member Apps & keys route."},
    "/design": {"groups": ("design",), "states": ("n-a",), "note": "Static design-system route; data state is N/A."},
    "/engines": {"groups": ("status-engines-services",), "states": STATES, "note": "Canonical Engines matrix."},
    "/gallery": {"groups": ("gallery",), "states": ("n-a",), "note": "Static alias; data state is N/A."},
    "/home": {"groups": ("home",), "states": STATES, "note": "Canonical Home route."},
    "/invite": {"groups": ("people-invite",), "states": STATES, "note": "Invite acceptance public route."},
    "/library": {"groups": ("library",), "states": ("grid", "empty", "error"), "note": "Library uses grid as seeded success."},
    "/models": {"groups": ("models",), "states": STATES, "note": "Canonical Models route."},
    "/people": {
        "groups": ("people-people", "people-invites", "people-plans"),
        "states": STATES,
        "note": "People route has separate tab matrices.",
    },
    "/providers": {"groups": ("providers",), "states": STATES, "note": "Canonical Providers route."},
    "/services": {
        "groups": ("status-engines-services",),
        "states": STATES,
        "note": "Compatibility alias covered by the Engines/Services matrix.",
    },
    "/settings": {"groups": ("settings",), "states": STATES, "note": "Canonical Settings route."},
    "/setup": {"groups": ("public-setup",), "states": STATES, "note": "Public setup route matrix."},
    "/showcase": {"groups": ("public-showcase",), "states": STATES, "note": "Public showcase route matrix."},
    "/sign-in": {"groups": ("public-sign-in",), "states": STATES, "note": "Public sign-in route matrix."},
    "/status": {"groups": ("status",), "states": STATES, "note": "Canonical Status route."},
    "/try": {"groups": ("try",), "states": ("empty", "error", "result"), "note": "Try uses result as seeded success."},
    "/usage": {"groups": ("usage",), "states": ("empty", "error", "lane"), "note": "Usage uses lane as seeded success."},
    "/docs": {"groups": ("docs",), "states": ("n-a",), "note": "Static API reference; data state is N/A."},
    "/redoc": {"groups": ("redoc",), "states": ("n-a",), "note": "Static API reference; data state is N/A."},
}


def classify(path: Path) -> tuple[str, str]:
    stem = path.with_suffix("").as_posix()
    name = stem.split("/")[-1]
    if stem.startswith("home/"):
        return "home", stem.split("/", 1)[1]
    for state in ("seeded", "empty", "error", "first-run", "n-a"):
        token = f"-{state}-"
        if token in name:
            return name.split(token, 1)[0], name
    parts = name.rsplit("-", 2)
    if len(parts) == 3 and parts[-2] in {"light", "dark"}:
        return parts[0], name
    return "misc", name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--since", type=float, default=0, help="Minimum capture mtime, Unix seconds")
    args = parser.parse_args()
    dest_root = args.destination
    if not SOURCE.exists():
        raise SystemExit(f"Missing screenshot source: {SOURCE}")
    dest_root.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[str]] = defaultdict(list)
    for src in sorted(SOURCE.rglob("*.png")):
        if src.stat().st_mtime < args.since:
            continue
        rel = src.relative_to(SOURCE)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        group, _ = classify(rel)
        groups[group].append(rel.as_posix())

    diagnostics = []
    for src in sorted(SOURCE.rglob("*.a11y.json")):
        if src.stat().st_mtime < args.since:
            continue
        rel = src.relative_to(SOURCE)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        diagnostics.append(rel.as_posix())

    captures = {rel for files in groups.values() for rel in files}

    def has_capture(group: str, state: str, theme: str, width: str) -> bool:
        candidates = captures
        if group == "home":
            return f"home/{state}-{theme}-{width}.png" in candidates
        if state == "n-a":
            return f"{group}-{theme}-{width}.png" in candidates or f"{group}-n-a-{theme}-{width}.png" in candidates
        return f"{group}-{state}-{theme}-{width}.png" in candidates

    route_rows = []
    missing = []
    for route, spec in sorted(ROUTE_COVERAGE.items()):
        route_missing = []
        for group in spec["groups"]:
            for state in spec["states"]:
                for theme in THEMES:
                    for width in WIDTHS:
                        if not has_capture(group, state, theme, width):
                            route_missing.append(f"{group}:{state}:{theme}:{width}")
        status = "complete" if not route_missing else "missing"
        route_rows.append((route, status, ", ".join(spec["groups"]), ", ".join(spec["states"]), spec["note"]))
        missing.extend((route, item) for item in route_missing)

    lines = [
        "# Integrated UI Screenshot Index",
        "",
        f"Source: `{SOURCE}`",
        f"Copied to: `{dest_root}`",
        f"Capture cutoff: `{args.since}` (Unix seconds). Earlier files are excluded.",
        "",
        "## State Notes",
        "",
        "- Seeded/empty/error screenshots use the route's own e2e fixture when present.",
        "- Static routes `design`, `gallery`, `docs`, and `redoc` have data state N/A.",
        "- Admin compatibility and fixture routes are listed separately; fixture routes have data state N/A.",
        "- `/` is covered by the Home matrix. `/services` is covered by the Engines/Services matrix.",
        "- Legacy admin captures retain existing contrast defects. Their adjacent `.a11y.json` files record the findings; they do not represent a passed accessibility check.",
        "",
        "## Exported Route Coverage",
        "",
        "| Route | Status | Screenshot group(s) | State set | Note |",
        "|---|---:|---|---|---|",
    ]
    for route, status, group, state, note in route_rows:
        lines.append(f"| `{route}` | {status} | `{group}` | `{state}` | {note} |")
    lines.extend(["", "## Missing Expected Captures", ""])
    if missing:
        for route, item in missing:
            lines.append(f"- `{route}`: `{item}`")
    else:
        lines.append("- None detected by filename coverage rules.")
    lines.extend(["", "## Files", ""])
    for group in sorted(groups):
        note = " (data state N/A)" if group in STATIC_NA else ""
        lines.append(f"### {group}{note}")
        lines.append("")
        for rel in sorted(groups[group]):
            lines.append(f"- [{rel}]({rel})")
        lines.append("")

    lines.extend(["## Legacy accessibility evidence", ""])
    for rel in diagnostics:
        lines.append(f"- [{rel}]({rel})")

    (dest_root / "INDEX.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Copied {sum(len(v) for v in groups.values())} screenshots to {dest_root}")
    print(f"Wrote {dest_root / 'INDEX.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
