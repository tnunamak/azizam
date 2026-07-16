#!/usr/bin/env bash
# Build the only dependency environment accepted by run-hermetic-pytest.sh.
# This is intentionally separate from test execution: the release runner never
# uses a package index or a sibling checkout.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$repo_root"
state_root="${HOME}/.tmp"
lock_file="$repo_root/tests/requirements-hermetic.lock"
input_file="$repo_root/tests/requirements-hermetic.in"
uv_bin=$(command -v uv || true)
python_spec="3.12.13"

for command in sha256sum mktemp mv mkdir; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "error: $command is required to provision hermetic pytest" >&2
    exit 127
  }
done
if [[ -z $uv_bin || ! -x $uv_bin ]]; then
  echo "error: uv is required to provision hermetic pytest" >&2
  exit 127
fi
for file in "$lock_file" "$input_file" "$repo_root/requirements.txt"; do
  [[ -f $file ]] || { echo "error: required dependency input is missing: $file" >&2; exit 127; }
done

lock_sha256=$(sha256sum "$lock_file" | awk '{print $1}')
manifest_sha256=$(sha256sum "$repo_root/requirements.txt" | awk '{print $1}')
input_sha256=$(sha256sum "$input_file" | awk '{print $1}')
python_store="$state_root/vivid-fish-hermetic-python"
uv_cache="$state_root/vivid-fish-hermetic-uv-cache"
uv_home="$state_root/vivid-fish-hermetic-uv-home"
mkdir -p -- "$python_store" "$uv_cache" "$uv_home"

run_uv() {
  env -i \
    HOME="$uv_home" \
    PATH="$(dirname -- "$uv_bin"):/usr/bin:/bin" \
    LANG=C.UTF-8 \
    UV_CACHE_DIR="$uv_cache" \
    UV_NO_CONFIG=1 \
    "$uv_bin" --no-config "$@"
}

shopt -s nullglob
python_candidates=("$python_store"/cpython-"$python_spec"-*/bin/python3.12)
if ((${#python_candidates[@]} == 0)); then
  run_uv python install --install-dir "$python_store" "$python_spec"
  python_candidates=("$python_store"/cpython-"$python_spec"-*/bin/python3.12)
fi
if ((${#python_candidates[@]} != 1)) || [[ ! -x ${python_candidates[0]:-} ]]; then
  echo "error: expected one hermetic CPython $python_spec runtime below $python_store" >&2
  exit 127
fi
python_bin=${python_candidates[0]}
python_root=$(cd -- "$(dirname -- "$python_bin")/.." && pwd -P)
python_abi=$("$python_bin" -c 'import sys; print(sys.implementation.cache_tag)')
platform=$("$python_bin" -c 'import sysconfig; print(sysconfig.get_platform().replace("/", "_"))')
environment_root="$state_root/vivid-fish-hermetic-environments/${lock_sha256}-${python_abi}-${platform}"
validate_existing() {
  [[ -x "$environment_root/venv/bin/python" ]] || return 1
  "$python_bin" -S "$repo_root/scripts/hermetic_preflight.py" \
    --environment-root "$environment_root/venv" --python-root "$python_root" \
    --requirements "$repo_root/requirements.txt" --input-manifest "$input_file" \
    --lock "$lock_file" --python-minor 3.12
}

if validate_existing; then
  printf '%s\n' "$environment_root"
  exit 0
fi

mkdir -p -- "$(dirname -- "$environment_root")"
build_root=$(mktemp -d "$(dirname -- "$environment_root")/.build.XXXXXXXX")
cleanup() { rm -rf -- "$build_root"; }
trap cleanup EXIT HUP INT TERM

run_uv venv --no-project --no-managed-python --no-python-downloads --python "$python_bin" "$build_root/venv"
run_uv pip sync --strict --require-hashes --link-mode copy --python "$build_root/venv/bin/python" "$lock_file"
# uv's virtualenv marker is an import-executing .pth file. The runtime does not
# need it, and a release oracle admits no .pth import hooks at all.
find "$build_root/venv" -type f -name '*.pth' -delete

rm -rf -- "$environment_root"
mv -- "$build_root" "$environment_root"
trap - EXIT HUP INT TERM
if ! validate_existing; then
  rm -rf -- "$environment_root"
  echo "error: provisioned hermetic environment failed import-root validation" >&2
  exit 127
fi
printf '%s\n' "$environment_root"
