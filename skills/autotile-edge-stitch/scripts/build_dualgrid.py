#!/usr/bin/env python3
"""Build a dual-grid 16-tile autotile set with byte-zero seams via
quarter-tile precomposition (Boris Eyrich's recommended technique for
AI-generated tiles).

Generates a small palette of canonical sub-tiles via diffusion, then
composites the 16 dual-grid configurations by stamping sub-tiles at
each of the 4 quadrant positions of each tile. Seams are byte-zero
by construction because every "filled" quadrant in every tile is
literally the same pixels.

The sub-tile palette:
  - Q_FILLED: a quadrant-sized (size/2 × size/2) tile of filled material
  - Q_EMPTY:  a quadrant-sized tile of empty material
  - Q_NW, Q_NE, Q_SW, Q_SE: corner-transition quadrants where the named
    corner is filled and the other corner is empty, with a smooth
    diagonal transition between them.

Dual-grid 16-tile rule:
  Each of the 16 tiles is identified by a 4-bit code (one bit per
  corner: TL, TR, BL, BR). Bit set = that corner's underlying terrain
  cell is filled.

  For each of the 4 quadrants of a tile, look up which transition
  sub-tile (or pure fill/empty) belongs there, based on whether the
  4 cells visible from that quadrant are filled.

Output:
  <out-dir>/sub_filled.png, sub_empty.png, sub_nw.png, sub_ne.png, sub_sw.png, sub_se.png
  <out-dir>/tile_00.png .. tile_15.png (the 16 dual-grid tiles)
  <out-dir>/spritesheet.png  (4x4 atlas, Tiled/Godot-compatible)
  <out-dir>/preview.png      (random terrain map rendered with the tiles)
  <out-dir>/manifest.json    (metadata: tile size, palette, rule)
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, uuid
from pathlib import Path
import numpy as np
from PIL import Image
from runtime import GatewayImageClient, add_comfy_args, add_gateway_args, comfy_cli_args, require

EDGE_STITCH = Path(__file__).parent / "edge_stitch.py"

def gen_seamless(client: GatewayImageClient, prompt: str, size: int, out_path: Path, model: str | None) -> None:
    """Generate a texture through the public OpenAI Images API."""
    print(f"  generating seamless {out_path.name} ({size}x{size})...")
    out_path.write_bytes(client.generate(model=model, prompt=prompt, size=size))


def gen_corner_transition(
    *, filled_img: Path, empty_img: Path, filled_corner: str,
    prompt: str, size: int, out_path: Path, seed: int, comfy_input: Path,
    edge_config: list[str]) -> None:
    """Generate a corner-transition sub-tile where one corner is filled
    material and the opposite corner is empty material. Uses edge_stitch.py:
    canvas has filled material in the named corner (byte-exact) and empty
    material in the opposite corner (byte-exact); the model inpaints the
    diagonal transition.

    filled_corner: 'NW', 'NE', 'SW', or 'SE' (which corner is filled).
    """
    filled = np.array(Image.open(filled_img).convert("RGB"))
    empty = np.array(Image.open(empty_img).convert("RGB"))
    if filled.shape != (size, size, 3) or empty.shape != (size, size, 3):
        sys.exit(f"sub-tile size mismatch: filled={filled.shape} empty={empty.shape} expected ({size},{size},3)")

    canvas = np.full((size, size, 3), 128, dtype=np.uint8)
    mask = np.full((size, size), 255, dtype=np.uint8)  # default: regenerate (white)

    # Half-size corner regions occupy size/2 × size/2 in each corner.
    h = size // 2

    # Stamp filled in its corner; stamp empty in the diagonally-opposite corner.
    # Leave the other two corners (the "anti-diagonal") as masked region where
    # the model produces the transition.
    if filled_corner == "NW":
        canvas[:h, :h] = filled[:h, :h]; mask[:h, :h] = 0       # preserve
        canvas[h:, h:] = empty[h:, h:]; mask[h:, h:] = 0
    elif filled_corner == "NE":
        canvas[:h, h:] = filled[:h, h:]; mask[:h, h:] = 0
        canvas[h:, :h] = empty[h:, :h]; mask[h:, :h] = 0
    elif filled_corner == "SW":
        canvas[h:, :h] = filled[h:, :h]; mask[h:, :h] = 0
        canvas[:h, h:] = empty[:h, h:]; mask[:h, h:] = 0
    elif filled_corner == "SE":
        canvas[h:, h:] = filled[h:, h:]; mask[h:, h:] = 0
        canvas[:h, :h] = empty[:h, :h]; mask[:h, :h] = 0
    else:
        sys.exit(f"unknown corner '{filled_corner}'")

    work = comfy_input / f"_dualgrid_corner_{uuid.uuid4().hex[:8]}"
    canvas_p = work.with_suffix(".canvas.png")
    mask_p = work.with_suffix(".mask.png")
    Image.fromarray(canvas).save(canvas_p)
    Image.fromarray(mask).save(mask_p)

    print(f"  generating corner-transition {filled_corner} -> {out_path.name}...")
    r = subprocess.run([str(EDGE_STITCH),
                        "--reference", str(canvas_p),
                        "--mask", str(mask_p),
                        "--prompt", prompt,
                        "--output", str(out_path),
                        "--seed", str(seed),
                        "--steps", "20", *edge_config])
    canvas_p.unlink(missing_ok=True); mask_p.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit(f"corner-transition gen failed for {filled_corner}")


# --- Dual-grid 16-tile rule ---
#
# Each tile covers 4 underlying terrain cells in a 2x2 layout. The 4 bits
# of the tile's index identify which cells are filled:
#   bit 0 = TL (top-left)
#   bit 1 = TR (top-right)
#   bit 2 = BL (bottom-left)
#   bit 3 = BR (bottom-right)
#
# Each tile's 4 quadrants depict the 4 cells. A quadrant's content depends
# on whether its cell is filled AND on the diagonal neighbor's state — i.e.,
# the quadrant might be pure-filled, pure-empty, or a transition.
#
# In quarter-tile precomposition with our 6-sub-tile palette:
#   - If the quadrant's cell is filled and its diagonal cell is filled:
#     use Q_FILLED.
#   - If the quadrant's cell is empty and its diagonal cell is empty:
#     use Q_EMPTY.
#   - If the quadrant's cell is filled and the diagonal is empty:
#     use the corner-transition where the quadrant's corner is filled.
#   - If the quadrant's cell is empty and the diagonal is filled:
#     use the corner-transition where the diagonal's corner is filled
#     (i.e., the empty side faces the quadrant).
#
# Quadrant positions in a tile (in row, col coordinates of the 2x2):
#   TL: (0, 0), TR: (0, 1), BL: (1, 0), BR: (1, 1)
#
# Each quadrant looks across its diagonal:
#   TL <-> BR
#   TR <-> BL


def composite_tile(palette: dict[str, np.ndarray], filled_bits: int, size: int) -> np.ndarray:
    """Composite one of the 16 dual-grid tiles by stamping the appropriate
    sub-tile at each of the 4 quadrants.

    filled_bits: 4-bit code, bit0=TL, bit1=TR, bit2=BL, bit3=BR.
    Each sub-tile in `palette` is half-size (size/2 × size/2 × 3).
    Output: full-size tile (size × size × 3).
    """
    h = size // 2
    tile = np.empty((size, size, 3), dtype=np.uint8)

    cells = {
        "TL": bool(filled_bits & 0b0001),
        "TR": bool(filled_bits & 0b0010),
        "BL": bool(filled_bits & 0b0100),
        "BR": bool(filled_bits & 0b1000),
    }
    diag = {"TL": "BR", "TR": "BL", "BL": "TR", "BR": "TL"}

    for quadrant, filled in cells.items():
        diag_quad = diag[quadrant]
        diag_filled = cells[diag_quad]

        if filled and diag_filled:
            sub_key, sub_corner = "filled", quadrant
        elif (not filled) and (not diag_filled):
            sub_key, sub_corner = "empty", quadrant
        elif filled and not diag_filled:
            # this quadrant is filled, diagonal is empty
            # use the transition where THIS quadrant's corner is filled
            sub_key = "trans"
            sub_corner = quadrant
        else:
            # this quadrant empty, diagonal filled — use transition where
            # the DIAGONAL's corner is filled (so this quadrant sees empty)
            sub_key = "trans"
            sub_corner = diag_quad

        # Look up the sub-tile content for this quadrant
        if sub_key == "filled":
            sub = palette["filled"]
        elif sub_key == "empty":
            sub = palette["empty"]
        else:
            sub = palette[f"trans_{sub_corner}"]

        # The sub-tile is full-size (size × size). We extract just the
        # quadrant of the sub-tile that corresponds to our quadrant position.
        if quadrant == "TL":
            tile[:h, :h] = sub[:h, :h]
        elif quadrant == "TR":
            tile[:h, h:] = sub[:h, h:]
        elif quadrant == "BL":
            tile[h:, :h] = sub[h:, :h]
        elif quadrant == "BR":
            tile[h:, h:] = sub[h:, h:]
    return tile


def make_spritesheet(tiles: list[np.ndarray], size: int) -> Image.Image:
    """4x4 grid of the 16 dual-grid tiles. Indexed by 4-bit code."""
    sheet = Image.new("RGB", (size * 4, size * 4))
    for i, t in enumerate(tiles):
        row = i // 4
        col = i % 4
        sheet.paste(Image.fromarray(t), (col * size, row * size))
    return sheet


def render_preview(palette: dict[str, np.ndarray], size: int, *, w: int = 12, h: int = 8, seed: int = 1) -> Image.Image:
    """Render a w×h tile-map of random terrain using the tileset, sampling
    each tile from the underlying terrain cells (a (w+1)×(h+1) bool grid).
    """
    rng = np.random.default_rng(seed)
    # terrain cell grid (corners of the tile grid)
    terrain = rng.random((h + 1, w + 1)) > 0.5
    out = Image.new("RGB", (w * size, h * size))
    for r in range(h):
        for c in range(w):
            tl = bool(terrain[r, c])
            tr = bool(terrain[r, c + 1])
            bl = bool(terrain[r + 1, c])
            br = bool(terrain[r + 1, c + 1])
            bits = (1 if tl else 0) | (2 if tr else 0) | (4 if bl else 0) | (8 if br else 0)
            tile_arr = composite_tile(palette, bits, size)
            out.paste(Image.fromarray(tile_arr), (c * size, r * size))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--filled-prompt", required=True,
                   help="seamless prompt for the FILLED material (e.g., stone wall)")
    p.add_argument("--empty-prompt", required=True,
                   help="seamless prompt for the EMPTY material (e.g., grass)")
    p.add_argument("--size", type=int, default=512, help="full tile size; quadrants are size/2")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--texture-model", default=None,
                   help="operator-configured model ID (env: AUTOTILE_TEXTURE_MODEL)")
    p.add_argument("--reuse-existing", action="store_true",
                   help="reuse sub-tile PNGs in out-dir if they exist (skip diffusion)")
    add_gateway_args(p)
    add_comfy_args(p)
    args = p.parse_args()

    texture_model = args.texture_model or os.environ.get("AUTOTILE_TEXTURE_MODEL")
    required_paths = [
        args.out_dir / "sub_filled.png", args.out_dir / "sub_empty.png",
        *(args.out_dir / f"sub_trans_{corner}.png" for corner in ("NW", "NE", "SW", "SE")),
    ]
    needs_generation = not args.reuse_existing or not all(path.exists() for path in required_paths)
    if not texture_model and needs_generation:
        p.error("--texture-model or AUTOTILE_TEXTURE_MODEL is required for live image generation")
    gateway = GatewayImageClient.from_args(args)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # --- Phase 1: generate the 2 base seamless sub-tiles ---
    sub_filled = args.out_dir / "sub_filled.png"
    sub_empty = args.out_dir / "sub_empty.png"

    if not args.reuse_existing or not sub_filled.exists():
        gen_seamless(gateway, args.filled_prompt, args.size, sub_filled, texture_model)
    else:
        print(f"  reusing {sub_filled}")
    if not args.reuse_existing or not sub_empty.exists():
        gen_seamless(gateway, args.empty_prompt, args.size, sub_empty, texture_model)
    else:
        print(f"  reusing {sub_empty}")

    # --- Phase 2: generate the 4 corner-transition sub-tiles ---
    # Combined transition prompt: describes both materials and the boundary.
    transition_prompt = (
        f"a stylized hand-painted top-down game tile: {args.filled_prompt} "
        f"meeting {args.empty_prompt} with a natural transition along the diagonal, "
        f"seamless tileable texture, detailed"
    )
    transition_paths = {}
    for i, corner in enumerate(["NW", "NE", "SW", "SE"]):
        path = args.out_dir / f"sub_trans_{corner}.png"
        if not args.reuse_existing or not path.exists():
            gen_corner_transition(
                filled_img=sub_filled, empty_img=sub_empty,
                filled_corner=corner, prompt=transition_prompt,
                size=args.size, out_path=path, seed=args.seed + i,
                comfy_input=Path(require(args.comfy_input_dir, "--comfy-input-dir")),
                edge_config=comfy_cli_args(args))
        else:
            print(f"  reusing {path}")
        transition_paths[corner] = path

    # --- Phase 3: load palette into memory ---
    palette = {
        "filled": np.array(Image.open(sub_filled).convert("RGB")),
        "empty": np.array(Image.open(sub_empty).convert("RGB")),
        "trans_TL": np.array(Image.open(transition_paths["NW"]).convert("RGB")),
        "trans_TR": np.array(Image.open(transition_paths["NE"]).convert("RGB")),
        "trans_BL": np.array(Image.open(transition_paths["SW"]).convert("RGB")),
        "trans_BR": np.array(Image.open(transition_paths["SE"]).convert("RGB")),
    }

    # --- Phase 4: composite the 16 dual-grid tiles ---
    tiles = []
    for bits in range(16):
        tile_arr = composite_tile(palette, bits, args.size)
        Image.fromarray(tile_arr).save(args.out_dir / f"tile_{bits:02d}.png")
        tiles.append(tile_arr)

    # --- Phase 5: spritesheet + preview ---
    sheet = make_spritesheet(tiles, args.size)
    sheet.save(args.out_dir / "spritesheet.png")
    preview = render_preview(palette, args.size, w=12, h=8, seed=1)
    preview.save(args.out_dir / "preview.png")

    # --- Phase 6: manifest ---
    manifest = {
        "tile_size": args.size,
        "tile_count": 16,
        "rule": "dual-grid-marching-squares",
        "filled_prompt": args.filled_prompt,
        "empty_prompt": args.empty_prompt,
        "palette": ["sub_filled.png", "sub_empty.png",
                    "sub_trans_NW.png", "sub_trans_NE.png",
                    "sub_trans_SW.png", "sub_trans_SE.png"],
        "tile_index_bits": {"TL": 0, "TR": 1, "BL": 2, "BR": 3},
        "files": {
            "spritesheet": "spritesheet.png",
            "preview": "preview.png",
            "tiles": [f"tile_{i:02d}.png" for i in range(16)],
        },
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n[build-dualgrid] generated 6 sub-tiles + 16 tiles in {time.time()-t0:.1f}s")
    print(f"[build-dualgrid] output: {args.out_dir}")
    print(f"[build-dualgrid] spritesheet: {args.out_dir / 'spritesheet.png'}")
    print(f"[build-dualgrid] preview map: {args.out_dir / 'preview.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
