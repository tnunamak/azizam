#!/usr/bin/env python3
"""Write the suite's bytecode once, in parallel, before pytest-xdist starts.

The hermetic runner extracts a fresh checkout for every run and the sandbox never
writes bytecode into it, so every xdist worker would read, assert-rewrite and
compile every test module by itself: about 8 s of CPU per worker.  This compiles
each file once, in SLICES processes, into PYTHONPYCACHEPREFIX (the run's private
directory), where the workers find it.  Test modules get the code pytest's own
import hook would write, at the path it looks in; other modules get ordinary
bytecode.  Both caches check the source's mtime and size.  A file that fails here
is left for the worker to compile, so the cache can only save work.

usage: hermetic_prewarm.py SLICES
"""
from __future__ import annotations

import os
import py_compile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SKIP_DIRS = {"venv", "cache", "node_modules", "build", "dist", ".git"}


def python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py")
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1])
    )


def compile_slice(root: Path, index: int, slices: int) -> None:
    from _pytest.assertion import rewrite

    config = SimpleNamespace(getini=lambda _name: False)
    for path in python_files(root)[index::slices]:
        try:
            if path.name == "conftest.py" or path.name.startswith("test_") or path.name.endswith("_test.py"):
                source_stat, code = rewrite._rewrite_test(path, config)
                cache = rewrite.get_cache_dir(path)
                cache.mkdir(parents=True, exist_ok=True)
                rewrite._write_pyc(SimpleNamespace(trace=lambda _message: None), code, source_stat,
                                   cache / (path.name[:-3] + rewrite.PYC_TAIL))
            else:
                py_compile.compile(str(path), doraise=True)
        except Exception:  # noqa: BLE001 - the worker compiles what this could not
            continue


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--slice":
        compile_slice(Path.cwd(), int(sys.argv[2]), int(sys.argv[3]))
        return 0
    slices = max(1, int(sys.argv[1]))
    children = [
        subprocess.Popen([sys.executable, __file__, "--slice", str(index), str(slices)], env=os.environ.copy())
        for index in range(slices)
    ]
    for child in children:
        child.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
