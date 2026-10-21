"""Pinned no-content launcher for Azizam's LiteLLM execution substrate.

This file is also mounted alone in the shipped Compose image. Never edit the
installed package. Re-verify the version and function fingerprint on upgrades.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import os
import sys
from pathlib import Path

SUPPORTED_VERSION = "1.86.2"
QUEUE_SOURCE_SHA256 = "0f760b1f54019de072223ac6799427e39f4291c6a29dfc20cbaad50063977c43"


class UnsupportedContentProfile(RuntimeError):
    pass


def install_no_content_shim() -> None:
    """Suppress only the unused ToolTable discovery queue; preserve accounting."""
    if importlib.metadata.version("litellm") != SUPPORTED_VERSION:
        raise UnsupportedContentProfile("LiteLLM version changed; re-verify the no-content shim before starting.")
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter

    target = getattr(DBSpendUpdateWriter, "_enqueue_tool_registry_upsert", None)
    if not inspect.isfunction(target):
        raise UnsupportedContentProfile("LiteLLM tool queue symbol changed; re-verify the no-content shim before starting.")
    try:
        fingerprint = hashlib.sha256(inspect.getsource(target).encode()).hexdigest()
    except (OSError, TypeError):
        fingerprint = "unavailable"
    if fingerprint != QUEUE_SOURCE_SHA256:
        raise UnsupportedContentProfile("LiteLLM tool queue implementation changed; re-verify the no-content shim before starting.")

    def no_tool_inventory(self, kwargs, completion_response, hashed_token=None, team_id=None) -> None:
        return None

    DBSpendUpdateWriter._enqueue_tool_registry_upsert = no_tool_inventory


def require_single_process(argv, env) -> None:
    """Spawned interpreters would import the unpatched upstream application."""
    workers = [env.get("NUM_WORKERS", "1")]
    for index, arg in enumerate(argv):
        if arg == "--reload" or arg.startswith("--reload="):
            raise ValueError("single_process_required: remove --reload and set NUM_WORKERS/--num_workers to 1")
        if arg == "--num_workers":
            workers.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--num_workers="):
            workers.append(arg.partition("=")[2])
    try:
        valid = all(int(value) == 1 for value in workers)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("single_process_required: set NUM_WORKERS/--num_workers to 1 and remove --reload")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--content-proof", type=Path)
    args, cli_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    try:
        require_single_process(cli_args, os.environ)
    except ValueError as exc:
        print(f"[litellm-content] {exc}", file=sys.stderr)
        return 78
    try:
        install_no_content_shim()
        if args.content_proof is not None:
            # Only the managed gateway needs a PID-bound proof. The Compose
            # launcher is standalone and uses its static shipped mount instead.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from gateway.litellm_content import write_launch_proof

            config_path = Path(cli_args[cli_args.index("--config") + 1])
            write_launch_proof(args.content_proof, config_path, os.environ)
    except UnsupportedContentProfile:
        print("[litellm-content] LiteLLM version or tool queue changed. Re-verify the pinned no-content shim; refusing to start.", file=sys.stderr)
        return 78
    except Exception:
        print("[litellm-content] No-content startup check failed; refusing to start.", file=sys.stderr)
        return 78
    # This is the installed `litellm` console entry point, in the same process
    # whose queue method was checked and patched above.
    from litellm import run_server

    sys.argv = ["litellm", *cli_args]
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
