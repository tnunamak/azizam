#!/usr/bin/env bash
# Install the UltraTex texture-refine nodes into a ComfyUI checkout and its venv, with the EGL patch.
# Usage: install.sh <comfyui-dir> <comfyui-venv-python>
# Run it only while the ComfyUI queue is empty; restart ComfyUI after it. Safe to run again.
set -euo pipefail
die() { echo "install.sh: $*" >&2; exit 1; }
[ "$#" -eq 2 ] || die "usage: install.sh <comfyui-dir> <comfyui-venv-python>"
comfy="$1"; python="$2"
here="$(cd "$(dirname "$0")" && pwd)"
pack="$comfy/custom_nodes/ComfyUI-UltraTex"
patch="$here/ultratex-egl.patch"
commit=1f2392308e7d08374cb1c3844afe7e3ee2fadbe0

# Check everything before changing anything.
[ -f "$patch" ] && [ -f "$here/requirements.txt" ] || die "the patch or requirements file is missing beside the script"
[ -d "$comfy/custom_nodes" ] || die "$comfy has no custom_nodes directory; is it a ComfyUI checkout?"
"$python" -c 'import torch' 2>/dev/null || die "$python cannot import torch; pass the ComfyUI venv's python"
if [ -e "$pack" ] && [ ! -e "$pack/.git" ] && [ -n "$(ls -A "$pack")" ]; then
  die "$pack exists and is not a git checkout; move it away, then run again"
fi
if [ -e "$pack/.git" ] && [ -n "$(git -C "$pack" status --porcelain)" ]; then
  # The only local change allowed is our own patch; it comes off just before the checkout below.
  if [ "$(git -C "$pack" status --porcelain)" != " M ultratex_core/render.py" ] \
     || ! git -C "$pack" apply --reverse --check "$patch" 2>/dev/null; then
    die "$pack has local changes other than the EGL patch; commit or discard them, then run again"
  fi
fi

# Existing packages are held at their installed versions, so this adds packages and never changes one. A conflict
# makes pip fail here instead of upgrading something ComfyUI already uses.
constraints="$(mktemp)"
trap 'rm -f "$constraints"' EXIT
"$python" -m pip freeze --exclude-editable > "$constraints"
"$python" -m pip install -c "$constraints" -r "$here/requirements.txt"

[ -e "$pack/.git" ] || git clone https://github.com/visualbruno/ComfyUI-UltraTex "$pack"
git -C "$pack" fetch --quiet origin
# The pack is unpatched only from here to the last line; a failure here puts the patch back on.
if [ -n "$(git -C "$pack" status --porcelain)" ]; then
  git -C "$pack" apply --reverse "$patch"
  trap 'git -C "$pack" apply "$patch" 2>/dev/null; rm -f "$constraints"' EXIT
fi
git -C "$pack" checkout --quiet "$commit"
git -C "$pack" apply "$patch"
trap 'rm -f "$constraints"' EXIT
echo "UltraTex at $commit with the EGL patch"
