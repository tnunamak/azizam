#!/usr/bin/env python3
"""Build a layered dual-grid tileset by generating each of the 16 tiles directly
via inpainting (no sub-tile precomposition).

For each non-base layer, for each of 16 dual-grid tile codes:
  1. Build a canvas where filled quadrants contain the layer's seamless material
     (byte-exact) and empty quadrants contain a contrasting reference (the
     layer-below's material).
  2. Build a mask that preserves the filled quadrants (mask=0) and regenerates
     the empty quadrants (mask=255).
  3. Run the configured inpaint workflow via edge_stitch.py. The model produces a transition
     curve where filled meets empty — exactly along the quadrant boundary.
  4. Post-process: set alpha=0 in the originally-empty quadrants so the layer
     below shows through at render time. The transition curve survives in the
     filled-side bulge of the alpha mask, which we compute via material
     similarity (same technique as build_layered.py's add_alpha_for_layer).

This is more expensive than precomposition (16 generations per layer vs 4) but
produces visually correct tiles where the transition lives exactly where the
dual-grid geometry demands.

Each tile validates per gate 1 immediately after generation; failures stop the
build with diagnostics.
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
    print(f"    seamless {out_path.name}...")
    out_path.write_bytes(client.generate(model=model, prompt=prompt, size=size))


# Quadrant geometry for a 4-bit S-V2 tile code (bit 0 = TL, 1 = TR, 2 = BL, 3 = BR).
QUADRANTS = ["TL", "TR", "BL", "BR"]
def quad_slice(quadrant: str, size: int) -> tuple[slice, slice]:
    h = size // 2
    return {
        "TL": (slice(0, h), slice(0, h)),
        "TR": (slice(0, h), slice(h, size)),
        "BL": (slice(h, size), slice(0, h)),
        "BR": (slice(h, size), slice(h, size)),
    }[quadrant]


def build_tile_canvas_mask(bits: int, size: int,
                            filled_material: np.ndarray,
                            empty_reference: np.ndarray) -> tuple[Image.Image, Image.Image]:
    """For a given tile code, build (canvas, mask) for inpainting.

    - canvas: filled-quadrant content from filled_material; empty-quadrant
      content from empty_reference. Both are size×size RGB arrays.
    - mask: 0 (preserve) in filled quadrants; 255 (regenerate) in empty quadrants.
      The model fills the empty regions, producing a transition curve at the
      quadrant boundaries with the filled regions.
    """
    assert filled_material.shape == (size, size, 3)
    assert empty_reference.shape == (size, size, 3)

    canvas = np.empty((size, size, 3), dtype=np.uint8)
    mask = np.empty((size, size), dtype=np.uint8)

    for q in QUADRANTS:
        rs, cs = quad_slice(q, size)
        is_filled = (bits & (1 << QUADRANTS.index(q))) != 0
        if is_filled:
            canvas[rs, cs] = filled_material[rs, cs]
            mask[rs, cs] = 0  # preserve
        else:
            canvas[rs, cs] = empty_reference[rs, cs]
            mask[rs, cs] = 255  # regenerate

    return Image.fromarray(canvas), Image.fromarray(mask)


def generate_tile(bits: int, size: int, prompt: str, seed: int, steps: int,
                  filled_material: np.ndarray, empty_reference: np.ndarray,
                  out_path: Path, comfy_input: Path, edge_config: list[str]) -> None:
    """Generate one of the 16 tiles through the configured edge stitcher."""
    if bits == 0:
        # All-empty tile: just save a fully-transparent image.
        rgba = np.zeros((size, size, 4), dtype=np.uint8)
        Image.fromarray(rgba).save(out_path)
        return
    if bits == 15:
        # All-filled tile: just save the filled material with alpha=255.
        rgba = np.dstack([filled_material, np.full((size, size), 255, dtype=np.uint8)])
        Image.fromarray(rgba).save(out_path)
        return

    canvas, mask = build_tile_canvas_mask(bits, size, filled_material, empty_reference)
    work = comfy_input / f"_directtile_{uuid.uuid4().hex[:8]}"
    canvas_p = work.with_suffix(".canvas.png")
    mask_p = work.with_suffix(".mask.png")
    try:
        canvas.save(canvas_p)
        mask.save(mask_p)
        print(f"    tile_{bits:02d} (S-V2[{bits & 1}{(bits>>1) & 1}{(bits>>2) & 1}{(bits>>3) & 1}])...")
        r = subprocess.run([str(EDGE_STITCH),
                        "--reference", str(canvas_p),
                        "--mask", str(mask_p),
                        "--prompt", prompt,
                        "--output", str(out_path),
                        "--seed", str(seed),
                            "--steps", str(steps), *edge_config])
    finally:
        canvas_p.unlink(missing_ok=True)
        mask_p.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit(f"tile gen failed for bits={bits}")

    # Post-process: re-apply alpha based on which quadrants were originally
    # filled. The filled quadrants keep alpha=255; the empty quadrants get
    # alpha=0 in their interior but with a smooth fall-off at the boundary
    # based on per-pixel similarity to the filled-material vs the empty-
    # reference. This preserves the transition curve in the filled-side
    # bulge while cutting off the empty side cleanly.
    rgb = np.array(Image.open(out_path).convert("RGB"))
    alpha = compute_alpha_for_tile(bits, size, rgb, filled_material, empty_reference)
    rgba = np.dstack([rgb, alpha])
    Image.fromarray(rgba).save(out_path)


def compute_alpha_for_tile(bits: int, size: int, rgb: np.ndarray,
                            filled_material: np.ndarray,
                            empty_reference: np.ndarray) -> np.ndarray:
    """Per-pixel alpha for the generated tile.

    For each pixel, we check whether it's closer to the filled material's
    color profile (high alpha) or to the empty reference's color profile
    (low alpha). This handles the transition zone correctly: pixels that
    look like the filled material get alpha=255, pixels that look like
    the empty reference get alpha=0, with a smooth fall-off in between
    based on perceptual color distance.

    We also start from a hard mask (filled quadrants → alpha 255, empty
    quadrants → alpha 0) and let the perceptual term modulate near the
    boundary.
    """
    # Hard base alpha from quadrant pattern
    alpha = np.zeros((size, size), dtype=np.float32)
    for q in QUADRANTS:
        rs, cs = quad_slice(q, size)
        is_filled = (bits & (1 << QUADRANTS.index(q))) != 0
        alpha[rs, cs] = 255.0 if is_filled else 0.0

    # Perceptual modulation: compute per-pixel distance to filled material
    # vs empty reference. Use local windows in the filled & empty quadrants
    # to sample representative colors.
    rgb_f = rgb.astype(np.float32)
    fm_f = filled_material.astype(np.float32)
    er_f = empty_reference.astype(np.float32)
    # Mean colors of representative quadrants
    filled_q = [q for q in QUADRANTS if (bits & (1 << QUADRANTS.index(q))) != 0]
    empty_q = [q for q in QUADRANTS if (bits & (1 << QUADRANTS.index(q))) == 0]
    fm_mean = np.mean([fm_f[quad_slice(q, size)].mean(axis=(0, 1)) for q in filled_q], axis=0)
    er_mean = np.mean([er_f[quad_slice(q, size)].mean(axis=(0, 1)) for q in empty_q], axis=0) if empty_q else fm_mean

    df = np.linalg.norm(rgb_f - fm_mean, axis=2)
    de = np.linalg.norm(rgb_f - er_mean, axis=2)
    perceptual = de / (df + de + 1e-6)  # 1 when close to filled, 0 when close to empty

    # Only apply perceptual modulation in a transition band near quadrant
    # boundaries. Far from boundaries, trust the hard alpha. Near boundaries,
    # blend with perceptual.
    # Define "near boundary" as within 32 px of any quadrant boundary.
    band = 48  # pixels
    h = size // 2
    # Distance from each pixel to the nearest quadrant boundary
    yy, xx = np.mgrid[0:size, 0:size]
    dy = np.minimum(np.abs(yy - h), np.minimum(yy, size - yy))
    dx = np.minimum(np.abs(xx - h), np.minimum(xx, size - xx))
    # Strongest perceptual blend at boundary (d=0), zero blend far from
    blend = np.clip(1.0 - np.minimum(dy, dx) / band, 0, 1)
    # Modulated alpha: blend between hard (0/255) and perceptual (0..255)
    alpha_perceptual = perceptual * 255.0
    final = alpha * (1 - blend) + alpha_perceptual * blend
    return np.clip(final, 0, 255).astype(np.uint8)


def gate1_validate(out_path: Path, bits: int, size: int) -> tuple[bool, str]:
    """Check the generated tile has the right alpha pattern per quadrant.

    Returns (ok, message).
    """
    img = np.array(Image.open(out_path).convert("RGBA"))
    h = size // 2
    results = {}
    for q in QUADRANTS:
        rs, cs = quad_slice(q, size)
        is_filled = (bits & (1 << QUADRANTS.index(q))) != 0
        center_rs = slice(rs.start + h//4, rs.stop - h//4)
        center_cs = slice(cs.start + h//4, cs.stop - h//4)
        center_alpha = img[center_rs, center_cs, 3].mean()
        results[q] = (is_filled, center_alpha)

    msgs = []
    ok = True
    for q, (is_filled, alpha) in results.items():
        if is_filled and alpha < 200:
            ok = False
            msgs.append(f"{q} filled but center alpha={alpha:.0f}")
        if (not is_filled) and alpha > 60:
            ok = False
            msgs.append(f"{q} empty but center alpha={alpha:.0f}")
    return ok, "; ".join(msgs) if msgs else "OK"


def build_layer_direct(layer_id: str, filled_prompt: str, transition_prompt: str,
                       size: int, seed_base: int, steps: int,
                       out_dir: Path, layer_below_path: Path | None,
                       is_base: bool, client: GatewayImageClient, texture_model: str | None,
                       comfy_input: Path, edge_config: list[str], reuse_existing: bool = False) -> dict:
    """Generate one layer's 16 tiles directly."""
    layer_dir = contained_path(out_dir, safe_spec_id(layer_id, "layer id"))
    layer_dir.mkdir(parents=True, exist_ok=True)

    # Generate the seamless filled material
    sub_filled = layer_dir / "sub_filled.png"
    if not (reuse_existing and sub_filled.exists()):
        gen_seamless(client, filled_prompt, size, sub_filled, texture_model)

    if is_base:
        # Base layer: every tile is the filled material with alpha=255
        filled_arr = np.array(Image.open(sub_filled).convert("RGB"))
        rgba = np.dstack([filled_arr, np.full((size, size), 255, dtype=np.uint8)])
        for bits in range(16):
            Image.fromarray(rgba).save(layer_dir / f"tile_{bits:02d}.png")
        print(f"  {layer_id}: base layer, 16 tiles identical")
        return {"id": layer_id, "is_base": True, "size": size}

    # Non-base: need the layer-below as empty reference
    assert layer_below_path is not None, f"non-base layer {layer_id} needs layer_below"
    empty_ref = np.array(Image.open(layer_below_path).convert("RGB"))
    filled_arr = np.array(Image.open(sub_filled).convert("RGB"))

    validation_results = []
    for bits in range(16):
        out_path = layer_dir / f"tile_{bits:02d}.png"
        if reuse_existing and out_path.exists():
            print(f"    tile_{bits:02d}: reusing existing")
        else:
            generate_tile(bits, size, transition_prompt, seed_base + bits * 7,
                          steps, filled_arr, empty_ref, out_path, comfy_input, edge_config)
        ok, msg = gate1_validate(out_path, bits, size)
        validation_results.append((bits, ok, msg))
        if not ok:
            print(f"    !!! GATE 1 FAIL tile_{bits:02d}: {msg}")

    fails = [r for r in validation_results if not r[1]]
    return {"id": layer_id, "is_base": False, "size": size,
            "gate1_pass": len(fails) == 0,
            "gate1_fails": fails}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--spec", required=True, type=Path)
    p.add_argument("--reuse-existing", action="store_true")
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
    ids = [safe_spec_id(layer.get("id"), "layer id") for layer in spec.get("layers", [])]
    if not ids or len(ids) != len({spec_id_key(value) for value in ids}):
        p.error("layers must be a non-empty list with unique portable ids")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    layer_manifests = []
    prev_filled = None

    for i, layer in enumerate(spec["layers"]):
        is_base = (i == 0)
        layer["id"] = ids[i]
        print(f"\n=== layer {i+1}/{len(spec['layers'])}: {ids[i]} (base={is_base}) ===")
        m = build_layer_direct(
            layer_id=layer["id"],
            filled_prompt=layer["filled_prompt"],
            transition_prompt=layer.get("transition_prompt", layer["filled_prompt"]),
            size=size, seed_base=42 + i * 100, steps=steps,
            out_dir=args.out_dir,
            layer_below_path=prev_filled,
            is_base=is_base,
            client=gateway,
            texture_model=args.texture_model,
            comfy_input=Path(require(args.comfy_input_dir, "--comfy-input-dir")),
            edge_config=comfy_cli_args(args),
            reuse_existing=args.reuse_existing,
        )
        layer_manifests.append(m)
        prev_filled = contained_path(args.out_dir, ids[i], "sub_filled.png")

    manifest = {
        "biome": spec.get("biome", "unknown"),
        "size": size,
        "steps": steps,
        "tile_count_per_layer": 16,
        "rule": "dual-grid-marching-squares",
        "rendering": "stacked-layers-back-to-front",
        "build_method": "direct-tile-generation",
        "layers": layer_manifests,
        "layer_order": [m["id"] for m in layer_manifests],
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    elapsed = time.time() - t0
    print(f"\n[build-direct] {len(layer_manifests)} layers in {elapsed:.0f}s = {elapsed/60:.1f}min")
    fails = [m for m in layer_manifests if not m.get("gate1_pass", True)]
    if fails:
        print(f"[build-direct] {len(fails)} layers have gate-1 failures:")
        for m in fails:
            print(f"  {m['id']}: {m['gate1_fails']}")
        return 1
    print(f"[build-direct] ALL gate-1 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
