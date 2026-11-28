#!/usr/bin/env python3
"""Edge-stitched inpainting for autotile generation.

Given a reference image and a preserve-mask (black=preserve, white=regenerate),
run an operator-configured ComfyUI inpaint workflow with strict-mask settings,
then hard-overwrite the preserved region from the reference to guarantee
byte-exact preservation.

The output is suitable for compositing into tile sets: any black region in the
mask is byte-identical to the same region in the reference.
"""
from __future__ import annotations
import argparse, json, shutil, sys, time, uuid, urllib.request, urllib.error
from pathlib import Path
import numpy as np
from PIL import Image
from runtime import add_comfy_args, comfy_output_path, require


def build_workflow(image_name: str, mask_name: str, prompt: str, *,
                   model_file: str, unet_loader_node: str, text_encoder_1: str,
                   text_encoder_2: str, dual_clip_type: str, dual_clip_loader_node: str,
                   vae_file: str, vae_loader_node: str, crop_node: str, stitch_node: str,
                   target_size: tuple[int, int], seed: int, steps: int,
                   output_prefix: str) -> dict:
    return {
        "1": {"class_type": unet_loader_node, "inputs": {"unet_name": model_file}},
        "2": {"class_type": dual_clip_loader_node, "inputs": {
            "clip_name1": text_encoder_1,
            "clip_name2": text_encoder_2,
            "type": dual_clip_type}},
        "3": {"class_type": vae_loader_node, "inputs": {"vae_name": vae_file}},
        "10": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "11": {"class_type": "LoadImage", "inputs": {"image": mask_name}},
        "12": {"class_type": "ImageToMask", "inputs": {"image": ["11", 0], "channel": "red"}},
        "20": {"class_type": crop_node, "inputs": {
            "image": ["10", 0], "mask": ["12", 0],
            "downscale_algorithm": "bilinear", "upscale_algorithm": "bicubic",
            "preresize": False, "preresize_mode": "ensure minimum resolution",
            "preresize_min_width": 1024, "preresize_min_height": 1024,
            "preresize_max_width": 16384, "preresize_max_height": 16384,
            "mask_fill_holes": False, "mask_expand_pixels": 0,
            "mask_invert": False, "mask_blend_pixels": 0, "mask_hipass_filter": 0.0,
            "extend_for_outpainting": False,
            "extend_up_factor": 1.0, "extend_down_factor": 1.0,
            "extend_left_factor": 1.0, "extend_right_factor": 1.0,
            "context_from_mask_extend_factor": 2.0,
            "output_resize_to_target_size": True,
            "output_target_width": target_size[0], "output_target_height": target_size[1],
            "output_padding": "32", "device_mode": "gpu (much faster)"}},
        "30": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "31": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "40": {"class_type": "InpaintModelConditioning", "inputs": {
            "positive": ["30", 0], "negative": ["31", 0], "vae": ["3", 0],
            "pixels": ["20", 1], "mask": ["20", 2], "noise_mask": True}},
        "50": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0],
            "positive": ["40", 0],
            "negative": ["40", 1],
            "cfg": 4.0}},
        "51": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "52": {"class_type": "BasicScheduler", "inputs": {
            "scheduler": "normal", "steps": steps, "denoise": 1.0, "model": ["1", 0]}},
        "53": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "54": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["53", 0], "guider": ["50", 0], "sampler": ["51", 0],
            "sigmas": ["52", 0], "latent_image": ["40", 2]}},
        "60": {"class_type": "VAEDecode", "inputs": {"samples": ["54", 0], "vae": ["3", 0]}},
        "70": {"class_type": stitch_node, "inputs": {
            "stitcher": ["20", 0], "inpainted_image": ["60", 0]}},
        "80": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": output_prefix, "images": ["70", 0]}},
    }


def submit_and_wait(workflow: dict, *, comfy_url: str, timeout_s: int = 600) -> str:
    payload = json.dumps({"prompt": workflow, "client_id": "edge-stitch"}).encode()
    req = urllib.request.Request(
        f"{comfy_url.rstrip('/')}/prompt", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        sys.exit(f"comfy submit failed: {e.code} {e.read().decode()[:600]}")
    prompt_id = json.loads(resp)["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        h = json.loads(urllib.request.urlopen(
            f"{comfy_url.rstrip('/')}/history/{prompt_id}", timeout=10).read())
        if prompt_id in h:
            outputs = h[prompt_id].get("outputs", {})
            for v in outputs.values():
                if "images" in v:
                    return v["images"][0]["filename"]
            sys.exit(f"comfy completed but produced no image: {h[prompt_id].get('status')}")
        time.sleep(2)
    sys.exit(f"comfy timed out after {timeout_s}s")


