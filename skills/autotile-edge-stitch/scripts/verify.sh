#!/usr/bin/env bash
# Deterministic verifier for the source-only autotile skill.
#
# Default mode never contacts a gateway or ComfyUI.  `--live` is an explicit
# opt-in smoke test that uses the same environment-based configuration as the
# builders; it never reads a token file or interpolates a token into source.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: verify.sh [--live]

Without --live, validates composition and byte-exact post-compositing using
generated fixture pixels only.  With --live, additionally requests one image
from the configured OpenAI-compatible gateway and runs one configured ComfyUI
inpaint pass.  Live mode requires AUTOTILE_TEXTURE_MODEL, AZIZAM_API_KEY
(or the variable named by AUTOTILE_GATEWAY_API_KEY_ENV), and the AUTOTILE_COMFY_*
and model-file variables documented in ../README.md.
EOF
}

live=false
case "${1:-}" in
    "") ;;
    --live) live=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
WORK_ROOT="${AUTOTILE_TEMP_DIR:-$HOME/.tmp}"
mkdir -p -- "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/autotile-edge-stitch-verify.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

echo "[verify] deterministic offline checks"
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$WORK" <<'PY'
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from build_dualgrid import composite_tile, make_spritesheet
from build_direct import generate_tile
from build_layered import gen_corner_transition
from build_set import main as build_set_main
from edge_stitch import hard_post_composite
from runtime import (GatewayImageClient, comfy_output_path, contained_path,
                     safe_spec_id, spec_id_key)

