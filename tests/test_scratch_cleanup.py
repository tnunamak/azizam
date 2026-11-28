"""Test harnesses delete their own scratch directories (scripts/lib/scratch.sh, scripts/scratch_owner.py)
and receipts keep a bounded size (scripts/ci/prune_receipts.py).

A harness removes its directory on EXIT, HUP, INT and TERM. SIGKILL runs no trap, so a detached watcher
removes it when the owner is gone, and the next start removes directories whose owner (pid and start time
in .scratch-owner) is dead. A directory with a live
owner, no marker or a symlink is left alone, and so is the one path named in SCRATCH_KEEP.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib" / "scratch.sh"
PRUNE = ROOT / "scripts" / "ci" / "prune_receipts.py"
sys.path.insert(0, str(ROOT / "scripts"))
import scratch_owner  # noqa: E402

# A fake harness: sweep, make a marked directory, announce it, wait for a signal.
HARNESS = f"""
source {LIB}
scratch_sweep "$1" fake-
dir=$(mktemp -d "$1/fake-XXXXXX")
scratch_mark "$dir"
trap 'rm -rf -- "$dir"' EXIT
scratch_on_signal
echo "$dir" > "$1/announced"
sleep 60 &
wait $!
"""


# bash cannot trap a signal it started ignoring, and an ignored signal survives exec. A parent run
# under nohup (HUP) or by a launcher that ignores TERM would make these tests fail for a reason that
# has nothing to do with the harness. Reset the three signals, then exec bash: same pid.
RESET_THEN_EXEC = (
    "import os, signal, sys\n"
    "for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):\n"
    "    signal.signal(number, signal.SIG_DFL)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)


def _start_harness(root: Path) -> tuple[subprocess.Popen[bytes], Path]:
    process = subprocess.Popen([sys.executable, "-c", RESET_THEN_EXEC, "bash", "-c", HARNESS, "harness", str(root)])
    deadline = time.monotonic() + 10
    while not (root / "announced").exists():
        assert time.monotonic() < deadline and process.poll() is None, "harness did not start"
        time.sleep(0.02)
    return process, Path((root / "announced").read_text().strip())


def _dead_pid() -> tuple[int, str]:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    started = scratch_owner.start_time(dead.pid)
    dead.wait()
    return dead.pid, str(started)


def _plant(root: Path, name: str, owner: str | None) -> Path:
    path = root / name
    (path / "data").mkdir(parents=True)
    (path / "data" / "copy-of-owner-data").write_text("x")
    if owner is not None:
        (path / ".scratch-owner").write_text(owner + "\n")
    return path


@pytest.mark.parametrize("signal_name", ["TERM", "HUP"])  # INT: xdist workers start with SIGINT ignored, which bash cannot trap
def test_a_signalled_harness_leaves_nothing(tmp_path: Path, signal_name: str) -> None:
    process, directory = _start_harness(tmp_path)
    assert directory.is_dir()
    subprocess.run(["kill", f"-{signal_name}", str(process.pid)], check=True)
    process.wait(timeout=20)
    assert not directory.exists()
    assert process.returncode == {"TERM": 143, "HUP": 129}[signal_name]


def test_a_killed_harness_directory_is_removed_by_its_watcher(tmp_path: Path) -> None:
    script = f"""
source {LIB}
dir=$(mktemp -d "$1/fake-XXXXXX")
scratch_reap_on_death "$dir"
echo "$dir" > "$1/announced"
sleep 60 &
wait $!
"""
    process = subprocess.Popen([sys.executable, "-c", RESET_THEN_EXEC, "bash", "-c", script, "harness", str(tmp_path)])
    deadline = time.monotonic() + 10
    while not (tmp_path / "announced").exists():
        assert time.monotonic() < deadline and process.poll() is None, "harness did not start"
        time.sleep(0.02)
    directory = Path((tmp_path / "announced").read_text().strip())
    time.sleep(3)  # the owner is alive: the watcher keeps the directory
    assert directory.is_dir()
    subprocess.run(["kill", "-KILL", str(process.pid)], check=True)
    process.wait(timeout=20)
    deadline = time.monotonic() + 10
    while directory.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not directory.exists()


def test_the_watcher_holds_no_lock_or_pipe_of_its_caller(tmp_path: Path) -> None:
    # ci-local holds the host lock on a descriptor; a watcher that kept a copy would hold the lock after ci-local ends.
    script = f"""
