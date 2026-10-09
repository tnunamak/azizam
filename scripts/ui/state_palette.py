#!/usr/bin/env python3
"""Check the state palette in web/src/styles/tokens.css (DESIGN.md, "State palette checks").

Usage: scripts/ui/state_palette.py [--fail]

For each theme it prints the WCAG 2 contrast of every state's text colour on the
card and the page, and of every state's fill on the card; then the CIELAB
distance (Delta E 1976) between pairs of fills after simulating protanopia,
deuteranopia and tritanopia at full severity (Machado, Oliveira and Fernandes
2009). With --fail it exits 1 when a text colour is under 4.5:1, a fill other
than the no-check cell is under 3:1, or Degraded and Down are closer than
Delta E 20 under any simulation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TOKENS = Path(__file__).resolve().parents[2] / "web" / "src" / "styles" / "tokens.css"

STATES = {
    # state: (text token, fill token)
    "ready": ("success", "success-fill"),
    "busy": ("info", "info-fill"),
    "degraded": ("warning", "warning-fill"),
    "down": ("danger", "danger-fill"),
    "blocked": ("blocked", "blocked-fill"),
    "unknown": ("subtle-foreground", "nocheck-fill"),
}
PAIRS = [
    ("degraded", "down"),
    ("ready", "degraded"),
    ("ready", "down"),
    ("busy", "blocked"),
    ("degraded", "blocked"),
]
MACHADO = {
    "normal": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}


def blocks(css: str) -> dict[str, dict[str, str]]:
    """Custom properties declared in the first :root block and the .dark block."""
    themes: dict[str, dict[str, str]] = {}
    for name, selector in (("light", r":root"), ("dark", r"\.dark")):
        match = re.search(selector + r"\s*\{(.*?)\n\}", css, re.S)
        if not match:
            raise SystemExit(f"no {selector} block in {TOKENS}")
        themes[name] = dict(re.findall(r"--([\w-]+):\s*([^;]+);", match.group(1)))
    # Dark inherits what it does not override.
    themes["dark"] = {**themes["light"], **themes["dark"]}
    return themes


def resolve(values: dict[str, str], name: str) -> str:
    value = values[name].strip()
    while value.startswith("var("):
        value = values[value[4:-1].strip().removeprefix("--")].strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise SystemExit(f"--{name} is {value!r}; expected a #rrggbb colour")
    return value


def rgb(hex_colour: str) -> list[float]:
    return [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]


def linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(colour: list[float]) -> float:
    r, g, b = (linear(c) for c in colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(rgb(a)), luminance(rgb(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def lab(hex_colour: str, kind: str) -> tuple[float, float, float]:
    lin = [linear(c) for c in rgb(hex_colour)]
    m = MACHADO[kind]
    r, g, b = (max(0.0, min(1.0, sum(m[i][j] * lin[j] for j in range(3)))) for i in range(3))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.9505
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.089

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def delta_e(a: str, b: str, kind: str) -> float:
    return sum((p - q) ** 2 for p, q in zip(lab(a, kind), lab(b, kind))) ** 0.5


def main(argv: list[str]) -> int:
    fail = "--fail" in argv
    problems: list[str] = []
    for theme, values in blocks(TOKENS.read_text()).items():
        card, page = resolve(values, "card"), resolve(values, "background")
        print(f"== {theme} (card {card}, page {page})")
        print("state     text     card  page   fill     card")
        fills = {}
        for state, (text_token, fill_token) in STATES.items():
            text, fill = resolve(values, text_token), resolve(values, fill_token)
            fills[state] = fill
            on_card, on_page, fill_ratio = contrast(text, card), contrast(text, page), contrast(fill, card)
            print(f"{state:9} {text}  {on_card:4.1f}  {on_page:4.1f}   {fill}  {fill_ratio:4.1f}")
            if min(on_card, on_page) < 4.5:
                problems.append(f"{theme} {state} text {min(on_card, on_page):.1f}:1 < 4.5:1")
            if state != "unknown" and fill_ratio < 3:
                problems.append(f"{theme} {state} fill {fill_ratio:.1f}:1 < 3:1")
        print("Delta E   " + "  ".join(f"{kind:>6}" for kind in MACHADO))
        for a, b in PAIRS:
            distances = [delta_e(fills[a], fills[b], kind) for kind in MACHADO]
            print(f"{a[:3]}-{b[:3]}   " + "  ".join(f"{d:6.0f}" for d in distances))
            if (a, b) == ("degraded", "down") and min(distances) < 20:
                problems.append(f"{theme} degraded and down fills are Delta E {min(distances):.0f} apart")
    for problem in problems:
        print(f"FAIL: {problem}")
    return 1 if fail and problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
