#!/usr/bin/env bash
# Install the UltraTex texture-refine nodes into a ComfyUI checkout and its venv, with the EGL patch.
# Usage: install.sh <comfyui-dir> <comfyui-venv-python>
# Run it only while the ComfyUI queue is empty; restart ComfyUI after it.
set -euo pipefail
comfy="$1"; python="$2"
here="$(cd "$(dirname "$0")" && pwd)"
pack="$comfy/custom_nodes/ComfyUI-UltraTex"
commit=1f2392308e7d08374cb1c3844afe7e3ee2fadbe0

"$python" -m pip install --dry-run -r "$here/requirements.txt" | grep -E "^Would install" || true
"$python" -m pip install -r "$here/requirements.txt"

if [ ! -d "$pack/.git" ]; then
  git clone https://github.com/visualbruno/ComfyUI-UltraTex "$pack"
fi
git -C "$pack" fetch --quiet origin
git -C "$pack" checkout --quiet "$commit"
if git -C "$pack" apply --reverse --check "$here/ultratex-egl.patch" 2>/dev/null; then
  echo "EGL patch already applied"
else
  git -C "$pack" apply "$here/ultratex-egl.patch"
fi