set -euo pipefail
source {LIB}
exec 9>"$1/host.lock"
flock -n 9
dir=$(mktemp -d "$1/fake-XXXXXX")
scratch_reap_on_death "$dir"
sleep 0.5  # the watcher is running and has seen its owner alive, so it would hold an inherited lock until its next poll
echo "$dir"
"""
    result = subprocess.run(["bash", "-c", script, "harness", str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert subprocess.run(["flock", "-n", str(tmp_path / "host.lock"), "true"]).returncode == 0
    directory = Path(result.stdout.strip())
    deadline = time.monotonic() + 10
    while directory.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not directory.exists()


def test_a_dead_owners_directory_is_removed_when_the_next_harness_starts(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    stale = _plant(tmp_path, "fake-stale", f"{pid}-{started}")
    reused_pid = _plant(tmp_path, "fake-reused", f"{os.getpid()}-{int(scratch_owner.start_time(os.getpid())) + 1}")
    live = _plant(tmp_path, "fake-live", f"{os.getpid()}-{scratch_owner.start_time(os.getpid())}")
    unmarked = _plant(tmp_path, "fake-unmarked", None)
    other = _plant(tmp_path, "other-stale", f"{pid}-{started}")
    link = tmp_path / "fake-link"
    link.symlink_to(other)
    process, directory = _start_harness(tmp_path)
    try:
        assert not stale.exists() and not reused_pid.exists()
        assert live.is_dir() and unmarked.is_dir() and other.is_dir() and link.is_symlink()
        assert directory.is_dir()
    finally:
        subprocess.run(["kill", "-TERM", str(process.pid)], check=True)
        process.wait(timeout=20)


def test_the_kept_path_survives_the_sweep(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    kept = _plant(tmp_path, "fake-kept", f"{pid}-{started}")
    swept = _plant(tmp_path, "fake-swept", f"{pid}-{started}")
    script = f'source {LIB}; SCRATCH_KEEP={kept} scratch_sweep {tmp_path} fake-'
    subprocess.run(["bash", "-c", script], check=True)
    assert kept.is_dir() and not swept.exists()


def test_the_python_twin_sweeps_and_unwinds_on_term(tmp_path: Path) -> None:
    pid, started = _dead_pid()
    stale = _plant(tmp_path, "fake-stale", f"{pid}-{started}")
    kept = _plant(tmp_path, "fake-kept", f"{pid}-{started}")
    assert scratch_owner.sweep(tmp_path, "fake-", keep=kept) == [stale]
    assert kept.is_dir()
    program = f"""
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, {str(ROOT / "scripts")!r})
import scratch_owner
scratch_owner.exit_on_signals()
with tempfile.TemporaryDirectory(prefix="fake-", dir={str(tmp_path)!r}) as d:
    scratch_owner.mark(Path(d))
    Path({str(tmp_path / "announced")!r}).write_text(d)
    time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", program])
    deadline = time.monotonic() + 10
    while not (tmp_path / "announced").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    directory = Path((tmp_path / "announced").read_text())
    assert directory.is_dir()
    subprocess.run(["kill", "-TERM", str(process.pid)], check=True)
    process.wait(timeout=20)
    assert not directory.exists()


# -- receipts ------------------------------------------------------------------

def _receipt(root: Path, name: str, age_days: float, signoff_age_days: float | None = None) -> Path:
    run = root / name
    (run / "logs").mkdir(parents=True)
    (run / "test-results").mkdir()
    (run / "cases.jsonl").write_text("x" * 100)
    (run / "logs" / "a.log").write_text("log")
    (run / "run.tsv").write_text("sha\tx\n")
    (run / "PASS").write_text("x")
    stamps = {run / "run.tsv": age_days}
    if signoff_age_days is not None:
        (run / f"signoff-{name}.tsv").write_text("posted\n")
        stamps[run / f"signoff-{name}.tsv"] = signoff_age_days
    now = time.time()
    for path, days in stamps.items():
        os.utime(path, (now - days * 86400, now - days * 86400))
    return run


def _prune(root: Path, *extra: str) -> None:
    subprocess.run([sys.executable, str(PRUNE), str(root), *extra], check=True, capture_output=True)


def test_run_stamp_uses_the_marker_instead_of_later_directory_changes(tmp_path: Path) -> None:
    run = tmp_path / "invented-run"
    run.mkdir()
    marker = run / "run.tsv"
    marker.write_text("synthetic\n")
    os.utime(marker, (1000, 1000))
    os.utime(run, (2000, 2000))
    assert prune_receipts.stamp(run) == 1000


def test_run_stamp_uses_directory_time_when_no_marker_exists(tmp_path: Path) -> None:
    run = tmp_path / "invented-unmarked-run"
    run.mkdir()
    (run / "unrelated.txt").write_text("synthetic\n")
    os.utime(run, (3000, 3000))
    assert prune_receipts.stamp(run) == 3000