work = Path(sys.argv[1])
size = 8
reference = np.arange(size * size * 3, dtype=np.uint8).reshape(size, size, 3)
inpainted = np.full((size, size, 3), 17, dtype=np.uint8)
mask = np.zeros((size, size), dtype=np.uint8)
mask[:, size // 2 :] = 255
Image.fromarray(reference).save(work / "reference.png")
Image.fromarray(inpainted).save(work / "inpainted.png")
Image.fromarray(mask).save(work / "mask.png")
max_diff, drift_count = hard_post_composite(
    work / "reference.png", work / "inpainted.png", work / "mask.png", work / "composited.png"
)
composited = np.array(Image.open(work / "composited.png").convert("RGB"))
assert np.array_equal(composited[:, : size // 2], reference[:, : size // 2])
assert np.array_equal(composited[:, size // 2 :], inpainted[:, size // 2 :])
assert max_diff > 0 and drift_count == size * (size // 2)
rgba_reference = np.dstack([reference, np.full((size, size), 91, dtype=np.uint8)])
Image.fromarray(rgba_reference).save(work / "rgba-reference.png")
Image.fromarray(np.zeros_like(rgba_reference)).save(work / "rgba-inpainted.png")
Image.fromarray(mask).save(work / "rgba-mask.png")
hard_post_composite(work / "rgba-reference.png", work / "rgba-inpainted.png",
                   work / "rgba-mask.png", work / "rgba-out.png")
rgba_out = Image.open(work / "rgba-out.png")
assert rgba_out.mode == "RGBA"
assert np.array_equal(np.array(rgba_out)[:, : size // 2], rgba_reference[:, : size // 2])

# Every accepted single-channel/color PNG mode must preserve its reference pixels.
for mode, values in (("L", np.arange(size * size, dtype=np.uint8).reshape(size, size)),
                     ("P", np.arange(size * size, dtype=np.uint8).reshape(size, size)),
                     ("RGB", reference), ("RGBA", rgba_reference)):
    ref_path = work / f"mode-{mode}-reference.png"
    out_path = work / f"mode-{mode}-inpainted.png"
    result_path = work / f"mode-{mode}-result.png"
    ref_image = Image.fromarray(values, mode=mode)
    if mode == "P":
        ref_image.putpalette(bytes(range(256)) * 3)
    ref_image.save(ref_path)
    Image.fromarray(np.zeros_like(values), mode=mode).save(out_path)
    hard_post_composite(ref_path, out_path, work / "mask.png", result_path)
    result = Image.open(result_path)
    assert result.mode == mode
    assert np.array_equal(np.array(result)[:, : size // 2], values[:, : size // 2])

# Distinct palette values make every quadrant placement and all 16 bit patterns
# observable without any diffusion runtime.
palette = {}
for index, name in enumerate(("filled", "empty", "trans_TL", "trans_TR", "trans_BL", "trans_BR"), start=1):
    palette[name] = np.full((size, size, 3), index, dtype=np.uint8)
tiles = [composite_tile(palette, bits, size) for bits in range(16)]
assert np.all(tiles[0] == 2)
assert np.all(tiles[15] == 1)
assert np.all(tiles[1][:4, :4] == 3)  # TL filled against BR empty.
assert np.all(tiles[1][4:, 4:] == 3)  # BR sees the same TL-filled transition.
sheet = make_spritesheet(tiles, size)
assert sheet.size == (size * 4, size * 4)
for bad in ("..", "../escape", "a/b", "a\\b", "/absolute", "C:drive", "CON", "aux.txt",
            "Lpt9", "bad*", "bad?", "bad<", "bad>", "bad|", "bad\"", "bad.", "bad "):
    try: safe_spec_id(bad, "tile id")
    except ValueError: pass
    else: raise AssertionError(f"accepted unsafe id: {bad}")
try: contained_path(work, "..", "escape.png")
except ValueError: pass
else: raise AssertionError("allowed output path escape")
assert spec_id_key("Foo") == spec_id_key("foo")
assert spec_id_key("e\u0301") == spec_id_key("é")
for bad in ("../../outside.png", "/outside.png", "..\\outside.png",
            "%2e%2e%2foutside.png", "%252e%252e%252foutside.png"):
    try: comfy_output_path(work, bad)
    except ValueError: pass
    else: raise AssertionError(f"accepted unsafe Comfy filename: {bad}")
assert comfy_output_path(work, "nested/result.png") == (work / "nested/result.png").resolve()
outside = work.parent / (work.name + "-outside")
outside.mkdir()
(work / "link").symlink_to(outside, target_is_directory=True)
try: comfy_output_path(work, "link/secret.png")
except ValueError: pass
else: raise AssertionError("accepted symlink/traversal Comfy filename")

# Build-set ID validation is a true preflight: a duplicate must fail before
# required backend config is resolved or the output directory is created.
duplicate_spec = work / "duplicate.json"
duplicate_spec.write_text('{"tiles": [{"id": "Foo"}, {"id": "foo"}]}')
duplicate_out = work / "duplicate-output"
with patch.object(sys, "argv", ["build_set.py", "--spec", str(duplicate_spec),
                                  "--out-dir", str(duplicate_out), "--seed-model", "fixture"]):
    try: build_set_main()
    except SystemExit as exc:
        assert exc.code == 2
    else: raise AssertionError("duplicate build-set IDs were accepted")
assert not duplicate_out.exists()

# Launch failures must not strand generated Comfy input files.
rgb = np.zeros((size, size, 3), dtype=np.uint8)
with patch("build_direct.subprocess.run", side_effect=OSError("launch failed")):
    try:
        generate_tile(1, size, "fixture", 1, 1, rgb, rgb, work / "direct.png", work, [])
    except OSError:
        pass
    else: raise AssertionError("expected direct launch failure")
assert not list(work.glob("_directtile_*.canvas.png"))
assert not list(work.glob("_directtile_*.mask.png"))
with patch("build_layered.subprocess.run", side_effect=OSError("launch failed")):
    try:
        gen_corner_transition(filled_img=work / "reference.png", empty_img=work / "inpainted.png",
                              filled_corner="NW", prompt="fixture", size=size, out_path=work / "layered.png",
                              seed=1, steps=1, comfy_input=work, edge_config=[])
    except OSError:
        pass
    else: raise AssertionError("expected layered launch failure")
assert not list(work.glob("_layered_corner_*.canvas.png"))
assert not list(work.glob("_layered_corner_*.mask.png"))

# The client boundary must stay on the documented OpenAI Images API shape and
# must not follow arbitrary response URLs with the bearer token.
class Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        encoded = base64.b64encode(b"fixture-png").decode("ascii")
        return {"data": [{"b64_json": encoded}]}

os.environ["AUTOTILE_VERIFY_KEY"] = "fixture-only-not-a-secret"
with patch("httpx.post", return_value=Response()) as post:
    encoded = GatewayImageClient("https://gateway.example/openai/v1", "AUTOTILE_VERIFY_KEY").generate(
        model="operator-model", prompt="fixture", size=64
    )
assert encoded == b"fixture-png"
assert post.call_args.args[0] == "https://gateway.example/openai/v1/images/generations"
assert post.call_args.kwargs["json"]["response_format"] == "b64_json"
print("  PASS: post-composite preservation, 16-case lookup, atlas layout, and Images API contract")
for url in ("http://gateway.example/openai/v1", "http://[2001:db8::1]/openai/v1"):
    try: GatewayImageClient(url, "AUTOTILE_VERIFY_KEY").generate(model="m", prompt="p", size=8)
    except ValueError: pass
    else: raise AssertionError(f"sent bearer to unsafe URL: {url}")
class ErrorResponse:
    status_code = 401
    text = "Bearer fixture-only-not-a-secret"
with patch("httpx.post", return_value=ErrorResponse()):
    try: GatewayImageClient("https://gateway.example/openai/v1", "AUTOTILE_VERIFY_KEY").generate(model="m", prompt="p", size=8)
    except RuntimeError as exc:
        assert "fixture-only-not-a-secret" not in str(exc)
    else: raise AssertionError("expected mocked HTTP error")
PY

if ! "$live"; then
    echo "[verify] PASS (offline only; use --live for an optional inference smoke test)"
    exit 0
fi

echo "[verify] optional live gateway + ComfyUI smoke test"
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$WORK" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

from runtime import GatewayImageClient

work = Path(sys.argv[1])
model = os.environ.get("AUTOTILE_TEXTURE_MODEL", "").strip()
if not model:
    raise SystemExit("AUTOTILE_TEXTURE_MODEL is required for --live")
client = GatewayImageClient(
    base_url=os.environ["AUTOTILE_GATEWAY_BASE_URL"].rstrip("/"),
    api_key_env=os.environ.get("AUTOTILE_GATEWAY_API_KEY_ENV", "AZIZAM_API_KEY"),
)
(work / "reference.png").write_bytes(client.generate(
    model=model,
    prompt="hand-painted grey stone, top-down game texture, tileable",
    size=512,
))
PY

PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$WORK" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

work = Path(sys.argv[1])
image = Image.open(work / "reference.png").convert("RGB")
width, height = image.size
image.save(work / "canvas.png")
mask = Image.new("L", (width, height), 0)
ImageDraw.Draw(mask).rectangle([width // 2, 0, width, height], fill=255)
mask.save(work / "mask.png")
PY

"$PYTHON_BIN" "$SCRIPT_DIR/edge_stitch.py" \
    --reference "$WORK/canvas.png" \
    --mask "$WORK/mask.png" \
    --prompt "hand-painted grey stone, top-down game texture, tileable" \
    --output "$WORK/output.png" \
    --seed 42 \
    --steps 20

PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$WORK" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

work = Path(sys.argv[1])
reference = np.array(Image.open(work / "reference.png").convert("RGB"))
output = np.array(Image.open(work / "output.png").convert("RGB"))
assert reference.shape == output.shape
assert np.array_equal(reference[:, : reference.shape[1] // 2], output[:, : output.shape[1] // 2])
print("  PASS: live preserve region is byte-exact after post-composite")
PY

echo "[verify] PASS (offline + live)"
