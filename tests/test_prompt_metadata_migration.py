"""Privacy migration checks use real containers and isolated cache directories."""
import asyncio
import io
import os
import shutil
import struct
import subprocess
import threading
from unittest.mock import patch

import pytest
from PIL import Image

from gateway import media_metadata as mm
from gateway import prompt_metadata_migration as migration

SECRET = "legacy-private-prompt-729b1"


def image_bytes(fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (12, 10), "red").save(out, fmt)
    return mm.embed_image_metadata(out.getvalue(), "." + fmt.lower(), {"prompt": SECRET, "workflow": SECRET})


def asset(cache, number=1, suffix=".png", content=None):
    cache.mkdir(exist_ok=True)
    digest = f"{number:064x}"
    path = cache / (digest + suffix)
    path.write_bytes(image_bytes() if content is None else content)
    return path, "img_" + digest


@pytest.mark.parametrize("fmt,suffix", [("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")])
def test_image_migration_preserves_pixels_filename_and_resume(tmp_path, fmt, suffix):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    source = image_bytes(fmt)
    path, _ = asset(cache, suffix=suffix, content=source)
    before = Image.open(io.BytesIO(source)).convert("RGB").tobytes()
    result = migration.strip_cached_assets(cache, state)
    assert result.complete and result.stripped == 1
    assert SECRET.encode() not in path.read_bytes()
    assert Image.open(path).convert("RGB").tobytes() == before
    with patch.object(mm, "strip_metadata", side_effect=AssertionError("already migrated")):
        resumed = migration.strip_cached_assets(cache, state)
    assert resumed.complete and resumed.already_clean == 1
    assert SECRET.encode() not in state.read_bytes()


