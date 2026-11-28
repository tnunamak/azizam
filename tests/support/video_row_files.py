"""The files a video create needs under test, now that a plain create is a row job (builder V5c).

A row job keeps its run spool, its payloads and its job row in files the gateway caches for the whole process. A rig that
posts to the video create route patches them to its own temporary tree, so the run core is rebuilt over it and no test reads
another test's job."""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import proxy
from gateway import media_jobs


def patch_video_row_files(stack: ExitStack, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "media_jobs.db"
    media_jobs.init_db(database)
    stack.enter_context(patch.object(media_jobs, "DB_PATH", database))
    stack.enter_context(patch.object(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", root / "run-spool"))
    stack.enter_context(patch.object(proxy, "_WORKFLOW_JOB_SPOOL", None))               # built from the root above, at first use
    stack.enter_context(patch.object(proxy, "_workflow_core_cache", None))              # the core holds the job spool
    stack.enter_context(patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"))


def patch_video_row_files_in_a_temporary_tree(stack: ExitStack) -> Path:
    """The same, over a new temporary directory the stack removes when it closes."""
    root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    patch_video_row_files(stack, root)
    return root
