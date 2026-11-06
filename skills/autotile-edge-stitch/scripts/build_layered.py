#!/usr/bin/env python3
"""Build a layered N-terrain autotile set (Boris Eyrich's "Option 1": stacked
S-V2 sets with transparency). Each non-base terrain layer is its own dual-grid
16-tile set where the "filled" side is the layer's material and the "empty"
side is transparent — when stacked back-to-front, the layer below shows through.

Usage:
  build_layered.py --out-dir ./tileset --spec ocean_biome.json [--size 512] [--steps 30]

Spec (JSON):
{
  "biome": "ocean",
  "size": 512,
  "steps": 30,
  "layers": [
    {"id": "ocean", "filled_prompt": "deep cobalt ocean water with subtle caustics..."},
    {"id": "shallow", "filled_prompt": "translucent turquoise shallows..."},
    {"id": "beach", "filled_prompt": "warm golden sand beach..."},
    {"id": "rocks", "filled_prompt": "mossy grey weathered coastal rocks..."}
  ]
}

The FIRST layer is the base (no transparency needed; just one seamless tile).
Every other layer gets the full 6-sub-tile palette WITH alpha — "empty" sub-tile
is fully transparent.

Output:
  <out-dir>/<layer_id>/sub_filled.png       (RGBA, alpha=255)
  <out-dir>/<layer_id>/sub_empty.png        (RGBA, alpha=0) — except for base layer where it's solid
  <out-dir>/<layer_id>/sub_trans_*.png      (RGBA, transition between filled and transparent)
  <out-dir>/<layer_id>/tile_00.png .. tile_15.png  (RGBA per layer)
  <out-dir>/manifest.json                   (per-layer metadata + rendering instructions)
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, uuid
from pathlib import Path
import numpy as np
from PIL import Image
from runtime import (GatewayImageClient, add_comfy_args, add_gateway_args, comfy_cli_args,
                     contained_path, require, safe_spec_id, spec_id_key)

EDGE_STITCH = Path(__file__).parent / "edge_stitch.py"


def gen_seamless(client: GatewayImageClient, prompt: str, size: int, out_path: Path, model: str | None) -> None:
    """Generate a texture through the public OpenAI Images API."""
    print(f"  generating seamless {out_path.name}...")
    out_path.write_bytes(client.generate(model=model, prompt=prompt, size=size))


def gen_corner_transition(
    *, filled_img: Path, empty_img: Path, filled_corner: str,
    prompt: str, size: int, out_path: Path, seed: int, steps: int,
    comfy_input: Path, edge_config: list[str]) -> None:
    """Generate a corner-transition sub-tile where one corner is filled
    material and the opposite corner is empty (later: made transparent).

    filled_corner: 'NW', 'NE', 'SW', or 'SE' (which corner is filled).
    """
    filled = np.array(Image.open(filled_img).convert("RGB"))
    empty = np.array(Image.open(empty_img).convert("RGB"))
    if filled.shape != (size, size, 3) or empty.shape != (size, size, 3):
        sys.exit(f"sub-tile size mismatch: filled={filled.shape} empty={empty.shape}")

    canvas = np.full((size, size, 3), 128, dtype=np.uint8)
    mask = np.full((size, size), 255, dtype=np.uint8)
    h = size // 2

    if filled_corner == "NW":
        canvas[:h, :h] = filled[:h, :h]; mask[:h, :h] = 0
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

    work = comfy_input / f"_layered_corner_{uuid.uuid4().hex[:8]}"
    canvas_p = work.with_suffix(".canvas.png")
    mask_p = work.with_suffix(".mask.png")
    try:
        Image.fromarray(canvas).save(canvas_p)
        Image.fromarray(mask).save(mask_p)
        print(f"    corner {filled_corner} -> {out_path.name}...")
        r = subprocess.run([str(EDGE_STITCH),
                        "--reference", str(canvas_p),
                        "--mask", str(mask_p),
                        "--prompt", prompt,
                        "--output", str(out_path),
                        "--seed", str(seed),
                            "--steps", str(steps), *edge_config])
    finally:
        canvas_p.unlink(missing_ok=True); mask_p.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit(f"corner-transition failed for {filled_corner}")


def add_alpha_for_layer(sub_path: Path, filled_corner: str, size: int) -> None:
    """Take a corner-transition RGB sub-tile and convert it to RGBA by
    computing per-pixel "filled-ness" from how close each pixel is to the
    filled material vs the empty material at the appropriate corner.

    For a stacked-layer renderer, the "empty" side of a transition tile must
    be transparent so the layer below shows through. We use color distance:
    pixels closer to `empty` get alpha 0; pixels closer to `filled` get
    alpha 255; gradients in between.
    """
    sub_rgb = np.array(Image.open(sub_path).convert("RGB")).astype(np.float32)
    h = size // 2

    # Per-pixel distance to the filled and empty materials.
    # For each pixel, compute distance to a spatially-local sample of filled
    # and empty (the nearest preserved-corner region's mean color).
    def mean_color(arr, slc_y, slc_x):
        return arr[slc_y, slc_x].mean(axis=(0, 1))

    # Find the filled-corner mean and empty-corner mean from the source sub-tile.
    # The two preserved-corner regions are at filled_corner and its anti-diagonal.
    corner_to_slices = {
        "NW": ((slice(0, h), slice(0, h)), (slice(h, size), slice(h, size))),
        "NE": ((slice(0, h), slice(h, size)), (slice(h, size), slice(0, h))),
        "SW": ((slice(h, size), slice(0, h)), (slice(0, h), slice(h, size))),
        "SE": ((slice(h, size), slice(h, size)), (slice(0, h), slice(0, h))),
    }
    if filled_corner not in corner_to_slices:
        sys.exit(f"unknown corner '{filled_corner}'")
    (f_slc, e_slc) = corner_to_slices[filled_corner]
    filled_mean = mean_color(sub_rgb, *f_slc)
    empty_mean = mean_color(sub_rgb, *e_slc)

    # Per-pixel: alpha = how-close-to-filled, normalized
    df = np.linalg.norm(sub_rgb - filled_mean, axis=2)
    de = np.linalg.norm(sub_rgb - empty_mean, axis=2)
    # When df is small (close to filled), alpha should be high
    # When de is small (close to empty), alpha should be low
    # Use df / (df + de) — pixels equidistant get 0.5
    eps = 1e-6
    alpha_f = de / (df + de + eps)  # in [0,1], high when far from empty
    alpha = np.clip(alpha_f, 0, 1)
    alpha8 = (alpha * 255).astype(np.uint8)

    rgba = np.dstack([sub_rgb.astype(np.uint8), alpha8])
    Image.fromarray(rgba).save(sub_path)


def make_transparent(out_path: Path, size: int) -> None:
    """Create an RGBA all-transparent 'empty' sub-tile."""
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    Image.fromarray(rgba).save(out_path)


def composite_tile_layered(palette: dict[str, np.ndarray], filled_bits: int,
                            size: int) -> np.ndarray:
    """Composite one 16-tile entry from a layer's palette. Same lookup logic as
    the original build_dualgrid.composite_tile but returns RGBA."""
    h = size // 2
    tile = np.zeros((size, size, 4), dtype=np.uint8)

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
            sub = palette["filled"]
        elif (not filled) and (not diag_filled):
            sub = palette["empty"]
        elif filled and not diag_filled:
            sub = palette[f"trans_{quadrant}"]
        else:
            sub = palette[f"trans_{diag_quad}"]

        if quadrant == "TL": tile[:h, :h] = sub[:h, :h]
        elif quadrant == "TR": tile[:h, h:] = sub[:h, h:]
        elif quadrant == "BL": tile[h:, :h] = sub[h:, :h]
        elif quadrant == "BR": tile[h:, h:] = sub[h:, h:]
    return tile


def to_rgba(img_path: Path, alpha: int = 255) -> None:
    """Convert an RGB PNG in place to RGBA with given alpha."""
    img = np.array(Image.open(img_path).convert("RGB"))
    rgba = np.dstack([img, np.full(img.shape[:2], alpha, dtype=np.uint8)])
    Image.fromarray(rgba).save(img_path)


def build_layer(layer_id: str, filled_prompt: str, transition_prompt: str,
                size: int, seed_base: int, steps: int,
                out_dir: Path, base_empty_path: Path | None = None,
                is_base: bool = False, client: GatewayImageClient | None = None,
                texture_model: str | None = None, comfy_input: Path | None = None,
                edge_config: list[str] | None = None, reuse_existing: bool = False) -> dict:
    """Generate a single terrain layer's sub-tile palette + 16 composited tiles.

    is_base=True means this is the bottom layer — opaque "empty" tile (no transparency).
    Otherwise the "empty" sub-tile is fully transparent.

    base_empty_path: when is_base=False, this is what gets shown in the transition
    sub-tiles' "empty" region during diffusion (so the model has something to blend
    *against*). We use a transparent-equivalent texture: typically the previous
    layer's filled material — visually the layer that would show through.
    """
    layer_dir = contained_path(out_dir, safe_spec_id(layer_id, "layer id"))
    layer_dir.mkdir(parents=True, exist_ok=True)

    # --- Phase 1: filled (seamless) ---
    sub_filled = layer_dir / "sub_filled.png"
    if not (reuse_existing and sub_filled.exists()):
        if client is None or texture_model is None:
            raise ValueError("gateway client and texture model are required when generating sub-tiles")
        gen_seamless(client, filled_prompt, size, sub_filled, texture_model)
    to_rgba(sub_filled, alpha=255)

    # --- Phase 2: empty ---
    sub_empty = layer_dir / "sub_empty.png"
    if is_base:
        # Base layer has no transparency; "empty" is the same as filled
        # (we won't actually use the empty sub-tile in rendering anyway —
        # the base layer is just tile_15 stamped everywhere).
        if not (reuse_existing and sub_empty.exists()):
            np_filled = np.array(Image.open(sub_filled).convert("RGBA"))
            Image.fromarray(np_filled).save(sub_empty)
    else:
        # Empty is fully transparent
        if not (reuse_existing and sub_empty.exists()):
            make_transparent(sub_empty, size)

    # --- Phase 3: corner transitions ---
    # For non-base layers, the transition needs to know what to blend *into*.
    # We pass `base_empty_path` (the layer-below's filled material) as the
    # "empty" reference so the model generates a transition between this
    # layer's material and the layer below.
    if not is_base:
        if base_empty_path is None:
            sys.exit(f"layer {layer_id}: non-base layer needs base_empty_path")
        empty_ref = base_empty_path
    else:
        empty_ref = sub_filled  # not used; base has no transitions

    transition_paths = {}
    for i, corner in enumerate(["NW", "NE", "SW", "SE"]):
        path = layer_dir / f"sub_trans_{corner}.png"
        if is_base:
            # base layer has no transitions
            continue
        if not (reuse_existing and path.exists()):
            gen_corner_transition(
                filled_img=sub_filled, empty_img=empty_ref,
                filled_corner=corner, prompt=transition_prompt,
                size=size, out_path=path, seed=seed_base + i, steps=steps,
                comfy_input=Path(require(comfy_input, "--comfy-input-dir")),
                edge_config=edge_config or [])
            # Convert RGB → RGBA with alpha derived from material similarity
            add_alpha_for_layer(path, corner, size)
        transition_paths[corner] = path

    # --- Phase 4: composite the 16 tiles ---
    if is_base:
        # Base layer: just stamp tile_15 (all-filled) everywhere; the other 15 tiles
        # are identical because there's no transparency
        base_tile = np.array(Image.open(sub_filled).convert("RGBA"))
        for bits in range(16):
            Image.fromarray(base_tile).save(layer_dir / f"tile_{bits:02d}.png")
    else:
        palette = {
            "filled": np.array(Image.open(sub_filled).convert("RGBA")),
            "empty": np.array(Image.open(sub_empty).convert("RGBA")),
            "trans_TL": np.array(Image.open(transition_paths["NW"]).convert("RGBA")),
            "trans_TR": np.array(Image.open(transition_paths["NE"]).convert("RGBA")),
            "trans_BL": np.array(Image.open(transition_paths["SW"]).convert("RGBA")),
            "trans_BR": np.array(Image.open(transition_paths["SE"]).convert("RGBA")),
        }
        for bits in range(16):
            tile_arr = composite_tile_layered(palette, bits, size)
            Image.fromarray(tile_arr).save(layer_dir / f"tile_{bits:02d}.png")

    return {"id": layer_id, "filled_prompt": filled_prompt,
            "is_base": is_base, "size": size}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--spec", required=True, type=Path,
                   help="JSON spec describing the biome + per-layer prompts")
    p.add_argument("--reuse-existing", action="store_true",
                   help="Skip generation if sub-tile PNGs already exist on disk")
    p.add_argument("--texture-model", default=os.environ.get("AUTOTILE_TEXTURE_MODEL"),
                   help="operator-configured model ID (env: AUTOTILE_TEXTURE_MODEL)")
    add_gateway_args(p)
    add_comfy_args(p)
    args = p.parse_args()

    if not args.texture_model and not args.reuse_existing:
        p.error("--texture-model or AUTOTILE_TEXTURE_MODEL is required for live image generation")
    gateway = GatewayImageClient.from_args(args)

    spec = json.loads(args.spec.read_text())
    size = int(spec.get("size", 512))
    steps = int(spec.get("steps", 30))
    if size < 2 or size % 2 or steps < 1:
        p.error("size must be an even positive integer and steps must be positive")
    layers_cfg = spec["layers"]
    if not isinstance(layers_cfg, list) or not layers_cfg:
        p.error("layers must be a non-empty list")
    ids = [safe_spec_id(layer.get("id"), "layer id") for layer in layers_cfg]
    if len(ids) != len({spec_id_key(value) for value in ids}):
        p.error("layer ids must be unique")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    layer_manifests = []
    prev_filled: Path | None = None  # for non-base layers: what's below

    for i, layer in enumerate(layers_cfg):
        layer_id = ids[i]
        is_base = (i == 0)
        print(f"\n=== layer {i+1}/{len(layers_cfg)}: {layer_id} (base={is_base}) ===")
        m = build_layer(
            layer_id=layer_id,
            filled_prompt=layer["filled_prompt"],
            transition_prompt=layer.get("transition_prompt",
                f"painterly hand-painted top-down game tile: "
                f"{layer['filled_prompt']} fading into the underlying material "
                f"with a soft organic diagonal transition, fantasy game art"),
            size=size, seed_base=42 + i * 10, steps=steps,
            out_dir=args.out_dir,
            base_empty_path=prev_filled,
            is_base=is_base,
            client=gateway,
            texture_model=args.texture_model,
            comfy_input=Path(require(args.comfy_input_dir, "--comfy-input-dir")),
            edge_config=comfy_cli_args(args),
            reuse_existing=args.reuse_existing,
        )
        layer_manifests.append(m)
        prev_filled = contained_path(args.out_dir, layer_id, "sub_filled.png")

    manifest = {
        "biome": spec.get("biome", "unknown"),
        "size": size,
        "steps": steps,
        "tile_count_per_layer": 16,
        "rule": "dual-grid-marching-squares",
        "rendering": "stacked-layers-back-to-front",
        "layers": layer_manifests,
        "layer_order": [m["id"] for m in layer_manifests],
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n[build-layered] generated {len(layers_cfg)} layers in {time.time()-t0:.1f}s")
    print(f"[build-layered] output: {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
