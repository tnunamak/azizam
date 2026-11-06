#!/usr/bin/env python3
"""Gates 1 + 2 for the layered dual-grid tileset.

Gate 1 (per-tile alpha pattern): each of the 16 tiles in each non-base layer
must have alpha matching its S-V2 corner pattern — filled corners opaque,
empty corners transparent.

Gate 2 (adjacency contact sheet): build a side-by-side contact sheet of
adjacent-tile pairs (horizontal and vertical) for each layer. Lets a human
inspect whether seams look continuous.

Usage:
  validate_tileset.py --tileset ~/.tmp/dualgrid_ocean_v2 --out ~/.tmp/edge-stitch-review
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np
from PIL import Image

QUADRANTS = ["TL", "TR", "BL", "BR"]
def quad_slice(quadrant: str, size: int):
    h = size // 2
    return {
        "TL": (slice(0, h), slice(0, h)),
        "TR": (slice(0, h), slice(h, size)),
        "BL": (slice(h, size), slice(0, h)),
        "BR": (slice(h, size), slice(h, size)),
    }[quadrant]


def gate1_check_tile(path: Path, bits: int, size: int) -> dict:
    """Returns dict of {quadrant: (expected_filled, center_alpha, ok)}."""
    img = np.array(Image.open(path).convert("RGBA"))
    h = size // 2
    out = {}
    for q in QUADRANTS:
        rs, cs = quad_slice(q, size)
        is_filled = (bits & (1 << QUADRANTS.index(q))) != 0
        # Center 50% of the quadrant — avoid the transition band
        center_rs = slice(rs.start + h//4, rs.stop - h//4)
        center_cs = slice(cs.start + h//4, cs.stop - h//4)
        center_alpha = float(img[center_rs, center_cs, 3].mean())
        if is_filled:
            ok = center_alpha >= 200
        else:
            ok = center_alpha <= 60
        out[q] = {"expected_filled": is_filled, "center_alpha": center_alpha, "ok": ok}
    return out


def gate1_report(tileset_dir: Path, manifest: dict) -> dict:
    """Run gate 1 on every tile in every non-base layer."""
    results = {}
    size = manifest["size"]
    for layer in manifest["layers"]:
        if layer.get("is_base"): continue
        layer_id = layer["id"]
        results[layer_id] = []
        for bits in range(16):
            tile_path = tileset_dir / layer_id / f"tile_{bits:02d}.png"
            if not tile_path.exists():
                results[layer_id].append({"bits": bits, "error": "missing"})
                continue
            quad_results = gate1_check_tile(tile_path, bits, size)
            all_ok = all(q["ok"] for q in quad_results.values())
            results[layer_id].append({"bits": bits, "ok": all_ok, "quadrants": quad_results})
    return results


def gate2_contact_sheet(tileset_dir: Path, manifest: dict, out_path: Path) -> None:
    """Build a contact sheet showing key tile adjacencies.

    For each non-base layer, show a series of side-by-side tile pairs:
      tile_03 (top edge) | tile_15 (full)    — vertical match: top of bottom should match bottom of top
      tile_15 | tile_15                      — repetition: should be byte-identical seam
      tile_15 | tile_12 (bottom edge)        — vertical match
      tile_03 | tile_03                      — horizontal repeat
      tile_07 (empty BR) | tile_15           — checks where the empty-BR meets the next tile
    """
    layers = [l for l in manifest["layers"] if not l.get("is_base")]
    # Display each tile at 256 (reduce file size)
    DISP = 256
    pairs = [
        (15, 15, "horizontal: same 15 | 15"),
        (3, 15, "vertical: top-edge above full"),
        (12, 15, "vertical: bottom-edge above full"),
        (5, 10, "horizontal: left-edge | right-edge"),
        (1, 8, "horizontal: TL-only | BR-only"),
        (6, 9, "vertical: diag-TR+BL above diag-TL+BR"),
        (14, 1, "horizontal: empty-TL | TL-only"),
        (15, 0, "vertical: full above empty"),
    ]
    cell_w = DISP * 2 + 20
    cell_h = DISP * 2 + 30
    grid_w = cell_w
    grid_h = cell_h * (len(layers) * len(pairs)) + 30

    canvas = Image.new("RGBA", (grid_w, grid_h), (40, 40, 40, 255))
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(canvas)

    # Try to load a font
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except:
        font = ImageFont.load_default()

    y = 30
    for layer in layers:
        for (a, b, lbl) in pairs:
            la = tileset_dir / layer["id"] / f"tile_{a:02d}.png"
            lb = tileset_dir / layer["id"] / f"tile_{b:02d}.png"
            if not la.exists() or not lb.exists():
                continue
            ta = Image.open(la).convert("RGBA").resize((DISP, DISP), Image.LANCZOS)
            tb = Image.open(lb).convert("RGBA").resize((DISP, DISP), Image.LANCZOS)
            # Composite both onto a checkerboard for visibility of transparency
            checker = Image.new("RGBA", (DISP, DISP))
            cd = ImageDraw.Draw(checker)
            for cy in range(0, DISP, 16):
                for cx in range(0, DISP, 16):
                    color = (60, 60, 60, 255) if (cy//16 + cx//16) % 2 == 0 else (80, 80, 80, 255)
                    cd.rectangle([cx, cy, cx+15, cy+15], fill=color)
            a_composed = Image.alpha_composite(checker, ta)
            b_composed = Image.alpha_composite(checker, tb)

            vertical = lbl.startswith("vertical:")
            canvas.paste(a_composed, (0, y))
            canvas.paste(b_composed, (0, y + DISP) if vertical else (DISP, y))
            draw.text((0, y + (DISP * 2 if vertical else DISP) + 4),
                      f"[{layer['id']}] tile_{a:02d} | tile_{b:02d}  ·  {lbl}",
                      fill=(220, 220, 220), font=font)
            y += cell_h
    canvas.save(out_path)
    print(f"Gate 2 contact sheet saved: {out_path}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tileset", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path,
                   help="Output directory for the contact sheet + report")
    args = p.parse_args()

    manifest = json.loads((args.tileset / "manifest.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)

    # Gate 1
    print("Running Gate 1 (per-tile alpha pattern)...")
    g1 = gate1_report(args.tileset, manifest)
    fails = []
    for layer_id, tiles in g1.items():
        for r in tiles:
            if "error" in r:
                fails.append((layer_id, r["bits"], r["error"]))
            elif not r["ok"]:
                fails.append((layer_id, r["bits"], r["quadrants"]))
    if fails:
        print(f"  GATE 1 FAIL ({len(fails)} issues):")
        for f in fails[:10]:
            print(f"    {f}")
    else:
        print(f"  GATE 1 PASS: all 16 × {len([l for l in manifest['layers'] if not l.get('is_base')])} non-base tiles correct")

    # Gate 2
    print("\nRunning Gate 2 (adjacency contact sheet)...")
    gate2_contact_sheet(args.tileset, manifest, args.out / "validation_contact_sheet.png")

    # Write the gate-1 report as JSON
    (args.out / "validation_gate1.json").write_text(json.dumps(g1, indent=2, default=str))
    print(f"  Gate 1 report: {args.out / 'validation_gate1.json'}")

    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