def test_old_receipts_keep_only_their_verdicts(tmp_path: Path) -> None:
    runs = [_receipt(tmp_path, f"r{index:02d}", age_days=index / 10) for index in range(25)]  # r00 is newest
    recent_signoff = _receipt(tmp_path, "signed-old", age_days=30, signoff_age_days=2)
    stale_signoff = _receipt(tmp_path, "signed-stale", age_days=30, signoff_age_days=9)
    scheduled = tmp_path / "scheduled" / "nightly-1"
    scheduled.mkdir(parents=True)
    (scheduled / "receipt.json").write_text("{}")
    _prune(tmp_path, "--keep", str(runs[-1]))
    for run in runs[:20] + [recent_signoff, runs[-1]]:
        assert (run / "cases.jsonl").is_file() and (run / "logs" / "a.log").is_file(), run.name
    for run in runs[20:-1]:
        assert sorted(path.name for path in run.iterdir()) == ["PASS", "run.tsv"], run.name
    assert sorted(path.name for path in stale_signoff.iterdir()) == ["PASS", "run.tsv", "signoff-signed-stale.tsv"]
    assert (scheduled / "receipt.json").is_file()


def test_catalog_publication_preserves_the_old_file_if_staging_is_interrupted(tmp_path: Path) -> None:
    from argparse import Namespace
    from unittest.mock import patch
    import gen_operation_catalog as catalog

    source = tmp_path / "words.yml"
    source.write_text("".join(f"{name}: {{}}\n" for name, _ in catalog.PROJECTED + catalog.UNEXPRESSED))
    output = tmp_path / "operations.yml"
    output.write_bytes(b"last complete catalog\n")
    args = Namespace(source=source, output=output, media_source=None, local_source=None,
                     models_source=None, frontends_source=None)
    write = Path.write_bytes

    def partially_write(path: Path, data: bytes) -> int:
        write(path, data[:1])
        raise OSError("synthetic interrupted catalog write")

    with patch("argparse.ArgumentParser.parse_args", return_value=args):
        with patch.object(Path, "write_bytes", partially_write):
            try:
                catalog.main()
            except OSError as error:
                assert str(error) == "synthetic interrupted catalog write"
            else:
                raise AssertionError("expected interrupted publication")
        assert output.read_bytes() == b"last complete catalog\n"
        assert sorted(path.name for path in tmp_path.iterdir()) == ["operations.yml", "words.yml"]
        catalog.main()
    assert output.read_text() == catalog.generate(source)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("local", "^Unsupported local source version$"),
        ("frontends", "^Frontend IDs must be unique nonempty text$"),
    ],
)
def test_catalog_refuses_bad_optional_input_before_replacing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    from argparse import Namespace
    import gen_operation_catalog as catalog

    source = tmp_path / "words.yml"
    source.write_text("".join(f"{name}: {{}}\n" for name, _ in catalog.PROJECTED + catalog.UNEXPRESSED))
    optional = tmp_path / "optional.yml"
    if kind == "local":
        optional.write_text(
            "schema_version: 999\n"
            + "".join(f"{key}: []\n" for key in catalog.LOCAL_LISTS)
            + "".join(f"{key}: {{}}\n" for key in catalog.LOCAL_DEFAULTS)
        )
    else:
        optional.write_text("records: [{id: sample}, {id: sample}]\nfrontends: []\ninput_contract: {}\n")
    output = tmp_path / "operations.yml"
    original = b"last complete catalog\n"
    output.write_bytes(original)
    args = Namespace(
        source=source,
        output=output,
        media_source=None,
        local_source=optional if kind == "local" else None,
        models_source=None,
        frontends_source=optional if kind == "frontends" else None,
    )
    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda _parser: args)

    with pytest.raises(ValueError, match=message):
        catalog.main()

    assert output.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["operations.yml", "optional.yml", "words.yml"]


def test_pruning_a_missing_or_empty_root_is_harmless(tmp_path: Path) -> None:
    _prune(tmp_path / "absent")
    _prune(tmp_path)


# -- failed runs keep their evidence -------------------------------------------

_spec = importlib.util.spec_from_file_location("prune_receipts", PRUNE)
assert _spec and _spec.loader
prune_receipts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune_receipts)


def test_receipt_age_uses_run_marker_when_directory_was_touched(tmp_path: Path) -> None:
    marker = tmp_path / "run.tsv"
    marker.touch()
    os.utime(marker, (100, 100))
    os.utime(tmp_path, (200, 200))

    assert prune_receipts.stamp(tmp_path) == 100


