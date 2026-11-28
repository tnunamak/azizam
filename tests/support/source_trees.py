"""One parse of each tracked source file per worker, shared by the tests that scan the code.

Several test files walk the same production modules (the egress and engine-boundary guards, the upstream-text checks).
Each used to parse them all on its own, which costs seconds per file. The tracked sources do not change during a run,
and callers only read the nodes: nothing here may be modified in place."""
from __future__ import annotations

import ast
import functools
from pathlib import Path


@functools.cache
def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))
