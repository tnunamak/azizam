from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run-hermetic-pytest.sh"
PREFLIGHT = ROOT / "scripts" / "hermetic_source_preflight.sh"


def _runner(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env["PATH"] = "/home/tnunamak/.local/bin:" + env.get("PATH", "")
    return subprocess.run([str(RUNNER), *args], text=True, capture_output=True, env=env)


def test_fresh_runner_contract_has_success_path() -> None:
    assert RUNNER.stat().st_mode & 0o111




def test_runner_contract_is_structurally_fresh_and_sanitized() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "uv pip sync --strict --require-hashes" in text
    assert "fresh-venv" in text
    assert "git -C \"$repo_root\" archive --format=tar HEAD" in text
    assert 'add_read_only_bind "$checkout_root" "$checkout_root"' in text
    assert "PYTHONPATH" not in text
    assert "hermetic_preflight.py" not in text
    assert "provision-hermetic-pytest-env.sh" not in text


@pytest.mark.parametrize("case", ["dirty", "untracked", "requirements", "input", "malformed", "missing"])
def test_source_preflight_specific_failures(tmp_path: Path, case: str) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@y"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
    req = repo / "requirements.txt"; inp = repo / "input.in"; lock = repo / "lock"
    req.write_text("a\n"); inp.write_text("b\n")
    import hashlib
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    lock.write_text(f"# hermetic-manifest-sha256: {h(req)}\n# hermetic-input-sha256: {h(inp)}\n")
    for name in ("requirements.txt", "input.in", "lock"): subprocess.run(["git", "-C", str(repo), "add", "-f", name], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    expected = {"dirty":"dirty tracked tree", "untracked":"untracked input", "requirements":"stale", "input":"stale", "malformed":"stale", "missing":"missing lock or manifest"}[case]
    if case == "dirty": req.write_text("changed\n")
    elif case == "untracked": (repo / "extra").write_text("x\n")
    elif case == "requirements": req.write_text("changed\n"); subprocess.run(["git", "-C", str(repo), "add", "-f", "requirements.txt"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    elif case == "input": inp.write_text("changed\n"); subprocess.run(["git", "-C", str(repo), "add", "-f", "input.in"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    elif case == "malformed": lock.write_text("broken\n"); subprocess.run(["git", "-C", str(repo), "add", "-f", "lock"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    else: lock.unlink(); subprocess.run(["git", "-C", str(repo), "add", "-u"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    marker = tmp_path / "not-started"
    result = subprocess.run([str(PREFLIGHT), str(repo), str(lock), str(req), str(inp)], text=True, capture_output=True)
    assert result.returncode == 2 and expected in result.stderr
    assert not marker.exists()
