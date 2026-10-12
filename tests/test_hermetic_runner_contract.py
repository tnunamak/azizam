from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "run-hermetic-pytest.sh"
PREFLIGHT = ROOT / "scripts" / "hermetic_source_preflight.sh"


def _runner(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env["PATH"] = "/home/tnunamak/.local/bin:" + env.get("PATH", "")
    return subprocess.run([str(RUNNER), *args], text=True, capture_output=True, env=env)


def test_runner_script_is_executable() -> None:
    assert RUNNER.stat().st_mode & 0o111




def test_runner_contract_is_structurally_fresh_and_sanitized() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "uv pip sync --offline --no-build --strict --require-hashes" in text
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
    result = subprocess.run([str(PREFLIGHT), str(repo), str(lock), str(req), str(inp)], text=True, capture_output=True)
    assert result.returncode == 2 and expected in result.stderr


def _copy_runner_source(repo: Path) -> tuple[Path, Path, Path]:
    (repo / "scripts").mkdir()
    (repo / "tests").mkdir()
    runner = repo / "scripts" / RUNNER.name
    preflight = repo / "scripts" / PREFLIGHT.name
    shutil.copy2(RUNNER, runner)
    shutil.copy2(PREFLIGHT, preflight)
    shutil.copy2(ROOT / "scripts" / "low-priority", repo / "scripts" / "low-priority")
    req = repo / "requirements.txt"
    inp = repo / "tests" / "requirements-hermetic.in"
    lock = repo / "tests" / "requirements-hermetic.lock"
    req.write_text("a\n")
    inp.write_text("b\n")
    import hashlib
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    lock.write_text(
        f"# hermetic-manifest-sha256: {digest(req)}\n"
        f"# hermetic-input-sha256: {digest(inp)}\n"
    )
    (repo / "proxy_config.yml").write_text("backends: {}\n")
    return req, inp, lock


def _make_invalid_source(repo: Path, req: Path, inp: Path, lock: Path, case: str) -> tuple[int, str]:
    expected = {
        "dirty": (2, "dirty tracked tree"),
        "untracked": (2, "untracked input"),
        "requirements": (2, "stale"),
        "input": (2, "stale"),
        "malformed": (2, "stale"),
        "missing": (127, "required hermetic test input is missing"),
    }[case]
    if case == "dirty":
        req.write_text("changed\n")
    elif case == "untracked":
        (repo / "extra").write_text("x\n")
    elif case == "requirements":
        req.write_text("changed\n")
        subprocess.run(["git", "-C", str(repo), "add", "-f", req.name], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    elif case == "input":
        inp.write_text("changed\n")
        subprocess.run(["git", "-C", str(repo), "add", "-f", "tests/requirements-hermetic.in"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    elif case == "malformed":
        lock.write_text("broken\n")
        subprocess.run(["git", "-C", str(repo), "add", "-f", "tests/requirements-hermetic.lock"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    else:
        lock.unlink()
        subprocess.run(["git", "-C", str(repo), "add", "-u"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    return expected


@pytest.mark.parametrize("case", ["dirty", "untracked", "requirements", "input", "malformed", "missing"])
def test_runner_rejects_invalid_source_before_state_or_provisioning(tmp_path: Path, case: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    req, inp, lock = _copy_runner_source(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@y"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-f", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    expected = _make_invalid_source(repo, req, inp, lock, case)

    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    provision_marker = tmp_path / "uv-invoked"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/usr/bin/env bash\n: > "$HERMETIC_PROVISION_MARKER"\n')
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "HERMETIC_PROVISION_MARKER": str(provision_marker),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run([str(repo / "scripts" / RUNNER.name)], text=True, capture_output=True, env=env)

    expected_code, expected_message = expected
    assert result.returncode == expected_code
    assert expected_message in result.stderr
    assert not list((home / ".tmp").glob("azizam-pytest.*"))
    assert not provision_marker.exists()


def test_runner_refuses_to_replace_an_existing_junit_report(tmp_path: Path) -> None:
    # A second run into the same report would replace the first run's failures.
    report = tmp_path / "pytest.xml"
    report.write_text("<testsuites/>")
    result = _runner("--junit-xml-out", str(report), "-q")
    assert result.returncode == 2
    assert f"--junit-xml-out {report} already exists" in result.stderr
    assert report.read_text() == "<testsuites/>"
