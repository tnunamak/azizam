#!/usr/bin/env python3
"""Build a tile set with edge-matched adjacencies.

Reads a JSON spec describing tiles, their adjacencies, and prompts; generates
tiles in topological order, building each tile's reference canvas + preserve
mask from already-generated neighbors; calls edge_stitch.py per tile.

Spec shape:
{
  "tile_size": 512,
  "edge_width": 32,
  "default_prompt": "...",
  "tiles": [
    {
      "id": "seed",
      "prompt": "...",
      "seed_only": true
    },
    {
      "id": "edge_E_of_seed",
      "neighbors": {"W": "seed"},
      "prompt": "..."
    },
    ...
  ]
}

Neighbor keys: N, S, E, W (compass).
- "W" -> neighbor whose RIGHT edge becomes this tile's LEFT edge
- "E" -> neighbor whose LEFT edge becomes this tile's RIGHT edge
- "N" -> neighbor whose BOTTOM edge becomes this tile's TOP edge
- "S" -> neighbor whose TOP edge becomes this tile's BOTTOM edge

A tile with `seed_only: true` is generated unconstrained via the proxy
image-gen endpoint (no neighbors required). Other tiles must have at least one
neighbor that has already been generated.

Outputs are written to <out-dir>/<tile_id>.png. The spec is processed in the
order listed; tiles referencing not-yet-generated neighbors error out.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
from runtime import GatewayImageClient, add_comfy_args, add_gateway_args, comfy_cli_args, require

EDGE_STITCH = Path(__file__).parent / "edge_stitch.py"


def generate_seed_tile(client: GatewayImageClient, prompt: str, size: int, out_path: Path, model: str) -> None:
    """Generate an unconstrained tile through the public OpenAI Images API."""
    out_path.write_bytes(client.generate(model=model, prompt=prompt, size=size))


def build_constraint_canvas(size: int, edge_width: int, neighbors: dict[str, Path]) -> tuple[Image.Image, Image.Image]:
    """Construct a 3x3 grid canvas (3*size × 3*size):
       - Surrounding cardinal cells (N/S/E/W) contain the full neighbor tiles
         where they exist. This is the "real surrounding context" regime:
         neighbors live as unmasked context, not pre-filled into the
         masked region.
       - Center cell is the target tile region: filled with mid-gray.
       - Mask covers the center cell EXCEPT for a thin (edge_width) strip
         along each side that has a neighbor. Those strips are filled with
         the neighbor's edge content (W: neighbor's rightmost edge_width
         columns, etc.) — byte-exact, in the unmasked region — so after
         CropAndStitch + hard post-composite the target tile's edge is
         byte-equal to the neighbor's facing edge. The model sees the
         strip as preserved context + the full neighbor tile further out
         as outer context, so it has continuous material on both scales.

    The output canvas is 3*size on each side. The caller crops the center
    cell after generation.
    """
    big = size * 3
    # Initialize: mid-gray everywhere (matches ImagePadForOutpaint canonical).
    canvas = np.full((big, big, 3), 128, dtype=np.uint8)
    mask = np.full((big, big), 0, dtype=np.uint8)  # 0 = preserve

    # Place full neighbor tiles in their cardinal cells.
    cell_offsets = {
        "N": (0, size),          # top-center cell origin
        "S": (size * 2, size),   # bottom-center
        "W": (size, 0),          # middle-left
        "E": (size, size * 2),   # middle-right
    }
    loaded: dict[str, np.ndarray] = {}
    for direction, neighbor_path in neighbors.items():
        if direction not in cell_offsets:
            sys.exit(f"unknown direction '{direction}' (use N/S/E/W)")
        n = np.array(Image.open(neighbor_path).convert("RGB"))
        if n.shape[:2] != (size, size):
            sys.exit(f"neighbor {neighbor_path} has size {n.shape[:2]}, expected ({size}, {size})")
        loaded[direction] = n
        y, x = cell_offsets[direction]
        canvas[y:y + size, x:x + size] = n

    # Center cell occupies canvas coords [size:size*2, size:size*2].
    cy0, cy1 = size, size * 2
    cx0, cx1 = size, size * 2

    # Mark the entire center cell as regenerate (white).
    mask[cy0:cy1, cx0:cx1] = 255

    # For each present neighbor, stamp its facing edge_width strip into the
    # center cell along the corresponding edge, byte-exact, and mark that
    # strip as preserve (black). This is the byte-exact compositing constraint:
    # the target tile's edge equals the neighbor's facing edge.
    for direction, n in loaded.items():
        if direction == "W":
            # neighbor's rightmost edge_width cols -> target's leftmost edge_width cols
            canvas[cy0:cy1, cx0:cx0 + edge_width] = n[:, size - edge_width:]
            mask[cy0:cy1, cx0:cx0 + edge_width] = 0
        elif direction == "E":
            canvas[cy0:cy1, cx1 - edge_width:cx1] = n[:, :edge_width]
            mask[cy0:cy1, cx1 - edge_width:cx1] = 0
        elif direction == "N":
            canvas[cy0:cy0 + edge_width, cx0:cx1] = n[size - edge_width:, :]
            mask[cy0:cy0 + edge_width, cx0:cx1] = 0
        elif direction == "S":
            canvas[cy1 - edge_width:cy1, cx0:cx1] = n[:edge_width, :]
            mask[cy1 - edge_width:cy1, cx0:cx1] = 0

    return Image.fromarray(canvas), Image.fromarray(mask)


def run_edge_stitch(reference: Path, mask: Path, prompt: str, output: Path,
                    *, seed: int, steps: int, edge_config: list[str]) -> None:
    cmd = [str(EDGE_STITCH),
           "--reference", str(reference),
           "--mask", str(mask),
           "--prompt", prompt,
           "--output", str(output),
           "--seed", str(seed),
           "--steps", str(steps),
           *edge_config]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"edge_stitch failed for {output.stem} (exit {r.returncode})")


def verify_adjacencies(tile_path: Path, neighbors: dict[str, Path], edge_width: int) -> dict[str, int]:
    """For each adjacency, return max byte-diff along the shared edge.
    Returns dict {direction: max_diff}."""
    t = np.array(Image.open(tile_path).convert("RGB"))
    size = t.shape[0]
    results = {}
    for direction, npath in neighbors.items():
        n = np.array(Image.open(npath).convert("RGB"))
        if direction == "W":
            ours, theirs = t[:, :edge_width], n[:, size - edge_width:]
        elif direction == "E":
            ours, theirs = t[:, size - edge_width:], n[:, :edge_width]
        elif direction == "N":
            ours, theirs = t[:edge_width, :], n[size - edge_width:, :]
        elif direction == "S":
            ours, theirs = t[size - edge_width:, :], n[:edge_width, :]
        else:
            sys.exit(f"unknown direction '{direction}' in verify_adjacencies")
        results[direction] = int(np.abs(ours.astype(int) - theirs.astype(int)).max())
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", required=True, type=Path, help="JSON spec file")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--seed-model", default=os.environ.get("AUTOTILE_TEXTURE_MODEL"),
                   help="operator-configured model ID for unconstrained seed tiles")
    p.add_argument("--skip-verify", action="store_true",
                   help="skip the byte-exact adjacency check after each tile")
    add_gateway_args(p)
    add_comfy_args(p)
    args = p.parse_args()

    if not args.seed_model:
        p.error("--seed-model or AUTOTILE_TEXTURE_MODEL is required")
    gateway = GatewayImageClient.from_args(args)
    edge_config = comfy_cli_args(args)

    spec = json.loads(args.spec.read_text())
    size = int(spec.get("tile_size", 512))
    edge_w = int(spec.get("edge_width", 32))
    default_prompt = spec.get("default_prompt", "")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    generated: dict[str, Path] = {}
    t0 = time.time()

    for i, tile in enumerate(spec["tiles"]):
        tid = tile["id"]
        out_path = args.out_dir / f"{tid}.png"
        prompt = tile.get("prompt", default_prompt) or default_prompt
        if not prompt:
            sys.exit(f"tile {tid} has no prompt and no default_prompt set")

        print(f"\n[build-set] [{i+1}/{len(spec['tiles'])}] {tid}")

        if tile.get("seed_only"):
            if out_path.exists():
                print(f"  seed tile already exists; reusing {out_path}")
            else:
                print(f"  generating seed (unconstrained) via {args.seed_model}")
                generate_seed_tile(gateway, prompt, size, out_path, model=args.seed_model)
            generated[tid] = out_path
            continue

        # Constrained tile: build canvas + mask from neighbors
        nrefs = tile.get("neighbors", {})
        if not nrefs:
            sys.exit(f"tile {tid} has no neighbors and is not seed_only")
        neighbor_paths: dict[str, Path] = {}
        for direction, neighbor_id in nrefs.items():
            if neighbor_id not in generated:
                sys.exit(f"tile {tid} references neighbor {neighbor_id} not yet generated")
            neighbor_paths[direction] = generated[neighbor_id]

        canvas, mask = build_constraint_canvas(size, edge_w, neighbor_paths)
        canvas_path = args.out_dir / f"_{tid}_canvas.png"
        mask_path = args.out_dir / f"_{tid}_mask.png"
        big_out_path = args.out_dir / f"_{tid}_big.png"
        canvas.save(canvas_path); mask.save(mask_path)

        # edge_stitch produces a 3*size output (same shape as the 3x3 canvas).
        # We then crop the center cell out as the final tile.
        run_edge_stitch(canvas_path, mask_path, prompt, big_out_path,
                        seed=args.seed + i, steps=args.steps, edge_config=edge_config)
        big = np.array(Image.open(big_out_path).convert("RGB"))
        center = big[size:size * 2, size:size * 2]
        Image.fromarray(center).save(out_path)
        generated[tid] = out_path

        # cleanup intermediate files
        canvas_path.unlink(); mask_path.unlink(); big_out_path.unlink()

        if not args.skip_verify:
            checks = verify_adjacencies(out_path, neighbor_paths, edge_w)
            for d, max_d in checks.items():
                marker = "OK" if max_d == 0 else f"DRIFT {max_d}"
                print(f"  adjacency {d} -> {nrefs[d]}: {marker}")
                if max_d != 0:
                    sys.exit(f"FAIL: {tid} edge {d} not byte-exact (max diff {max_d})")

    print(f"\n[build-set] generated {len(generated)} tiles in {time.time()-t0:.1f}s")
    print(f"[build-set] output dir: {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
