#!/usr/bin/env python3
"""Compose prompt-memory-capture.mjs shots into one JPEG per screen.

Usage: prompt-memory-sheet.py <shots-dir> <out-dir>

Rows are the screen's states; columns are 1440 light, 1440 dark, 390 light
and 390 dark, at half size. Needs Pillow (uv run --with pillow).
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SHEETS = {
    "try": ["try-kept", "try-forget", "try-household-off"],
    "library": [
        "library-maker",
        "library-maker-not-saved",
        "library-owner-sees-member",
        "library-owner-own",
    ],
    "your-settings": [
        "account-member",
        "account-capped",
        "account-cleanup",
        "account-household-off",
    ],
    "household-settings": ["settings-owner"],
    "key-reveal": ["apps-key"],
}
COLUMNS = [("1440", "light"), ("1440", "dark"), ("390", "light"), ("390", "dark")]
SCALE, GAP, LABEL = 0.5, 12, 26


def main() -> None:
    shots, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for sheet, states in SHEETS.items():
        rows = []
        for state in states:
            images = []
            for width, theme in COLUMNS:
                image = Image.open(shots / f"{state}-{width}-{theme}.png").convert("RGB")
                images.append(image.resize((int(image.width * SCALE), int(image.height * SCALE))))
            rows.append((state, images))
        width = GAP + sum(i.width + GAP for i in rows[0][1])
        height = GAP + sum(LABEL + max(i.height for i in imgs) + GAP for _, imgs in rows)
        canvas = Image.new("RGB", (width, height), (246, 247, 248))
        draw = ImageDraw.Draw(canvas)
        y = GAP
        for state, images in rows:
            draw.text((GAP, y + 6), state, fill=(40, 40, 40))
            y += LABEL
            x = GAP
            for image in images:
                canvas.paste(image, (x, y))
                x += image.width + GAP
            y += max(i.height for i in images) + GAP
        canvas.save(out / f"{sheet}.jpg", quality=78, optimize=True)
        print(out / f"{sheet}.jpg")


if __name__ == "__main__":
    main()
