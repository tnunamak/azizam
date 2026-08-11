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
inpaint pass.  Live mode requires AUTOTILE_TEXTURE_MODEL, AI_GATEWAY_API_KEY
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
from edge_stitch import hard_post_composite
from runtime import GatewayImageClient

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
    base_url=os.environ.get("AUTOTILE_GATEWAY_BASE_URL", "http://127.0.0.1:5000/openai/v1").rstrip("/"),
    api_key_env=os.environ.get("AUTOTILE_GATEWAY_API_KEY_ENV", "AI_GATEWAY_API_KEY"),
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