def test_receipt_signoff_includes_exact_cutoff_but_ignores_unrelated_file(tmp_path: Path) -> None:
    stray = tmp_path / "notes.tsv"
    stray.touch()
    os.utime(stray, (300, 300))
    signoff = tmp_path / "signoff-fixture.tsv"
    signoff.touch()
    os.utime(signoff, (200, 200))

    assert prune_receipts.has_recent_signoff(tmp_path, 200)
    assert not prune_receipts.has_recent_signoff(tmp_path, 201)


def test_receipt_stamp_falls_back_to_directory_when_run_marker_is_absent(tmp_path: Path) -> None:
    os.utime(tmp_path, (40, 40))
    assert prune_receipts.stamp(tmp_path) == 40


def test_receipt_signoff_with_wrong_extension_is_not_a_signoff(tmp_path: Path) -> None:
    decoy = tmp_path / "signoff-fixture.log"
    decoy.touch()
    os.utime(decoy, (200, 200))
    assert not prune_receipts.has_recent_signoff(tmp_path, 200)


def _failed_run(root: Path, name: str, age_days: float, trace_bytes: int = 10, screenshot_bytes: int = 10) -> Path:
    run = _receipt(root, name, age_days=age_days)
    (run / "PASS").unlink()
    (run / "FAIL").write_text("x")
    case = run / "e2e-failures" / "artifacts-18200" / "a-spec-case-chromium"
    case.mkdir(parents=True)
    (case / "error-context.md").write_text("context")
    (case / "test-failed-1.png").write_bytes(b"p" * screenshot_bytes)
    (case / "trace.zip").write_bytes(b"t" * trace_bytes)
    stamp = time.time() - age_days * 86400
    os.utime(run / "run.tsv", (stamp, stamp))
    return run


def _names(run: Path) -> list[str]:
    return sorted(path.name for path in run.iterdir())


