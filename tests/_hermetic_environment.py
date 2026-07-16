"""Runtime assertions for the already structurally-checked hermetic process."""
from __future__ import annotations

import importlib.util
import os
import site
import sys
import sysconfig
from pathlib import Path


class HermeticEnvironmentError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _origin(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise HermeticEnvironmentError(f"cannot locate {name}")
    return Path(spec.origin)


def validate_test_environment() -> dict[str, str]:
    env = Path(os.environ["VIVID_FISH_HERMETIC_TEST_ENV_ROOT"])
    repo = Path(os.environ["VIVID_FISH_HERMETIC_REPO_ROOT"])
    pyroot = Path(os.environ["VIVID_FISH_HERMETIC_PYTHON_ROOT"])
    for label, value in (("environment", env), ("repo", repo), ("python", pyroot)):
        if value.is_symlink() or not value.is_dir() or value.resolve() != value.absolute():
            raise HermeticEnvironmentError(f"{label} root is not real")
    if not _inside(Path(sys.executable), env) or not _inside(Path(sys.executable).resolve(), pyroot):
        raise HermeticEnvironmentError("interpreter is outside approved roots")
    if not _inside(Path(sys.prefix), env) or not _inside(Path(sys.base_prefix), pyroot):
        raise HermeticEnvironmentError("Python prefixes escape approved roots")
    if site.ENABLE_USER_SITE:
        raise HermeticEnvironmentError("user site packages enabled")
    approved = (env, repo, pyroot)
    for entry in (Path(p) for p in sys.path if p):
        if not any(_inside(entry, root) for root in approved):
            raise HermeticEnvironmentError(f"unapproved sys.path entry: {entry}")
    for key in ("purelib", "platlib"):
        if not _inside(Path(sysconfig.get_path(key)), env):
            raise HermeticEnvironmentError(f"sysconfig {key} escapes environment")
    for module, root in (("proxy", repo), ("gateway", repo), ("pytest", env)):
        if not _inside(_origin(module), root):
            raise HermeticEnvironmentError(f"{module} import escaped approved root")
    return {"interpreter": str(Path(sys.executable).resolve()), "environment_root": str(env),
            "python_root": str(pyroot), "repo_root": str(repo),
            "lock_sha256": os.environ["VIVID_FISH_HERMETIC_LOCK_SHA256"],
            "python_abi": sys.implementation.cache_tag, "platform": sysconfig.get_platform()}
