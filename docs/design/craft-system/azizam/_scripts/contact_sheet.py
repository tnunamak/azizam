#!/usr/bin/env python3
"""Compose before/after captures from contact.mjs into one JPEG per surface.

Usage: contact_sheet.py <before-dir> <after-dir> <out-dir> [surface,surface]

Each sheet has two rows (before, after) and four columns (1440 light, 1440 dark,
390 light, 390 dark). Desktop shots are scaled to 50 %, phone shots to 50 %.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SURFACES = ["home", "status", "library", "try", "people", "usage", "workflows", "settings"]
COLUMNS = [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")]
SCALE = 0.5
GAP = 12
LABEL = 22


def main() -> None:
    before, after, out = (Path(a) for a in sys.argv[1:4])
    surfaces = sys.argv[4].split(",") if len(sys.argv) > 4 else SURFACES
    out.mkdir(parents=True, exist_ok=True)
    for surface in surfaces:
        rows = []
        for name, folder in (("before", before), ("after", after)):
            shots = []
            for width, theme in COLUMNS:
                path = folder / f"{surface}-{width}-{theme}.jpg"
                image = Image.open(path).convert("RGB")
                shots.append(image.resize((int(image.width * SCALE), int(image.height * SCALE)), Image.LANCZOS))
            rows.append((name, shots))
        width = sum(s.width for s in rows[0][1]) + GAP * (len(COLUMNS) + 1)
        row_height = max(s.height for _, shots in rows for s in shots)
        height = (row_height + LABEL + GAP) * len(rows) + GAP
        sheet = Image.new("RGB", (width, height), (250, 250, 250))
        draw = ImageDraw.Draw(sheet)
        y = GAP
        for name, shots in rows:
            x = GAP
            for (w, theme), shot in zip(COLUMNS, shots):
                draw.text((x, y + 4), f"{surface} {name} · {w} · {theme}", fill=(40, 40, 40))
                sheet.paste(shot, (x, y + LABEL))
                x += shot.width + GAP
            y += row_height + LABEL + GAP
        sheet.save(out / f"{surface}.jpg", "JPEG", quality=72, optimize=True, progressive=True)
        print(out / f"{surface}.jpg")


if __name__ == "__main__":
    main()