def hard_post_composite(reference: Path, inpainted: Path, mask: Path, output: Path) -> tuple[int, int]:
    """Preserve every channel in the reference's image mode."""
    ref_image = Image.open(reference)
    out_image = Image.open(inpainted).convert(ref_image.mode)
    ref = np.array(ref_image)
    out = np.array(out_image)
    m = np.array(Image.open(mask).convert("L"))
    if ref.shape != out.shape:
        sys.exit(f"shape mismatch: reference {ref.shape} vs inpainted {out.shape}")
    if m.shape != ref.shape[:2]:
        sys.exit(f"mask shape mismatch: {m.shape} vs {ref.shape[:2]}")
    preserve = (m < 128)
    ref_channels = ref[..., None] if ref.ndim == 2 else ref
    out_channels = out[..., None] if out.ndim == 2 else out
    diff = np.abs(ref_channels.astype(int) - out_channels.astype(int)).sum(-1)
    drift = diff[preserve]
    max_d = int(drift.max()) if drift.size else 0
    n_drift = int((drift > 0).sum())
    composite_mask = preserve if ref.ndim == 2 else preserve[..., None]
    composited = np.where(composite_mask, ref, out).astype(np.uint8)
    result_image = Image.fromarray(composited, mode=ref_image.mode)
    if ref_image.mode == "P" and ref_image.palette is not None:
        result_image.putpalette(ref_image.palette)
    result_image.save(output)
    result_image.close()
    ref_image.close(); out_image.close()
    return max_d, n_drift


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reference", required=True, type=Path,
                   help="reference image (PNG); preserved-region pixels come from here")
    p.add_argument("--mask", required=True, type=Path,
                   help="preserve mask (PNG); black=preserve, white=regenerate")
    p.add_argument("--prompt", required=True, type=str)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--timeout-s", type=int, default=600)
    add_comfy_args(p)
    p.add_argument("--require-byte-exact", action="store_true",
                   help="exit nonzero if any preserve-region pixel drifted before post-composite")
    args = p.parse_args()

    if not args.reference.exists():
        sys.exit(f"reference not found: {args.reference}")
    if not args.mask.exists():
        sys.exit(f"mask not found: {args.mask}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        comfy_url = str(require(args.comfy_url, "--comfy-url"))
        comfy_input = Path(require(args.comfy_input_dir, "--comfy-input-dir"))
        comfy_output = Path(require(args.comfy_output_dir, "--comfy-output-dir"))
        model_file = str(require(args.inpaint_model_file, "--inpaint-model-file"))
        unet_loader_node = str(require(args.unet_loader_node, "--unet-loader-node"))
        text_encoder_1 = str(require(args.text_encoder_1, "--text-encoder-1"))
        text_encoder_2 = str(require(args.text_encoder_2, "--text-encoder-2"))
        dual_clip_type = str(require(args.dual_clip_type, "--dual-clip-type"))
        dual_clip_loader_node = str(require(args.dual_clip_loader_node, "--dual-clip-loader-node"))
        vae_file = str(require(args.vae_file, "--vae-file"))
        vae_loader_node = str(require(args.vae_loader_node, "--vae-loader-node"))
    except ValueError as exc:
        p.error(str(exc))
    if not comfy_input.is_dir() or not comfy_output.is_dir():
        p.error("--comfy-input-dir and --comfy-output-dir must name existing directories")

    reference_image = Image.open(args.reference)
    target_size = reference_image.size
    reference_image.close()

    # Stage in ComfyUI input dir with unique names
    tag = uuid.uuid4().hex[:8]
    img_name = f"edge_stitch_{tag}_ref.png"
    mask_name = f"edge_stitch_{tag}_mask.png"
    out_prefix = f"edge_stitch_{tag}_out"
    staged_reference = comfy_input / img_name
    staged_mask = comfy_input / mask_name
    try:
        shutil.copy(args.reference, staged_reference)
        shutil.copy(args.mask, staged_mask)
        wf = build_workflow(
            img_name, mask_name, args.prompt,
            model_file=model_file, unet_loader_node=unet_loader_node,
            text_encoder_1=text_encoder_1, text_encoder_2=text_encoder_2,
            dual_clip_type=dual_clip_type, dual_clip_loader_node=dual_clip_loader_node,
            vae_file=vae_file, vae_loader_node=vae_loader_node, crop_node=args.inpaint_crop_node,
            stitch_node=args.inpaint_stitch_node, target_size=target_size,
            seed=args.seed, steps=args.steps, output_prefix=out_prefix,
        )
        print(f"[edge-stitch] submitting to {comfy_url} (seed={args.seed}, steps={args.steps})")
        t0 = time.time()
        out_filename = submit_and_wait(wf, comfy_url=comfy_url, timeout_s=args.timeout_s)
        print(f"[edge-stitch] generation completed in {time.time()-t0:.1f}s -> {out_filename}")
        try:
            inpainted_path = comfy_output_path(comfy_output, out_filename)
        except ValueError as exc:
            p.error(str(exc))
        max_d, n_drift = hard_post_composite(args.reference, inpainted_path, args.mask, args.output)
    finally:
        staged_reference.unlink(missing_ok=True)
        staged_mask.unlink(missing_ok=True)
    print(f"[edge-stitch] preserve-region drift before post-composite: max={max_d}, n_pixels={n_drift}")
    print(f"[edge-stitch] hard post-composite applied; preserved region is byte-identical in all reference channels")
    print(f"[edge-stitch] wrote {args.output}")

    if args.require_byte_exact and n_drift:
        print(f"[edge-stitch] FAIL: {n_drift} preserve-region pixels drifted before post-composite "
              f"(max diff={max_d})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
