#!/usr/bin/env bash

# Run pytest from an exact Git tree with no operator filesystem, environment,
# network, processes, IPC, terminal, or service manager access.
set -euo pipefail
[[ -n ${AZIZAM_PRIORITY_SET:-} ]] || exec "${BASH_SOURCE[0]%/*}/low-priority" "${BASH_SOURCE[0]}" "$@"

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/scripts/lib/scratch.sh"
state_root="$HOME/.tmp"
lock_file="$repo_root/tests/requirements-hermetic.lock"
python_store="$state_root/azizam-hermetic-python"
python_abi=cpython-312
python_minor=3.12
python_version=3.12.13
platform=linux-x86_64
python_root="$python_store/cpython-$python_version-linux-x86_64-gnu"
created_cache_mount_dir=0

# --junit-xml-out PATH: pytest writes JUnit XML inside the sandbox, which
# cannot write to the host; the runner copies it to PATH afterwards, also
# when tests fail. An existing PATH is refused, so a second run cannot
# replace the first run's report.
junit_out=
if [[ ${1:-} == "--junit-xml-out" ]]; then
  (($# >= 2)) || { echo "error: --junit-xml-out requires a path" >&2; exit 2; }
  junit_out=$2; shift 2
  [[ ! -e $junit_out ]] || { echo "error: --junit-xml-out $junit_out already exists" >&2; exit 2; }
fi

for command in bwrap git ip sha256sum tar uv; do
  command -v "$command" >/dev/null 2>&1 || { echo "error: $command is required for hermetic pytest" >&2; exit 127; }
done
for file in "$lock_file" "$repo_root/tests/requirements-hermetic.in" "$repo_root/requirements.txt" "$repo_root/proxy_config.yml"; do
  [[ -f $file ]] || { echo "error: required hermetic test input is missing: $file" >&2; exit 127; }
done
"$repo_root/scripts/hermetic_source_preflight.sh" "$repo_root" "$lock_file" "$repo_root/requirements.txt" "$repo_root/tests/requirements-hermetic.in"

if [[ ! -x $python_root/bin/python3.12 ]]; then
  echo "hermetic pytest: reinstalling Python $python_version in $python_store" >&2
  env -i HOME="$state_root/hermetic-uv-home" PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" UV_CACHE_DIR="$state_root/hermetic-uv-cache" UV_NO_CONFIG=1 uv python install "$python_version" --install-dir "$python_store"
fi
if [[ $("$python_root/bin/python3.12" --version) != "Python $python_version" ]]; then
  echo "error: hermetic pytest requires Python $python_version at $python_root/bin/python3.12" >&2
  exit 1
fi

lock_sha256=$(sha256sum "$lock_file" | awk '{print $1}')
manifest_sha256=$(sha256sum "$repo_root/requirements.txt" | awk '{print $1}')
input_sha256=$(sha256sum "$repo_root/tests/requirements-hermetic.in" | awk '{print $1}')
checkout_head=$(git -C "$repo_root" rev-parse HEAD)

mkdir -p -- "$state_root"
# Name each allocation after this runner (pid + start time) so a later run can
# sweep what a SIGKILLed one left: an EXIT trap does not run on SIGKILL.
read -r -a own_stat < <(sed 's/^.*) //' "/proc/$$/stat")  # start time is field 20 after "(comm)"
owner_tag="$$-${own_stat[19]}"
"$repo_root/scripts/sweep-stale-pytest-tmpfs.sh" "$state_root" 2>/dev/null || true
state_dir=$(mktemp -d "$state_root/azizam-pytest.$owner_tag.XXXXXXXX")
tmpfs_tmp=
tmpfs_fallback=
bwrap_pid=
cleanup() {
  trap '' HUP INT TERM
  # bwrap runs in the background (below), so a signal reaches this trap at once and not after the tests end.
  if [[ -n $bwrap_pid ]]; then
    kill -TERM -- "$bwrap_pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 -- "$bwrap_pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "$bwrap_pid" 2>/dev/null || true
    wait "$bwrap_pid" 2>/dev/null || true
  fi
  rm -rf -- "$state_dir" ${tmpfs_tmp:+"$tmpfs_tmp"}
  if ((created_cache_mount_dir)); then rmdir -- "$repo_root/cache"; fi
}
trap cleanup EXIT
scratch_on_signal
checkout_root="$state_dir/checkout"
mkdir -p -- "$checkout_root"
git -C "$repo_root" archive --format=tar HEAD | tar -x -C "$checkout_root"
mkdir -- "$checkout_root/venv" "$checkout_root/cache"
cp -- "$checkout_root/proxy_config.yml" "$state_dir/proxy_config.yml"
fresh_env="$state_dir/fresh-venv"
env -i HOME="$state_root/hermetic-uv-home" PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" UV_CACHE_DIR="$state_root/hermetic-uv-cache" UV_NO_CONFIG=1 uv venv --no-project --no-managed-python --no-python-downloads --python "$python_root/bin/python3.12" "$fresh_env" >/dev/null
env -i HOME="$state_root/hermetic-uv-home" PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" UV_CACHE_DIR="$state_root/hermetic-uv-cache" UV_NO_CONFIG=1 uv pip sync --offline --no-build --strict --require-hashes --link-mode copy --python "$fresh_env/bin/python" "$lock_file" >/dev/null
find "$fresh_env" -type f -name '*.pth' -delete
mkdir -p -- "$state_dir"/{tmp,cache,pycache,home,disk}

# pytest's TMPDIR (tmp_path, fixture databases) is RAM: a SQLite commit on the
# host disk costs ~71 ms of fsync, which made the tier 7x slower. Tests that need
# real-disk semantics take the host_disk_dir fixture, bound from $state_dir/disk.
# If tmpfs is missing or too small the run keeps TMPDIR on disk and says so.
tmpfs_root=${AZIZAM_PYTEST_TMPFS_ROOT:-/dev/shm}
tmpfs_min_kib=${AZIZAM_PYTEST_TMPFS_MIN_KIB:-4194304}
# A SIGKILLed runner leaves its allocation behind (no trap runs), so sweep dead owners first, before measuring free space.
"$repo_root/scripts/sweep-stale-pytest-tmpfs.sh" "$tmpfs_root" 2>/dev/null || true
tmpfs_free_kib=$(df --output=avail -k -- "$tmpfs_root" 2>/dev/null | tail -n 1 | tr -d ' ' || true)
if [[ $(stat -f -c %T -- "$tmpfs_root" 2>/dev/null || true) == tmpfs && ${tmpfs_free_kib:-0} -ge $tmpfs_min_kib ]] \
   && tmpfs_tmp=$(mktemp -d "$tmpfs_root/azizam-pytest.$owner_tag.XXXXXXXX" 2>/dev/null); then
  tmpfs_source=$tmpfs_tmp
  echo "pytest TMPDIR: tmpfs ($tmpfs_source)" >&2
else
  tmpfs_tmp=
  tmpfs_fallback=1
  tmpfs_source=$state_dir/tmp
  echo "pytest TMPDIR: host disk (no tmpfs with ${tmpfs_min_kib} KiB free at $tmpfs_root); the tier will be slow" >&2
fi

host_runner_pid=$$
host_pid_namespace=$(readlink /proc/self/ns/pid)
host_ipc_namespace=$(readlink /proc/self/ns/ipc)
forbidden_production_path="$(dirname -- "$repo_root")/azizam"
python_bin="/venv/bin/python"

declare -a bwrap_args=(
  --die-with-parent --new-session --unshare-user --uid 0 --gid 0
  --disable-userns --assert-userns-disabled --unshare-net --unshare-pid --unshare-ipc
  --clearenv --tmpfs / --proc /proc --dev /dev
  --dir /usr --dir /etc --dir /home --dir /var --dir /run --dir /run/user --dir /run/user/0
  --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink /run /var/run
  --setenv HOME /home/test --setenv PATH /usr/bin:/bin --setenv LANG C.UTF-8
  --setenv TMPDIR "$state_dir/tmp" --setenv TMP "$state_dir/tmp" --setenv TEMP "$state_dir/tmp"
  --setenv XDG_CACHE_HOME "$state_dir/cache" --setenv XDG_CONFIG_HOME "$state_dir/home/.config" --setenv XDG_RUNTIME_DIR /run/user/0
  --setenv PYTHONNOUSERSITE 1 --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTHONPYCACHEPREFIX "$state_dir/pycache" --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1
  --setenv HERMETIC_TEST_STATE_DIR "$state_dir" --setenv HERMETIC_HOST_DISK_DIR "$state_dir/disk" --setenv HERMETIC_TMPDIR_ON_DISK "$tmpfs_fallback" --setenv AZIZAM_CONFIG_PATH "$state_dir/proxy_config.yml"
  --setenv HERMETIC_ADMIN_DB "$state_dir/admin.db"
  --setenv HYPOTHESIS_PROFILE "${HYPOTHESIS_PROFILE:-gate}"
  --setenv HERMETIC_TEST_NAMESPACE 1 --setenv HERMETIC_STATE_DIR "$state_dir"
  --setenv HERMETIC_FORBIDDEN_PRODUCTION_PATH "$forbidden_production_path"
  --setenv HERMETIC_HOST_RUNNER_PID "$host_runner_pid"
  --setenv HERMETIC_HOST_PID_NAMESPACE "$host_pid_namespace" --setenv HERMETIC_HOST_IPC_NAMESPACE "$host_ipc_namespace"
  --setenv HERMETIC_TEST_ENV_ROOT /venv --setenv HERMETIC_REPO_ROOT "$checkout_root"
  --setenv HERMETIC_PYTHON_ROOT "$python_root"
)

declare -A sandbox_dirs=(['/usr']=1 ['/etc']=1 ['/home']=1 ['/var']=1 ['/run']=1 ['/run/user']=1 ['/run/user/0']=1)
add_parent_dirs() {
  local target=$1 parent= parents=()
  parent=$(dirname -- "$target")
  while [[ $parent != / ]]; do parents+=("$parent"); parent=$(dirname -- "$parent"); done
  for ((index=${#parents[@]} - 1; index >= 0; index--)); do
    parent=${parents[index]}
    if [[ ! ${sandbox_dirs[$parent]+present} ]]; then bwrap_args+=(--dir "$parent"); sandbox_dirs[$parent]=1; fi
  done
}
add_read_only_bind() {
  local source=$1 target=$2
  [[ -e $source ]] || return 0
  add_parent_dirs "$target"
  bwrap_args+=(--ro-bind "$source" "$target")
}
for system_path in /usr/bin /usr/sbin /usr/lib /usr/lib64 /usr/libexec /usr/share/terminfo /usr/share/zoneinfo; do add_read_only_bind "$system_path" "$system_path"; done
for config_path in /etc/ld.so.cache /etc/ld.so.conf /etc/ld.so.conf.d /etc/passwd /etc/group /etc/nsswitch.conf /etc/hosts /etc/localtime /etc/mime.types /etc/os-release /etc/debian_version /etc/ssl /etc/ca-certificates /etc/ca-certificates.conf; do add_read_only_bind "$config_path" "$config_path"; done
add_read_only_bind "$python_root" "$python_root"
add_parent_dirs "$state_dir"
for writable in cache pycache home disk; do
  bwrap_args+=(--bind "$state_dir/$writable" "$state_dir/$writable")
done
bwrap_args+=(--bind "$tmpfs_source" "$state_dir/tmp")
bwrap_args+=(--ro-bind "$state_dir/proxy_config.yml" "$state_dir/proxy_config.yml")
add_read_only_bind "$checkout_root" "$checkout_root"
add_read_only_bind "$fresh_env" /venv
bwrap_args+=(--bind "$state_dir/cache" "$checkout_root/cache")

namespace_command='if ! ip -o link show dev lo | grep -q "UP"; then echo "error: namespace has no loopback" >&2; exit 1; fi
if [[ $(ip -o link show | wc -l) -ne 1 ]] || tail -n +2 /proc/net/route | grep -q .; then echo "error: namespace exposes host interfaces or routes" >&2; exit 1; fi
read -r _ _ _ _ _ _ tty_number _ < /proc/self/stat
if [[ $tty_number != 0 ]] || ( : </dev/tty ) 2>/dev/null; then echo "error: sandbox retained a controlling terminal" >&2; exit 1; fi
if [[ -e "/proc/$HERMETIC_HOST_RUNNER_PID" ]] || [[ $(readlink /proc/1/ns/pid) == "$HERMETIC_HOST_PID_NAMESPACE" ]] || [[ $(readlink /proc/1/ns/ipc) == "$HERMETIC_HOST_IPC_NAMESPACE" ]]; then echo "error: sandbox exposes host process or IPC state" >&2; exit 1; fi
if [[ -e "$HERMETIC_FORBIDDEN_PRODUCTION_PATH" ]] || [[ -e /run/docker.sock ]] || [[ -e /var/run/docker.sock ]]; then echo "error: sandbox exposes forbidden production or Unix-socket path" >&2; exit 1; fi
exec "$@"'

if [[ -n $junit_out ]]; then set -- --junitxml="$state_dir/tmp/junit.xml" "$@"; fi
if [[ ${1:-} == "--python-script" ]]; then
  # Test tooling that drives pytest itself (scripts/mutate) runs as one
  # repo-relative Python file inside the same namespace.
  (($# >= 2)) && [[ $2 == scripts/*.py && -f $checkout_root/$2 ]] || { echo "error: --python-script needs a scripts/*.py file" >&2; exit 2; }
  shift
  test_command=("$python_bin" "$@")
elif [[ ${1:-} == "--check" ]]; then
  shift; (($# == 0)) || { echo "error: --check does not accept pytest arguments" >&2; exit 2; }
  test_command=("$python_bin" -S -c 'import os, sys; assert sys.executable; assert not os.environ.get("OPENAI_API_KEY"); assert not os.environ.get("AZIZAM_ADMIN_DB")')
else
  # Plugin autoload is off, so load pytest-xdist explicitly; `-n N` opts in.
  test_command=("$python_bin" -m pytest -p no:cacheprovider -p xdist "$@")
  # A full xdist run (`-n N`, no path argument) first compiles the suite once in N parallel
  # slices (scripts/hermetic_prewarm.py) so the workers do not each rewrite every test module.
  # AZIZAM_PYTEST_PREWARM=0 turns it off.
  workers=
  for argument in "$@"; do
    if [[ $workers == next ]]; then workers=$argument; continue; fi
    case $argument in
      -n|--numprocesses) workers=next ;;
      -n[0-9]*) workers=${argument#-n} ;;
      --numprocesses=*) workers=${argument#--numprocesses=} ;;
      -*) ;;
      *) [[ -e $checkout_root/${argument%%::*} ]] && workers=skip ;;
    esac
  done
  if [[ ${AZIZAM_PYTEST_PREWARM:-1} != 0 && $workers =~ ^[0-9]+$ ]] && ((workers > 1)); then
    bwrap_args+=(--setenv HERMETIC_PREWARM_SLICES "$workers")
    test_command=(/bin/bash -c '"$0" scripts/hermetic_prewarm.py "$HERMETIC_PREWARM_SLICES"; exec "$@"' "$python_bin" "${test_command[@]}")
  fi
fi
rc=0
bwrap "${bwrap_args[@]}" --chdir "$checkout_root" -- /bin/bash -ceu "$namespace_command" bash "${test_command[@]}" <&0 &
bwrap_pid=$!
wait "$bwrap_pid" || rc=$?
bwrap_pid=
if [[ -n $junit_out ]]; then
  if [[ -e $junit_out ]]; then
    echo "error: $junit_out appeared during the run; not replacing it" >&2; ((rc)) || rc=1
  elif ! cp -- "${tmpfs_tmp:-$state_dir/tmp}/junit.xml" "$junit_out"; then
    echo "error: no JUnit XML copied to $junit_out" >&2; ((rc)) || rc=1
  fi
fi
exit "$rc"
