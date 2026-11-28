"""Draw the Azizam curl and write its two SVG drawings.

The curl is one lock of a baby's red hair: a spiral of circular quarter
turns (each radius 1.42 times the last) that runs out into a falling tail.
The stroke holds a round tip for the inner fifth of the length, then
thickens smoothly toward the tail. The outline is sampled, simplified to
within 0.02 units and fitted into a 48 box, centred halfway between its
bounding-box centre and its area centroid (so the heavy head does not ride
high).

    python3 docs/design/brand/curl.py          # writes the SVGs beside this file
    python3 docs/design/brand/curl.py --path   # also prints the master path for BrandMark

Then run `node web/scripts/brand-icons.mjs` to render the icon set.
Rules: docs/design/DESIGN.md, "Brand".
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOX = 48

# Widths are in final 48-box units.
MASTER = dict(r0=8.5, growth=1.42, tail=((1.0, -40), (1.6, 24)), tip=4.8, root=8.8, margin=2.0)
# The 16 and 32 px favicons: firmer ends so the tip keeps a pixel and the stem is about 3 px at 16.
SMALL = dict(r0=8.5, growth=1.42, tail=((1.0, -40), (1.6, 24)), tip=6.0, root=10.0, margin=1.0)
TILT = -18.0  # degrees; leans the lock so it fills the square
HOLD = 0.22   # share of the length, from the tip, drawn at the tip width


def centreline(r0: float, growth: float, tail) -> list[tuple[float, float, float]]:
    """Samples (x, y, heading) from the inner tip outward; screen axes, y down."""
    x = y = 0.0
    heading = math.pi / 2
    samples = [(x, y, heading)]

    def arc(radius: float, turn_deg: float) -> None:
        nonlocal x, y, heading
        side = 1 if turn_deg > 0 else -1
        cx = x + radius * math.cos(heading + side * math.pi / 2)
        cy = y + radius * math.sin(heading + side * math.pi / 2)
        start = math.atan2(y - cy, x - cx)
        turn = math.radians(turn_deg)
        steps = max(4, int(abs(turn_deg) / 3))
        for i in range(1, steps + 1):
            a = start + turn * i / steps
            samples.append((cx + radius * math.cos(a), cy + radius * math.sin(a), heading + turn * i / steps))
        x, y, heading = samples[-1][0], samples[-1][1], heading + turn

    radius = r0
    for _ in range(4):  # one full turn of the curl
        arc(radius, -90)
        radius *= growth
    for factor, turn in tail:
        arc(radius * factor, turn)
    tilt = math.radians(TILT)
    c, s = math.cos(tilt), math.sin(tilt)
    return [(px * c - py * s, px * s + py * c, h + tilt) for px, py, h in samples]


def outline(line, tip: float, root: float) -> list[tuple[float, float]]:
    """Closed outline of the variable-width stroke with round ends (raw units)."""
    n = len(line)

    def width(t: float) -> float:
        u = min(1.0, max(0.0, (t - HOLD) / (0.92 - HOLD)))
        return tip + (root - tip) * u * u * (3 - 2 * u)

    left, right = [], []
    for i, (x, y, h) in enumerate(line):
        half = width(i / (n - 1)) / 2
        left.append((x - math.sin(h) * half, y + math.cos(h) * half))
        right.append((x + math.sin(h) * half, y - math.cos(h) * half))

    def cap(i: int, at_start: bool) -> list[tuple[float, float]]:
        x, y, h = line[i]
        half = width(i / (n - 1)) / 2
        base = h - math.pi / 2 if at_start else h + math.pi / 2
        return [(x + half * math.cos(base - math.pi * k / 24), y + half * math.sin(base - math.pi * k / 24))
                for k in range(1, 24)]

    return left + cap(n - 1, False) + right[::-1] + cap(0, True)


def place(poly, margin: float):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    scale = (BOX - 2 * margin) / max(max(xs) - min(xs), max(ys) - min(ys))
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    cx, cy = cx / (3 * area), cy / (3 * area)
    tx = ((min(xs) + max(xs)) / 2 + cx) / 2
    ty = ((min(ys) + max(ys)) / 2 + cy) / 2
    dx = min(max(BOX / 2 - tx * scale, margin - min(xs) * scale), BOX - margin - max(xs) * scale)
    dy = min(max(BOX / 2 - ty * scale, margin - min(ys) * scale), BOX - margin - max(ys) * scale)
    return [(x * scale + dx, y * scale + dy) for x, y in poly], scale


def drawing(r0, growth, tail, tip, root, margin):
    line = centreline(r0, growth, tail)
    scale = 1.0
    for _ in range(4):  # widths are final units: converge on the fitted scale
        poly, scale = place(outline(line, tip / scale, root / scale), margin)
    return poly


def simplify(points, tolerance=0.02):
    """Ramer-Douglas-Peucker on a closed outline."""
    def rdp(pts):
        if len(pts) < 3:
            return pts
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy) or 1e-9
        far, index = 0.0, 0
        for i, (x, y) in enumerate(pts[1:-1], 1):
            d = abs(dy * (x - x0) - dx * (y - y0)) / length
            if d > far:
                far, index = d, i
        if far <= tolerance:
            return [pts[0], pts[-1]]
        return rdp(pts[: index + 1])[:-1] + rdp(pts[index:])
    half = len(points) // 2
    return rdp(points[: half + 1])[:-1] + rdp(points[half:] + points[:1])[:-1]


def path_data(points) -> str:
    def num(v: float) -> str:
        text = f"{v:.2f}".rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text
    return "M" + "L".join(f"{num(x)} {num(y)}" for x, y in points) + "Z"


def main() -> None:
    master = path_data(simplify(drawing(**MASTER)))
    small = path_data(simplify(drawing(**SMALL)))
    for name, d in (("azizam-curl.svg", master), ("azizam-curl-small.svg", small)):
        (HERE / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {BOX} {BOX}" fill="currentColor">'
            f'<path d="{d}"/></svg>\n'
        )
    if "--path" in sys.argv:
        print(master)


if __name__ == "__main__":
    main()
