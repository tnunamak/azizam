"""Explicit runtime configuration shared by the autotile build scripts.

This module deliberately knows only the public OpenAI Images request/response
shape and the configurable ComfyUI HTTP/file interfaces. Model IDs, workflow
node names, model files, endpoints, and directories are operator-supplied
data; no gateway or inference-engine selection lives in the scripts.
"""
from __future__ import annotations

import argparse
import base64
import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def add_gateway_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("gateway image generation")
    group.add_argument(
        "--gateway-base-url",
        default=_env("AUTOTILE_GATEWAY_BASE_URL", "http://127.0.0.1:5000/openai/v1"),
        help="OpenAI-compatible API base URL (env: AUTOTILE_GATEWAY_BASE_URL)",
    )
    group.add_argument(
        "--gateway-api-key-env",
        default=_env("AUTOTILE_GATEWAY_API_KEY_ENV", "AI_GATEWAY_API_KEY"),
        help="environment variable holding the bearer token; never pass a token as an argument",
    )


def add_comfy_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("ComfyUI inpainting")
    group.add_argument(
        "--comfy-url", default=_env("AUTOTILE_COMFY_URL"),
        help="ComfyUI HTTP base URL (env: AUTOTILE_COMFY_URL)",
    )
    group.add_argument(
        "--comfy-input-dir", type=Path, default=_env("AUTOTILE_COMFY_INPUT_DIR"),
        help="ComfyUI input directory (env: AUTOTILE_COMFY_INPUT_DIR)",
    )
    group.add_argument(
        "--comfy-output-dir", type=Path, default=_env("AUTOTILE_COMFY_OUTPUT_DIR"),
        help="ComfyUI output directory (env: AUTOTILE_COMFY_OUTPUT_DIR)",
    )
    group.add_argument(
        "--inpaint-model-file", default=_env("AUTOTILE_INPAINT_MODEL_FILE"),
        help="operator-installed inpainting model filename (env: AUTOTILE_INPAINT_MODEL_FILE)",
    )
    group.add_argument(
        "--unet-loader-node", default=_env("AUTOTILE_UNET_LOADER_NODE", "UnetLoaderGGUF"),
        help="ComfyUI UNet-loader node type (env: AUTOTILE_UNET_LOADER_NODE)",
    )
    group.add_argument(
        "--text-encoder-1", default=_env("AUTOTILE_TEXT_ENCODER_1"),
        help="first text-encoder filename (env: AUTOTILE_TEXT_ENCODER_1)",
    )
    group.add_argument(
        "--text-encoder-2", default=_env("AUTOTILE_TEXT_ENCODER_2"),
        help="second text-encoder filename (env: AUTOTILE_TEXT_ENCODER_2)",
    )
    group.add_argument(
        "--dual-clip-type", default=_env("AUTOTILE_DUAL_CLIP_TYPE"),
        help="DualCLIPLoaderGGUF type value (env: AUTOTILE_DUAL_CLIP_TYPE)",
    )
    group.add_argument(
        "--dual-clip-loader-node", default=_env("AUTOTILE_DUAL_CLIP_LOADER_NODE", "DualCLIPLoaderGGUF"),
        help="ComfyUI dual-CLIP-loader node type (env: AUTOTILE_DUAL_CLIP_LOADER_NODE)",
    )
    group.add_argument(
        "--vae-file", default=_env("AUTOTILE_VAE_FILE"),
        help="VAE filename (env: AUTOTILE_VAE_FILE)",
    )
    group.add_argument(
        "--vae-loader-node", default=_env("AUTOTILE_VAE_LOADER_NODE", "VAELoader"),
        help="ComfyUI VAE-loader node type (env: AUTOTILE_VAE_LOADER_NODE)",
    )
    group.add_argument(
        "--inpaint-crop-node", default=_env("AUTOTILE_INPAINT_CROP_NODE", "InpaintCropImproved"),
        help="ComfyUI crop node type (env: AUTOTILE_INPAINT_CROP_NODE)",
    )
    group.add_argument(
        "--inpaint-stitch-node", default=_env("AUTOTILE_INPAINT_STITCH_NODE", "InpaintStitchImproved"),
        help="ComfyUI stitch node type (env: AUTOTILE_INPAINT_STITCH_NODE)",
    )


def require(value: str | Path | None, option: str) -> str | Path:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{option} is required (or set its documented environment variable)")
    return value


@dataclass(frozen=True)
class GatewayImageClient:
    base_url: str
    api_key_env: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "GatewayImageClient":
        return cls(base_url=str(args.gateway_base_url).rstrip("/"), api_key_env=args.gateway_api_key_env)

    def generate(self, *, model: str | None, prompt: str, size: int) -> bytes:
        """Request one image using the documented b64_json response contract."""
        token = os.environ.get(self.api_key_env, "").strip()
        if not token:
            raise RuntimeError(
                f"missing bearer token in ${self.api_key_env}; set that environment variable before a live run"
            )
        if not model or not model.strip():
            raise ValueError("--texture-model (or AUTOTILE_TEXTURE_MODEL) is required")

        import httpx

        response = httpx.post(
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": model, "prompt": prompt, "size": f"{size}x{size}", "n": 1,
                  "response_format": "b64_json"},
            timeout=httpx.Timeout(connect=10, read=300, write=60, pool=None),
        )
        if response.status_code != 200:
            raise RuntimeError(f"image generation failed with HTTP {response.status_code}: {response.text[:300]}")
        try:
            encoded = response.json()["data"][0]["b64_json"]
            return base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("gateway response did not contain data[0].b64_json") from exc


def comfy_cli_args(args: argparse.Namespace) -> list[str]:
    """Return explicit edge_stitch arguments without passing secrets via argv."""
    fields = {
        "--comfy-url": args.comfy_url,
        "--comfy-input-dir": args.comfy_input_dir,
        "--comfy-output-dir": args.comfy_output_dir,
        "--inpaint-model-file": args.inpaint_model_file,
        "--unet-loader-node": args.unet_loader_node,
        "--text-encoder-1": args.text_encoder_1,
        "--text-encoder-2": args.text_encoder_2,
        "--dual-clip-type": args.dual_clip_type,
        "--dual-clip-loader-node": args.dual_clip_loader_node,
        "--vae-file": args.vae_file,
        "--vae-loader-node": args.vae_loader_node,
        "--inpaint-crop-node": args.inpaint_crop_node,
        "--inpaint-stitch-node": args.inpaint_stitch_node,
    }
    return [part for option, value in fields.items() for part in (option, str(require(value, option)))]