def test_owned_selection_batches_throttle_and_new_writes(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    first, first_id = asset(cache, 1)
    second, _ = asset(cache, 2)
    with patch.object(migration.time, "sleep") as sleep:
        result = migration.strip_cached_assets(cache, state, asset_ids=[first_id], throttle_seconds=0.1)
    sleep.assert_called_once_with(0.1)
    assert result.complete and result.scanned == 1
    assert SECRET.encode() not in first.read_bytes()
    assert SECRET.encode() in second.read_bytes()
    result = migration.strip_cached_assets(cache, state, max_files=0)
    assert result.pending == 1 and not result.complete
    result = migration.strip_cached_assets(cache, state, max_files=1)
    assert result.complete
    first.write_bytes(image_bytes())
    assert migration.strip_cached_assets(cache, state).stripped == 1


def test_concurrent_replace_is_preserved_and_retried(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    path, _ = asset(cache)
    real = mm.strip_metadata
    replacement = image_bytes("PNG") + b"replacement"
    def racing_strip(content, suffix):
        other = cache / "replace.tmp"
        other.write_bytes(replacement)
        os.replace(other, path)
        return real(content, suffix)
    with patch.object(mm, "strip_metadata", racing_strip):
        result = migration.strip_cached_assets(cache, state)
    assert result.pending == 1 and path.read_bytes() == replacement
    assert not list(cache.glob(".metadata-clean-*"))
    assert migration.strip_cached_assets(cache, state).complete


def test_concurrent_delete_and_strip_failure_cleanup(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    path, _ = asset(cache)
    real = mm.strip_metadata
    def deleting_strip(content, suffix):
        path.unlink()
        return real(content, suffix)
    with patch.object(mm, "strip_metadata", deleting_strip):
        assert migration.strip_cached_assets(cache, state).complete
    path, _ = asset(cache)
    with patch.object(migration.os, "replace", side_effect=OSError("denied")):
        result = migration.strip_cached_assets(cache, state)
    assert result.pending == 1 and SECRET.encode() in path.read_bytes()
    assert not list(cache.glob(".metadata-clean-*"))
    assert migration.strip_cached_assets(cache, state).complete


def test_unsupported_malformed_symlink_and_invalid_id_are_honest(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    path, _ = asset(cache, suffix=".gif", content=b"GIF89a" + SECRET.encode())
    asset(cache, 2, content=b"broken" + SECRET.encode())
    digest = f"{3:064x}"
    (cache / (digest + ".png")).symlink_to(path)
    result = migration.strip_cached_assets(cache, state)
    assert result.pending == 3 and len(result.errors) == 3
    with pytest.raises(ValueError):
        migration.strip_cached_assets(cache, state, asset_ids=["img_../../secret"])


def test_stopped_scan_leaves_resumable_pending(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    asset(cache)
    stop = threading.Event()
    stop.set()
    assert migration.strip_cached_assets(cache, state, stop_event=stop).pending == 1
    assert migration.strip_cached_assets(cache, state).complete


@pytest.mark.parametrize("fmt,suffix", [("JPEG", ".jpg"), ("WEBP", ".webp")])
def test_metadata_restore_preserves_compressed_payload(fmt, suffix):
    original = io.BytesIO()
    Image.new("RGB", (32, 24), "purple").save(original, fmt, quality=93)
    clean = original.getvalue()
    full = mm.embed_image_metadata(clean, suffix, {"prompt": SECRET})
    assert Image.open(io.BytesIO(full)).getexif()[0x0110] == "prompt:" + SECRET
    assert Image.open(io.BytesIO(full)).convert("RGB").tobytes() == Image.open(io.BytesIO(clean)).convert("RGB").tobytes()
    # WebP may need VP8X to advertise EXIF, but its VP8/VP8L bytes stay exact.
    if fmt == "JPEG":
        assert mm.strip_metadata(full, suffix) == mm.strip_metadata(clean, suffix)
    else:
        assert clean[20:] in full


def test_wav_clean_bytes_unchanged_without_ffmpeg():
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16)
    clean = out.getvalue()
    payload = b"INFO" + b"ICMT" + struct.pack("<I", len(SECRET) + 1) + SECRET.encode() + b"\0"
    if len(payload) & 1:
        payload += b"\0"
    body = clean[8:] + b"LIST" + struct.pack("<I", len(payload)) + payload
    tagged = b"RIFF" + struct.pack("<I", len(body)) + body
    with patch.object(mm, "_run", side_effect=AssertionError("no ffmpeg")):
        assert mm.strip_metadata(clean, ".wav") == clean
        assert mm.strip_metadata(tagged, ".wav") == clean


def test_memfd_cleanup_on_tool_error(tmp_path):
    saved_fds = []
    def broken(cmd, *, pass_fds=()):
        saved_fds.extend(pass_fds)
        assert all("/proc/self/fd/" in part for part in cmd if "azizam" in part)
        raise mm.MediaMetadataError("failed")
    with patch.object(mm, "_run", broken), patch.object(mm.shutil, "which", return_value="ffmpeg"):
        with pytest.raises(mm.MediaMetadataError):
            mm.strip_metadata(SECRET.encode(), ".mp4", workdir=tmp_path)
    assert not list(tmp_path.iterdir())
    for fd in saved_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
@pytest.mark.parametrize("suffix,codec", [(".mp4", "aac"), (".webm", "libopus"), (".mov", "aac"), (".mkv", "flac"), (".m4a", "aac"), (".mp3", "libmp3lame"), (".flac", "flac"), (".ogg", "libvorbis"), (".wav", "pcm_s16le"), (".aac", "aac")])
def test_media_containers_migrate_without_prompt_scratch(tmp_path, suffix, codec):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    cache.mkdir()
    sample = tmp_path / ("fixture" + suffix)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.03:r=24000", "-c:a", codec, str(sample)], check=True, capture_output=True)
    content = mm.embed_media_tags(sample.read_bytes(), suffix, {"prompt": SECRET})
    path, _ = asset(cache, suffix=suffix, content=content)
    result = migration.strip_cached_assets(cache, state)
    assert result.complete
    assert SECRET.encode() not in path.read_bytes()
    assert SECRET not in str(mm.read_media_tags(path.read_bytes(), suffix))
    assert not list(cache.glob(".metadata-clean-*"))


def test_permanent_failure_does_not_starve_next_batch(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    asset(cache, 1, content=b"malformed image")
    good, _ = asset(cache, 2)
    first = migration.strip_cached_assets(cache, state, max_files=1)
    assert first.pending == 2
    second = migration.strip_cached_assets(cache, state, max_files=1)
    assert second.stripped == 1 and second.pending == 1
    assert SECRET.encode() not in good.read_bytes()


def test_jpeg_trailing_text_is_removed():
    source = image_bytes("JPEG") + SECRET.encode()
    clean = mm.strip_metadata(source, ".jpg")
    assert SECRET.encode() not in clean
    Image.open(io.BytesIO(clean)).verify()


def test_background_worker_resumes_bounded_batches(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    asset(cache, 1)
    asset(cache, 2)
    result = asyncio.run(migration.background_migration(cache, state, batch_size=1,
                                                      throttle_seconds=0, retry_seconds=0))
    assert result.complete
    assert migration.strip_cached_assets(cache, state).already_clean == 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
@pytest.mark.parametrize("suffix,codec", [(".mp3", "libmp3lame"), (".flac", "flac")])
def test_speech_container_strip_needs_no_ffmpeg_and_is_idempotent(tmp_path, suffix, codec):
    sample = tmp_path / ("speech" + suffix)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.03:r=24000", "-c:a", codec, "-metadata", "comment=" + SECRET, str(sample)], check=True, capture_output=True)
    with patch.object(mm, "_run", side_effect=AssertionError("no ffmpeg strip")):
        clean = mm.strip_metadata(sample.read_bytes(), suffix)
        assert SECRET.encode() not in clean
        assert mm.strip_metadata(clean, suffix) == clean


def test_legacy_tool_scratch_purged_without_touching_unknown_names(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    scratch = cache / "media_metadata"
    scratch.mkdir(parents=True)
    for purpose in ("in", "out", "probe"):
        (scratch / ("a" * 32 + "." + purpose + ".mp4")).write_text(SECRET)
    other = scratch / "operator-file.mp4"
    other.write_text("keep this file")
    result = migration.strip_cached_assets(cache, state)
    assert result.complete and result.stripped == 3
    assert list(scratch.iterdir()) == [other]


def test_owned_stale_staging_removed_recent_staging_pending(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    _, asset_id = asset(cache, 1)
    staging = cache / ("0" * 63 + "1.png.tmp")
    staging.write_text(SECRET)
    recent = migration.strip_cached_assets(cache, state, asset_ids=[asset_id])
    assert not recent.complete and recent.errors[staging.name] == "staging_file_recent"
    os.utime(staging, (0, 0))
    assert migration.strip_cached_assets(cache, state, asset_ids=[]).complete
    assert staging.exists()
    assert migration.strip_cached_assets(cache, state, asset_ids=[asset_id]).complete
    assert not staging.exists()


def test_legacy_scratch_failure_is_honest(tmp_path):
    cache, state = tmp_path / "assets", tmp_path / "state.db"
    scratch = cache / "media_metadata"
    scratch.mkdir(parents=True)
    # A directory with a recognized scratch name cannot safely be unlinked.
    (scratch / ("b" * 32 + ".in.mp4")).mkdir()
    result = migration.strip_cached_assets(cache, state)
    assert result.pending == 1 and not result.complete
