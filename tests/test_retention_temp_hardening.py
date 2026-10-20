"""Regression checks for explicit scratch-directory bypasses and allocation ownership."""
import asyncio
import ctypes
import io
import os
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from starlette.datastructures import Headers

from gateway import media_metadata, private_tmp, prompt_metadata_migration as migration
from gateway import stt_chunking as stt, stt_measurement, thumbnails, video_inputs
from tests.support.stt_pcm import planning_pcm


def test_video_probe_rejects_external_workdir_before_writing(tmp_path, monkeypatch):
    root, outside = tmp_path / "private", tmp_path / "outside"
    root.mkdir()
    monkeypatch.setattr(private_tmp, "_active", root)
    monkeypatch.setattr(video_inputs.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(video_inputs.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(private_tmp.PrivateTmpError):
        video_inputs.probe_video(b"request-video", workdir=outside)
    assert not outside.exists()


def test_video_probe_rejects_symlink_escape(tmp_path, monkeypatch):
    root, outside = tmp_path / "private", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(private_tmp, "_active", root)
    monkeypatch.setattr(video_inputs.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(video_inputs.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(private_tmp.PrivateTmpError):
        video_inputs.probe_video(b"request-video", workdir=root / "escape")
    assert list(outside.iterdir()) == []


def test_video_probe_default_ignores_process_tempdir(tmp_path, monkeypatch):
    root, outside = tmp_path / "private", tmp_path / "os-tmp"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(private_tmp, "_active", root)
    monkeypatch.setattr(tempfile, "tempdir", str(outside))
    monkeypatch.setattr(video_inputs.shutil, "which", lambda _: "ffprobe")
    seen = []

    def probe(command, **kwargs):
        path = Path(command[-1])
        assert path.is_relative_to(root)
        assert path.read_bytes() == b"request-video"
        seen.append(path)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(video_inputs.subprocess, "run", probe)
    assert video_inputs.probe_video(b"request-video") is None
    assert seen and not seen[0].exists()
    assert list(outside.iterdir()) == []


def test_metadata_staging_is_private_even_for_a_public_asset(tmp_path, monkeypatch):
    path = tmp_path / ("a" * 64 + ".png")
    image = io.BytesIO()
    Image.new("RGB", (2, 2)).save(image, "PNG")
    path.write_bytes(media_metadata.embed_image_metadata(image.getvalue(), ".png", {"prompt": "private"}))
    path.chmod(0o644)
    replace = os.replace
    modes = []

    def inspect(source, target):
        modes.append(stat.S_IMODE(Path(source).stat().st_mode))
        replace(source, target)

    monkeypatch.setattr(migration.os, "replace", inspect)
    assert migration.strip_cached_assets(tmp_path, tmp_path / "state.db").complete
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_metadata_migration_sweeps_orphans_even_in_a_bounded_selected_run(tmp_path):
    orphan = tmp_path / ".metadata-clean-orphan"
    orphan.write_bytes(b"retained request media")
    unrelated = tmp_path / "keep-me"
    unrelated.write_bytes(b"durable")
    result = migration.strip_cached_assets(tmp_path, tmp_path / "state.db", asset_ids=[], max_files=0)
    assert not orphan.exists()
    assert unrelated.read_bytes() == b"durable"
    assert result.complete


def test_metadata_orphan_sweep_never_follows_symlinks(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    target = tmp_path / "keep"
    target.write_bytes(b"durable")
    (cache / ".metadata-clean-link").symlink_to(target)
    migration.strip_cached_assets(cache, tmp_path / "state.db")
    assert not (cache / ".metadata-clean-link").is_symlink()
    assert target.read_bytes() == b"durable"


def test_metadata_orphan_unlink_failure_stays_pending(tmp_path, monkeypatch):
    orphan = tmp_path / ".metadata-clean-orphan"
    orphan.write_bytes(b"retained media")
    unlink = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == orphan:
            raise PermissionError("cannot unlink")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    result = migration.strip_cached_assets(tmp_path, tmp_path / "state.db")
    assert not result.complete
    assert result.errors == {orphan.name: "PermissionError"}


def test_metadata_sweep_waits_for_an_active_migration(tmp_path, monkeypatch):
    path = tmp_path / ("b" * 64 + ".png")
    out = io.BytesIO()
    Image.new("RGB", (2, 2)).save(out, "PNG")
    path.write_bytes(media_metadata.embed_image_metadata(out.getvalue(), ".png", {"prompt": "private"}))
    staged, release, second_started = threading.Event(), threading.Event(), threading.Event()
    replace = os.replace
    errors = []

    def pause(source, target):
        staged.set()
        assert release.wait(10)
        assert Path(source).is_file(), "another scan must not sweep active staging"
        replace(source, target)

    monkeypatch.setattr(migration.os, "replace", pause)

    def scan(second=False):
        try:
            if second:
                second_started.set()
            assert migration.strip_cached_assets(tmp_path, tmp_path / "state.db").complete
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=scan)
    second = threading.Thread(target=scan, args=(True,))
    first.start()
    try:
        assert staged.wait(10)
        second.start()
        assert second_started.wait(10)
        acquired = migration._SCAN_LOCK.acquire(blocking=False)
        if acquired:
            migration._SCAN_LOCK.release()
        assert not acquired, "active staging must hold the scan lock"
    finally:
        release.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []


def execution(root):
    return stt.SttExecutionConfig.from_service(service_id="test", config_directory=root,
        service_config={"chunking": {"spool_dir": str(root / "operator-spool")}})


@pytest.mark.parametrize("escape", ["outside", "symlink", "parent", "spool_root"])
def test_stt_parser_refuses_unconfigured_workspace(tmp_path, escape):
    config = execution(tmp_path)
    config.spool_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = config.spool_dir / "link"
    link.symlink_to(outside, target_is_directory=True)
    workspace = {"outside": outside, "symlink": link, "parent": config.spool_dir / ".." / "outside",
                 "spool_root": config.spool_dir}[escape]
    with pytest.raises(stt.SttConfigurationError):
        stt.DiskBoundedMultiPartParser(Headers(), None, config=config, workspace=workspace)
    assert list(outside.iterdir()) == []


def test_stt_parser_accepts_validated_absolute_operator_spool(tmp_path):
    config = execution(tmp_path)
    workspace = stt.create_request_workspace(config)
    parser = stt.DiskBoundedMultiPartParser(Headers(), None, config=config, workspace=workspace)
    assert parser._workspace == workspace


@pytest.mark.parametrize("helper,escape", [
    (helper, escape) for helper in ("decode", "extract") for escape in ("source", "destination", "symlink")
] + [("guard", "source"), ("execute", "source")])
def test_stt_media_helpers_refuse_escape_before_command_or_unlink(tmp_path, monkeypatch, helper, escape):
    config = execution(tmp_path)
    workspace = stt.create_request_workspace(config)
    other = stt.create_request_workspace(config)
    outside = tmp_path / "outside"
    outside.mkdir()
    source = workspace / "upload.wav"
    source.write_bytes(b"request-audio")
    destination = workspace / "derived.wav"
    if escape == "source":
        source = outside / "upload.wav"
        source.write_bytes(b"request-audio")
    elif escape == "destination":
        destination = other / "derived.wav"
    else:
        destination.symlink_to(outside / "derived.wav")
    command = AsyncMock(side_effect=AssertionError("command must not start"))
    monkeypatch.setattr(stt, "run_media_command", command)
    sentinel = source.parent / "planning-16k.pcm"
    sentinel.write_bytes(b"keep")
    uncut = source.parent / "uncut-0-source.pcm"
    uncut.write_bytes(b"keep")

    async def run():
        kwargs = dict(config=config, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 5, on_cancel=None)
        with pytest.raises(stt.SttConfigurationError):
            if helper == "decode":
                await stt.decode_planning_pcm(source, destination, **kwargs)
            elif helper == "extract":
                await stt.extract_segment(source, destination, start_seconds=0, end_seconds=1, **kwargs)
            elif helper == "guard":
                from dataclasses import replace
                await stt.guard_uncut_uploads([source], **{**kwargs, "config": replace(config, max_segment_declared=True)})
            else:
                await stt.execute_chunking(source_path=source, source_stem="upload", fields=[], post_file=AsyncMock(), **kwargs)

    asyncio.run(run())
    command.assert_not_awaited()
    assert sentinel.read_bytes() == b"keep"
    assert uncut.read_bytes() == b"keep"


@pytest.mark.parametrize("collision", ["source", "symlink"])
@pytest.mark.parametrize("owned_cleanup", [False, True])
def test_rejected_segment_is_never_owned_or_unlinked(tmp_path, monkeypatch, collision, owned_cleanup):
    from dataclasses import replace
    config = replace(execution(tmp_path), threshold_seconds=30, target_seconds=20,
                     max_segment_seconds=40, max_segment_declared=True)
    workspace = stt.create_request_workspace(config)
    segment = workspace / "segment-000.wav"
    source = segment if collision == "source" else workspace / "upload.part"
    source.write_bytes(b"original-request-media")
    sentinel = tmp_path / "keep"
    sentinel.write_bytes(b"durable")
    if collision == "symlink":
        segment.symlink_to(sentinel)
    command = AsyncMock(side_effect=AssertionError("invalid extraction must not start"))
    post = AsyncMock()
    monkeypatch.setattr(stt, "run_media_command", command)
    monkeypatch.setattr(stt, "probe_audio", AsyncMock(return_value=stt.ProbedAudio(45, 16000, 1)))

    async def run():
        cleanup = stt.SttCleanupOwner() if owned_cleanup else None
        try:
            with planning_pcm(45), pytest.raises(stt.SttConfigurationError):
                await stt.execute_chunking(source_path=source, source_stem="upload", fields=[], config=config,
                    post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10,
                    on_cancel=None, cleanup=cleanup)
        finally:
            if cleanup is not None:
                await cleanup.aclose()

    asyncio.run(run())
    assert source.read_bytes() == b"original-request-media"
    if collision == "symlink":
        assert segment.is_symlink()
    assert sentinel.read_bytes() == b"durable"
    command.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.parametrize("failure", ["native", "libc"])
def test_memfd_unavailable_fails_closed_without_disk_fallback(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    if failure == "native":
        def unavailable(*args):
            raise OSError("memfd disabled")
        monkeypatch.setattr(os, "memfd_create", unavailable, raising=False)
        monkeypatch.setattr(os, "MFD_CLOEXEC", media_metadata._MFD_CLOEXEC, raising=False)
    else:
        monkeypatch.delattr(os, "memfd_create", raising=False)
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    with pytest.raises((OSError, media_metadata.MediaMetadataError)):
        with media_metadata._media_buffers(b"request-media"):
            pytest.fail("memfd failure must not produce buffers")
    assert list(tmp_path.iterdir()) == []


def test_measurement_scratch_is_private_and_repairs_mode_under_lease(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    scratch = root / "stt-measurement-work"
    scratch.mkdir(mode=0o755)
    monkeypatch.setattr(private_tmp, "_active", root)
    runner = stt_measurement.Measurements(SimpleNamespace(path=tmp_path / "durable" / "admin.db"), None)
    monkeypatch.setattr(runner, "_save", lambda _: None)
    seen = []

    async def measure(ctx, job, policy, workspace):
        seen.append(workspace)
        assert workspace.is_relative_to(scratch)
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert runner._lease_fd is not None
        (workspace / "audio.wav").write_bytes(b"request-audio")
        return {}

    monkeypatch.setattr(runner, "_measure", measure)
    job = {}
    asyncio.run(runner._run(None, job, {"deadline_seconds": 5}))
    assert job["status"] == "succeeded"
    assert seen and not seen[0].exists()
    assert runner.path == tmp_path / "durable" / "stt_measurements.db"
    assert not (runner.path.parent / "stt-measurement-work").exists()


def test_measurement_private_orphans_wait_for_worker_lease(tmp_path, monkeypatch):
    root = tmp_path / "private"
    orphan = root / "stt-measurement-work" / "job-orphan"
    orphan.mkdir(parents=True)
    (orphan / "partial").write_bytes(b"request-audio")
    monkeypatch.setattr(private_tmp, "_active", root)
    runner = stt_measurement.Measurements(SimpleNamespace(path=tmp_path / "durable" / "admin.db"), None)
    with runner._workspace_lease():
        runner.startup_cleanup()
        assert orphan.exists()
    runner.startup_cleanup()
    assert not orphan.exists()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_first_frame_keeps_allocation_ownership_on_unexpected_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(thumbnails.tempfile, "tempdir", str(tmp_path))
    def fail(*a, **k):
        assert list(tmp_path.glob("*.png")), "positive control: frame allocated"
        raise failure("interrupted")
    monkeypatch.setattr(thumbnails.subprocess, "run", fail)
    with pytest.raises(failure):
        thumbnails._first_frame(tmp_path / "source.mp4")
    assert list(tmp_path.glob("*.png")) == []


def test_first_frame_transfers_successful_allocation_to_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    def succeed(command, **kwargs):
        Path(command[-1]).write_bytes(b"frame")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(thumbnails.subprocess, "run", succeed)
    frame = thumbnails._first_frame(tmp_path / "source.mp4")
    assert frame.read_bytes() == b"frame"
    frame.unlink()
