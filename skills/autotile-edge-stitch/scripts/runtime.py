"""Explicit runtime configuration shared by the autotile build scripts.

This module deliberately knows only the public OpenAI Images request/response
shape and the configurable ComfyUI HTTP/file interfaces. Model IDs, workflow
node names, model files, endpoints, and directories are operator-supplied
data; no gateway or inference-engine selection lives in the scripts.
"""
from __future__ import annotations

import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_sys.path.insert(0, str(_CompatPath(__file__).resolve().parents[3]))
from gateway.compat import get_env

import argparse
import base64
import os
import ipaddress
import unicodedata
from urllib.parse import unquote, urlparse
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath


def _env(name: str, default: str | None = None) -> str | None:
    value = get_env(name)
    return value if value else default


def add_gateway_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("gateway image generation")
    group.add_argument(
        "--gateway-base-url",
        default=_env("AUTOTILE_GATEWAY_BASE_URL"),
        help="OpenAI-compatible API base URL (env: AUTOTILE_GATEWAY_BASE_URL)",
    )
    group.add_argument(
        "--gateway-api-key-env",
        default=_env("AUTOTILE_GATEWAY_API_KEY_ENV", "AZIZAM_API_KEY"),
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


def safe_spec_id(value: object, label: str) -> str:
    """Accept only portable, single-component IDs suitable for filenames."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"{label} must be a non-empty portable identifier")
    if any(ch in value for ch in '<>:"/\\|?*') or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{label} must not contain path separators or control characters")
    if value[-1] in ". ":
        raise ValueError(f"{label} must not end with a dot or space")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if value.split(".", 1)[0].casefold() in reserved:
        raise ValueError(f"{label} must not use a Windows reserved filename")
    return value


def spec_id_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def contained_path(root: Path, *parts: str) -> Path:
    root = root.resolve()
    candidate = (root.joinpath(*parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"resolved output path escapes output root: {candidate}")
    return candidate


def comfy_output_path(root: Path, server_filename: object) -> Path:
    """Resolve a Comfy history filename under the configured output root.

    Comfy returns a filename, but the value is endpoint-controlled data.  The
    API permits a flat or nested relative filename; it does not permit a path
    root, traversal component, Windows path syntax, or encoded traversal.
    """
    if not isinstance(server_filename, str) or not server_filename:
        raise ValueError("Comfy response filename must be a non-empty relative path")
    decoded = server_filename
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if "\\" in decoded or "\x00" in decoded:
        raise ValueError("Comfy response filename must use POSIX relative path syntax")
    path = Path(decoded)
    if path.is_absolute() or PureWindowsPath(decoded).is_absolute() or PureWindowsPath(decoded).drive:
        raise ValueError("Comfy response filename must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Comfy response filename must not contain traversal components")
    return contained_path(root, *path.parts)


def validate_gateway_url(base_url: str, *, bearer: bool) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("gateway base URL must be an absolute http:// or https:// URL")
    if bearer and parsed.scheme == "http":
        try:
            host = ipaddress.ip_address(parsed.hostname or "")
            loopback = host.is_loopback
        except ValueError:
            loopback = parsed.hostname in {"localhost", "localhost."}
        if not loopback:
            raise ValueError("refusing to send bearer credentials over non-loopback HTTP")
    return base_url.rstrip("/")


@dataclass(frozen=True)
class GatewayImageClient:
    base_url: str
    api_key_env: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "GatewayImageClient":
        return cls(base_url=str(args.gateway_base_url).rstrip("/"), api_key_env=args.gateway_api_key_env)

    def generate(self, *, model: str | None, prompt: str, size: int) -> bytes:
        """Request one image using the documented b64_json response contract."""
        token = get_env(self.api_key_env, "").strip()
        if not token:
            raise RuntimeError(
                f"missing bearer token in ${self.api_key_env}; set that environment variable before a live run"
            )
        if not model or not model.strip():
            raise ValueError("--texture-model (or AUTOTILE_TEXTURE_MODEL) is required")
        url = validate_gateway_url(self.base_url, bearer=True)

        import httpx

        response = httpx.post(
            f"{url}/images/generations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": model, "prompt": prompt, "size": f"{size}x{size}", "n": 1,
                  "response_format": "b64_json"},
            timeout=httpx.Timeout(connect=10, read=300, write=60, pool=None),
        )
        if response.status_code != 200:
            raise RuntimeError(f"image generation failed with HTTP {response.status_code}")
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