def test_receipt_stamp_prefers_run_marker_then_falls_back_to_directory(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    os.utime(receipt, (1000, 1000))
    assert prune_receipts.stamp(receipt) == 1000
    marker = receipt / "run.tsv"
    marker.touch()
    os.utime(marker, (2000, 2000))
    os.utime(receipt, (3000, 3000))
    assert prune_receipts.stamp(receipt) == 2000


def test_signoff_cutoff_includes_the_boundary_and_ignores_other_notes(tmp_path: Path) -> None:
    signoff = tmp_path / "signoff-review.tsv"
    ignored = tmp_path / "review.tsv"
    for file, stamp in ((signoff, 2000), (ignored, 3000)):
        file.touch()
        os.utime(file, (stamp, stamp))
    assert prune_receipts.has_recent_signoff(tmp_path, 2000)
    assert not prune_receipts.has_recent_signoff(tmp_path, 2001)


def test_a_failed_run_keeps_logs_and_artifacts_for_three_days(tmp_path: Path) -> None:
    newer = [_receipt(tmp_path, f"r{index:02d}", age_days=index / 100) for index in range(20)]
    fresh = _failed_run(tmp_path, "failed-fresh", age_days=2.5)
    stale = _failed_run(tmp_path, "failed-stale", age_days=3.5)
    passed = _receipt(tmp_path, "passed-old", age_days=2.6)
    _prune(tmp_path)
    assert _names(fresh) == ["FAIL", "e2e-failures", "logs", "run.tsv"]
    assert (fresh / "logs" / "a.log").is_file()
    kept = fresh / "e2e-failures" / "artifacts-18200" / "a-spec-case-chromium"
    assert sorted(path.name for path in kept.iterdir()) == ["error-context.md", "test-failed-1.png", "trace.zip"]
    assert _names(stale) == ["FAIL", "run.tsv"]
    assert _names(passed) == ["PASS", "run.tsv"]
    assert all((run / "cases.jsonl").is_file() for run in newer)


def test_a_failed_runs_artifacts_lose_traces_then_screenshots_over_the_cap(tmp_path: Path) -> None:
    cap = prune_receipts.ARTIFACT_CAP
    padding = [_receipt(tmp_path, f"r{index:02d}", age_days=index / 100) for index in range(20)]
    heavy = _failed_run(tmp_path, "heavy", age_days=1, trace_bytes=cap, screenshot_bytes=1000)
    huge = _failed_run(tmp_path, "huge", age_days=1.1, trace_bytes=10, screenshot_bytes=cap + 1)
    light = _failed_run(tmp_path, "light", age_days=1.2, trace_bytes=cap - 1000, screenshot_bytes=500)
    assert padding
    _prune(tmp_path)

    def left(run: Path) -> list[str]:
        return sorted(path.name for path in (run / "e2e-failures").rglob("*") if path.is_file())

    assert left(heavy) == ["OMITTED.txt", "error-context.md", "test-failed-1.png"]
    assert (heavy / "e2e-failures" / "OMITTED.txt").read_text() == "artifacts-18200/a-spec-case-chromium/trace.zip\n"
    assert left(huge) == ["OMITTED.txt", "error-context.md"]
    assert left(light) == ["error-context.md", "test-failed-1.png", "trace.zip"]


def test_collect_keeps_only_a_failed_tests_files(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    case = out / "artifacts-18200" / "b-spec-case-chromium"
    case.mkdir(parents=True)
    for name in ("error-context.md", "test-failed-1.png", "trace.zip", "video.webm", "notes.txt"):
        (case / name).write_text("x")
    (out / "artifacts-18210").mkdir()
    (out / "report.json").write_text("{}")
    dest = tmp_path / "receipt" / "e2e-failures"
    dest.parent.mkdir()
    subprocess.run([sys.executable, str(PRUNE), "--collect", str(out), str(dest)], check=True, capture_output=True)
    copied = sorted(str(path.relative_to(dest)) for path in dest.rglob("*") if path.is_file())
    assert copied == [f"artifacts-18200/b-spec-case-chromium/{name}" for name in ("error-context.md", "test-failed-1.png", "trace.zip")]
    subprocess.run([sys.executable, str(PRUNE), "--collect", str(tmp_path / "absent"), str(tmp_path / "none")], check=True, capture_output=True)
    assert not (tmp_path / "none").exists()


def test_collect_keeps_same_named_evidence_from_distinct_cases(tmp_path: Path) -> None:
    source = tmp_path / "results"
    dest = tmp_path / "kept"
    for case, data in (("port-a", b"first"), ("port-b", b"second")):
        folder = source / case
        folder.mkdir(parents=True)
        (folder / "error-context.md").write_bytes(data)

    assert prune_receipts.collect_failures(source, dest, cap=200) == 2
    assert (dest / "port-a" / "error-context.md").read_bytes() == b"first"
    assert (dest / "port-b" / "error-context.md").read_bytes() == b"second"


def test_note_keeps_complete_utf8_lines_and_existing_evidence(tmp_path: Path) -> None:
    note = tmp_path / prune_receipts.OMITTED
    original = b"earlier.zip\n"
    note.write_bytes(original)
    next_line = "café.zip\n".encode("utf-8")
    room = len(original) + len(next_line)

    prune_receipts.note_omitted(
        tmp_path, [tmp_path / "café.zip", tmp_path / "later.zip"], room
    )

    assert note.read_bytes() == original + next_line


def test_the_cap_also_bounds_error_contexts_and_names_what_it_dropped(tmp_path: Path) -> None:
    case = tmp_path / "e2e-failures" / "artifacts-18200" / "c-spec-case-chromium"
    case.mkdir(parents=True)
    (case / "error-context.md").write_text("x" * 600)
    (case / "test-failed-1.png").write_bytes(b"p" * 300)
    (case / "trace.zip").write_bytes(b"t" * 300)
    prune_receipts.bound_artifacts(tmp_path / "e2e-failures", cap=500)
    left = sorted(path.name for path in (tmp_path / "e2e-failures").rglob("*") if path.is_file())
    assert left == ["OMITTED.txt"]
    assert (tmp_path / "e2e-failures" / "OMITTED.txt").read_text().splitlines() == [
        f"artifacts-18200/c-spec-case-chromium/{name}" for name in ("trace.zip", "test-failed-1.png", "error-context.md")
    ]


def test_collect_copies_contexts_first_and_stops_at_the_cap(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    case = out / "artifacts-18200" / "d-spec-case-chromium"
    case.mkdir(parents=True)
    (case / "error-context.md").write_text("x" * 100)
    (case / "test-failed-1.png").write_bytes(b"p" * 200)
    (case / "trace.zip").write_bytes(b"t" * 300)
    dest = tmp_path / "receipt" / "e2e-failures"
    dest.parent.mkdir()
    assert prune_receipts.collect_failures(out, dest, cap=350) == 2
    copied = sorted(path.name for path in dest.rglob("*") if path.is_file())
    assert copied == ["OMITTED.txt", "error-context.md", "test-failed-1.png"]
    assert (dest / "OMITTED.txt").read_text() == "artifacts-18200/d-spec-case-chromium/trace.zip\n"


def test_no_symlink_leads_pruning_or_collection_outside_the_receipts(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "trace.zip"
    sentinel.write_bytes(b"t" * 1000)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    padding = [_receipt(receipts, f"r{index:02d}", age_days=index / 100) for index in range(20)]
    assert padding
    run = _failed_run(receipts, "linked", age_days=1)
    shutil.rmtree(run / "e2e-failures")
    (run / "e2e-failures").symlink_to(outside, target_is_directory=True)
    _prune(receipts)
    assert sentinel.is_file()
    assert not (run / "e2e-failures").exists() and not (run / "e2e-failures").is_symlink()
    prune_receipts.bound_artifacts(tmp_path / "link-root", cap=0)
    (tmp_path / "link-root").symlink_to(outside, target_is_directory=True)
    prune_receipts.bound_artifacts(tmp_path / "link-root", cap=0)
    assert sentinel.is_file()

    src = tmp_path / "browser-results"
    src.mkdir()
    (src / "artifacts-18200").symlink_to(outside, target_is_directory=True)
    dest = tmp_path / "receipt" / "e2e-failures"
    assert prune_receipts.collect_failures(src, dest) == 0
    assert prune_receipts.collect_failures(tmp_path / "link-root", dest) == 0
    assert not dest.exists()


def _bytes_under(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file() and not path.is_symlink())


@pytest.mark.parametrize("cap", [0, 20, 60, 100, 250, 1000])
def test_the_cap_holds_with_the_note_counted(tmp_path: Path, cap: int) -> None:
    out = tmp_path / "browser-results"
    for case, context, picture, trace in (("a-case", 100, 0, 10), ("b-case", 30, 200, 400)):
        (out / case).mkdir(parents=True)
        (out / case / "error-context.md").write_bytes(b"x" * context)
        if picture:
            (out / case / "test-failed-1.png").write_bytes(b"p" * picture)
        (out / case / "trace.zip").write_bytes(b"t" * trace)
    dest = tmp_path / "receipt" / "e2e-failures"
    dest.parent.mkdir()
    prune_receipts.collect_failures(out, dest, cap=cap)
    assert _bytes_under(tmp_path / "receipt") <= cap
    whole = tmp_path / "whole"
    shutil.copytree(out, whole)
    prune_receipts.bound_artifacts(whole, cap=cap)
    assert _bytes_under(whole) <= cap


def test_bounding_names_a_dropped_trace_even_when_nothing_else_goes(tmp_path: Path) -> None:
    case = tmp_path / "e2e-failures" / "e-case"
    case.mkdir(parents=True)
    (case / "error-context.md").write_bytes(b"x" * 10)
    (case / "trace.zip").write_bytes(b"t" * 100)
    prune_receipts.bound_artifacts(tmp_path / "e2e-failures", cap=80)
    assert (tmp_path / "e2e-failures" / "OMITTED.txt").read_text() == "e-case/trace.zip\n"
    assert (case / "error-context.md").is_file() and _bytes_under(tmp_path / "e2e-failures") <= 80


def test_collect_puts_contexts_first_across_cases(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    (out / "a-case").mkdir(parents=True)
    (out / "b-case").mkdir()
    (out / "a-case" / "trace.zip").write_bytes(b"t" * 100)
    (out / "b-case" / "error-context.md").write_bytes(b"x" * 100)
    dest = tmp_path / "e2e-failures"
    assert prune_receipts.collect_failures(out, dest, cap=150) == 1
    assert (dest / "b-case" / "error-context.md").is_file() and not (dest / "a-case").exists()


def test_new_receipt_file_refuses_collision_and_keeps_previous_bytes(tmp_path: Path) -> None:
    existing = tmp_path / "fixture.tsv"
    existing.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        with prune_receipts.open_new(existing) as output:
            output.write(b"replacement")
    assert existing.read_bytes() == b"original"


def test_receipt_append_refuses_a_directory_without_creating_children(tmp_path: Path) -> None:
    directory = tmp_path / "fixture-directory"
    directory.mkdir()
    with pytest.raises(IsADirectoryError):
        with prune_receipts.open_new(directory, append=True):
            pass
    assert list(directory.iterdir()) == []


def test_collect_writes_only_into_a_new_destination(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    (out / "f-case").mkdir(parents=True)
    (out / "f-case" / "error-context.md").write_text("context")
    dest = tmp_path / "e2e-failures"
    dest.mkdir()
    (dest / "f-case").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert prune_receipts.collect_failures(out, dest) == 0
    assert not (tmp_path / "elsewhere").exists()


def test_bounding_never_writes_or_counts_through_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note").write_text("keep me")
    (outside / "big.zip").write_bytes(b"t" * 1000)
    evidence = tmp_path / "e2e-failures"
    (evidence / "g-case").mkdir(parents=True)
    (evidence / "g-case" / "trace.zip").write_bytes(b"t" * 300)
    (evidence / "OMITTED.txt").symlink_to(outside / "note")
    (evidence / "g-case" / "linked.zip").symlink_to(outside / "big.zip")
    prune_receipts.bound_artifacts(evidence, cap=100)
    assert (outside / "note").read_text() == "keep me"
    assert (outside / "big.zip").stat().st_size == 1000
    assert not (evidence / "OMITTED.txt").is_symlink()
    assert (evidence / "OMITTED.txt").read_text() == "g-case/trace.zip\n"


@pytest.mark.parametrize("cap", [0, 100])
def test_a_symlinked_note_is_removed_even_when_nothing_is_dropped(tmp_path: Path, cap: int) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    evidence = tmp_path / "e2e-failures"
    (evidence / "h-case").mkdir(parents=True)
    if cap == 0:
        (evidence / "h-case" / "trace.zip").write_bytes(b"t" * 10)
    (evidence / "OMITTED.txt").symlink_to(outside)
    prune_receipts.bound_artifacts(evidence, cap=cap)
    assert outside.read_text() == "keep me"
    assert not (evidence / "OMITTED.txt").is_symlink()


def test_a_copy_never_passes_the_allowance_it_was_given(tmp_path: Path) -> None:
    source = tmp_path / "trace.zip"
    source.write_bytes(b"t" * 100)
    (tmp_path / "dest").mkdir()
    root = os.open(tmp_path / "dest", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert prune_receipts.copy_new(source, root, Path("a") / "trace.zip", allowance=99) is None
        assert prune_receipts.copy_new(source, root, Path("a") / "trace.zip", allowance=100) == 100
    finally:
        os.close(root)
    assert (tmp_path / "dest" / "a" / "trace.zip").stat().st_size == 100


def test_omission_note_uses_utf8_bytes_and_never_writes_a_partial_line(tmp_path: Path) -> None:
    item = tmp_path / "café.png"
    line = "café.png\n".encode()
    prune_receipts.note_omitted(tmp_path, [item], room=len(line) - 1)
    note = tmp_path / "OMITTED.txt"
    assert not note.exists()
    prune_receipts.note_omitted(tmp_path, [item], room=len(line))
    assert note.read_bytes() == line


def test_omission_note_preserves_an_existing_line_when_the_next_wont_fit(tmp_path: Path) -> None:
    note = tmp_path / "OMITTED.txt"
    note.write_bytes(b"first.png\n")
    paths = [tmp_path / "second.png", tmp_path / "third.png"]
    limit = len(b"first.png\nsecond.png\n")
    prune_receipts.note_omitted(tmp_path, paths, room=limit)
    assert note.read_bytes() == b"first.png\nsecond.png\n"


def test_a_source_that_grows_after_it_is_opened_is_copied_at_its_opened_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "trace.zip"
    source.write_bytes(b"t" * 100)
    (tmp_path / "dest").mkdir()
    real_fstat = os.fstat
    grown = []

    def fstat_then_grow(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if not grown:
            grown.append(True)
            with source.open("ab") as out:
                out.write(b"g" * 1000)
        return result

    monkeypatch.setattr(prune_receipts.os, "fstat", fstat_then_grow)
    root = os.open(tmp_path / "dest", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert prune_receipts.copy_new(source, root, Path("trace.zip"), allowance=100) == 100
    finally:
        os.close(root)
    assert grown
    assert source.stat().st_size == 1100
    assert (tmp_path / "dest" / "trace.zip").read_bytes() == b"t" * 100


@pytest.mark.parametrize(("paths", "room"), [([], 100), (["i-case/trace.zip"], 0)])
def test_the_note_writer_removes_a_symlinked_note_before_it_returns(
    tmp_path: Path, paths: list[str], room: int
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    evidence = tmp_path / "e2e-failures"
    evidence.mkdir()
    (evidence / "OMITTED.txt").symlink_to(outside)
    prune_receipts.note_omitted(evidence, [evidence / path for path in paths], room)
    assert outside.read_text() == "keep me"
    assert not (evidence / "OMITTED.txt").exists()
    assert not (evidence / "OMITTED.txt").is_symlink()


def test_collect_never_writes_through_a_linked_directory_below_the_destination(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    (out / "i-case").mkdir(parents=True)
    (out / "i-case" / "error-context.md").write_text("context")
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "e2e-failures"
    dest.mkdir()
    (dest / "i-case").symlink_to(outside, target_is_directory=True)
    root = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            prune_receipts.copy_new(out / "i-case" / "error-context.md", root, Path("i-case") / "error-context.md", 1000)
    finally:
        os.close(root)
    assert list(outside.iterdir()) == []


def test_collect_needs_a_real_parent(tmp_path: Path) -> None:
    out = tmp_path / "browser-results"
    (out / "j-case").mkdir(parents=True)
    (out / "j-case" / "error-context.md").write_text("context")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked-run").symlink_to(outside, target_is_directory=True)
    assert prune_receipts.collect_failures(out, tmp_path / "linked-run" / "e2e-failures") == 0
    assert prune_receipts.collect_failures(out, tmp_path / "missing-run" / "e2e-failures") == 0
    assert list(outside.iterdir()) == [] and not (tmp_path / "missing-run").exists()


def test_a_directory_swapped_for_a_link_after_it_is_opened_does_not_redirect_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy writes through the descriptors it holds, not through path names it would look up again."""
    source = tmp_path / "trace.zip"
    source.write_bytes(b"t" * 10)
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    real_open_dir = prune_receipts.open_dir
    swapped: list[str] = []

    def open_dir_then_swap(name, parent=None):
        descriptor = real_open_dir(name, parent)
        if name == "a" and not swapped:
            swapped.append(name)
            (dest / "a").rename(dest / "a-moved")
            (dest / "a").symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(prune_receipts, "open_dir", open_dir_then_swap)
    root = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        written = prune_receipts.copy_new(source, root, Path("a") / "b" / "trace.zip", allowance=100)
    finally:
        os.close(root)
    assert swapped, "the copy no longer opens its directories one by one"
    assert list((outside / "b").iterdir()) == [], "the copy wrote through the swapped link"
    assert written == 10
    assert (dest / "a-moved" / "b" / "trace.zip").read_bytes() == b"t" * 10


def test_catalog_rejects_an_unclassified_source_key(tmp_path: Path) -> None:
    from unittest import TestCase
    import gen_operation_catalog as catalog

    source = tmp_path / "words.yml"
    source.write_text("".join(f"{name}: {{}}\n" for name, _ in catalog.PROJECTED + catalog.UNEXPRESSED)
                      + "new_policy: {}\n")
    with TestCase().assertRaisesRegex(ValueError, "^Every source top-level key must be classified$"):
        catalog.generate(source)


def test_catalog_rejects_a_blank_projected_family(tmp_path: Path) -> None:
    from unittest import TestCase
    import gen_operation_catalog as catalog

    source = tmp_path / "words.yml"
    source.write_text("".join(
        'protocol_words: {sample: "   "}\n' if name == "protocol_words" else f"{name}: {{}}\n"
        for name, _ in catalog.PROJECTED + catalog.UNEXPRESSED
    ))
    with TestCase().assertRaisesRegex(ValueError, "^Projected words and families must be text$"):
        catalog.generate(source)


def _projected_catalog_source(tmp_path: Path) -> Path:
    source = tmp_path / "invented-words.yml"
    source.write_text(
        "schema_version: 1\n"
        "type_capabilities: {shared: type-family}\n"
        "route_intents: {shared: route-family}\n"
        "protocol_words: {zulu: first-family, shared: protocol-family}\n"
        "modality_words: {not-an-operation: Display only}\n"
        "media_type_modalities: {}\n"
        "capabilities: {}\n"
        "audio_rule: {}\n",
        encoding="utf-8",
    )
    return source


def test_catalog_keeps_source_order_within_each_context(tmp_path: Path) -> None:
    import json

    import gen_operation_catalog as catalog

    result = json.loads(catalog.generate(_projected_catalog_source(tmp_path)))
    assert [(row["context"], row["order"], row["word"], row["family"]) for row in result["relations"]] == [
        ("protocol", 0, "zulu", "first-family"),
        ("protocol", 1, "shared", "protocol-family"),
        ("route", 0, "shared", "route-family"),
        ("type", 0, "shared", "type-family"),
    ]


def test_catalog_discloses_unprojected_vocabulary_without_emitting_it(tmp_path: Path) -> None:
    import json

    import gen_operation_catalog as catalog

    source = _projected_catalog_source(tmp_path)
    result = json.loads(catalog.generate(source))
    assert result["operations"] == []
    assert all(row["word"] != "not-an-operation" for row in result["relations"])
    gap, = (row for row in result["unexpressed"] if row["key"] == "modality_words")
    assert gap["source"] == source.name
    assert gap["reason"] == "Presentation vocabulary for modalities."
