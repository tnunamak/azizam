"""Optional Linux namespace wrapper; Python benchmark itself needs no bwrap."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    if not shutil.which("bwrap"):
        raise SystemExit("bwrap is required for the optional namespace wrapper")
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    output = root / "bench" / "results"
    output.mkdir(exist_ok=True)
    state_root = root / "bench" / "results"
    with tempfile.TemporaryDirectory(prefix="namespace-", dir=state_root) as folder:
        home = Path(folder)
        args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
                "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/dev/shm"]
        for path in ("/usr", "/lib", "/lib64", "/etc/ssl", "/etc/ld.so.cache", "/etc/localtime"):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        # A uv interpreter may live outside /usr. Bind only its runtime, plus
        # this checkout and its lock-only venv; the operator checkout is absent.
        for path in {str(root), sys.base_prefix, sys.prefix}:
            args += ["--ro-bind", path, path]
        if Path(sys.executable).is_symlink():
            target = Path(os.readlink(sys.executable))
            if target.is_absolute():
                args += ["--ro-bind", str(target.parent.parent), str(target.parent.parent)]
        args += ["--bind", str(home), "/home/bench", "--bind", str(output), str(output),
                 "--setenv", "HOME", "/home/bench", "--setenv", "PATH", "/usr/bin:/bin",
                 "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                 "--setenv", "PYTHONNOUSERSITE", "1", "--setenv", "BENCH_NAMESPACE", "1",
                 "--setenv", "BENCH_HOST_NETNS", os.readlink("/proc/self/ns/net"),
                 "--setenv", "BENCH_CODE_SHA", revision,
                 "--chdir", str(root), "--", sys.executable, "-m", "bench.run"]
        command_args = sys.argv[1:]
        if command_args[:1] == ["--"]:
            command_args = command_args[1:]
        if "--json" not in command_args:
            command_args += ["--json", "bench/results/namespace-run.json"]
        return subprocess.call(args + command_args)


if __name__ == "__main__":
    raise SystemExit(main())
