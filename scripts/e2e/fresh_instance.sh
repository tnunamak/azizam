#!/usr/bin/env bash
# Executable FIRST_RUN_SETUP.md: no existing DB, config, keys, or engines.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
exec "${FRESH_INSTANCE_PYTHON:-$repo_root/venv/bin/python}" "$repo_root/tests/e2e/fresh_instance.py" "$@"
