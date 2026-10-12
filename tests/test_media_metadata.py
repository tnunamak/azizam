"""Hermetic tests for gateway.media_metadata (lossless strip/embed).

Real PNG/JPEG/WebP bytes come from Pillow; audio/video from ffmpeg lavfi.
ffmpeg-dependent tests skip when ffmpeg/ffprobe/the codec is missing.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from gateway import media_metadata as mm  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SKIP_FF = "ffmpeg/ffprobe not installed"
BIG = json.dumps({"k": "x" * 30000, "n": [1, 2, 3]})


def _image(size=(32, 24)) -> Image.Image:
    img = Image.new("RGB", size)
    img.putdata([((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
                 for y in range(size[1]) for x in range(size[0])])
    return img


def _png_with_text() -> bytes:
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("prompt", '{"1": {}}')
    info.add_text("workflow", '{"nodes": []}')
    info.add_itxt("note", "café")
    info.add_text("z", "zipped", zip=True)
    buf = io.BytesIO()
    _image().save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def _chunks(png: bytes):
    pos = 8
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        yield png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        pos += 12 + length


def _idat(png: bytes) -> bytes:
    return b"".join(d for t, d in _chunks(png) if t == b"IDAT")


def _px(data: bytes):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.mode, img.size, list(img.convert("RGBA").getdata())


def _jpeg_segments(data: bytes):
    """Return ([(marker, payload)], scan_bytes_list)."""
    segs, scans, pos = [], [], 2
    while pos < len(data):
        assert data[pos] == 0xFF
        m = data[pos + 1]
        pos += 2
        if m == 0xD9:
            break
        ln = struct.unpack(">H", data[pos:pos + 2])[0]
        segs.append((m, data[pos + 2:pos + ln]))
        pos += ln
        if m == 0xDA:
            start = pos
            while not (data[pos] == 0xFF and data[pos + 1] not in (0, 0xFF)
                       and not 0xD0 <= data[pos + 1] <= 0xD7):
                pos += 1
            scans.append(data[start:pos])
    return segs, scans


def _riff_chunks(data: bytes):
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
    pos, out = 12, []
    while pos < len(data):
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        out.append((data[pos:pos + 4], data[pos + 8:pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return out


class KindTests(unittest.TestCase):
    def test_media_kind(self):
        cases = {
            ".png": "png", "image/png": "png", "PNG": "png", ".jpg": "jpeg",
            ".jpeg": "jpeg", "image/jpeg": "jpeg", ".webp": "webp",
            "image/webp": "webp", ".mp4": "video", "video/webm": "video",
            ".mov": "video", ".mkv": "video", ".webm": "video", ".wav": "audio",
            ".flac": "audio", ".mp3": "audio", ".ogg": "audio", ".m4a": "audio",
            "audio/mpeg": "audio", ".txt": None, "": None, "text/plain": None,
        }
        for key, want in cases.items():
            self.assertEqual(mm.media_kind(key), want, key)

    def test_unknown_unchanged(self):
        self.assertEqual(mm.strip_metadata(b"abc", ".txt"), b"abc")
        self.assertEqual(mm.strip_metadata(b"abc", ".png"), b"abc")
        self.assertEqual(mm.strip_metadata(b"abc", ".jpg"), b"abc")
        self.assertEqual(mm.strip_metadata(b"abc", ".webp"), b"abc")


class PngTests(unittest.TestCase):
    def test_strip_keeps_pixels_and_idat(self):
        src = _png_with_text()
        self.assertIn("prompt", mm.png_text_chunks(src))
        out = mm.strip_metadata(src, ".png")
        self.assertEqual(mm.png_text_chunks(out), {})
        self.assertEqual(_idat(out), _idat(src))
        self.assertEqual(_px(out), _px(src))
        types = {t for t, _ in _chunks(out)}
        self.assertFalse(types & {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})
        self.assertTrue({b"IHDR", b"IDAT", b"IEND"} <= types)

    def test_strip_keeps_ancillary_non_metadata(self):
        src = _png_with_text()
        extra = mm._png_chunk(b"gAMA", struct.pack(">I", 45455)) + \
            mm._png_chunk(b"tIME", b"\x07\xe9\x01\x01\x00\x00\x00") + \
            mm._png_chunk(b"pHYs", struct.pack(">IIB", 1, 1, 1))
        first_idat = src.index(b"IDAT") - 4
        src = src[:first_idat] + extra + src[first_idat:]
        out = mm.strip_metadata(src, "image/png")
        types = [t for t, _ in _chunks(out)]
        self.assertIn(b"gAMA", types)
        self.assertIn(b"pHYs", types)
        self.assertNotIn(b"tIME", types)

    def test_embed_roundtrip_and_replace(self):
        src = _png_with_text()
        out = mm.embed_png_text(src, {"prompt": "new", "azizam": BIG, "u": "café ☃"})
        got = mm.png_text_chunks(out)
        self.assertEqual(got["prompt"], "new")
        self.assertEqual(got["azizam"], BIG)
        self.assertEqual(got["u"], "café ☃")
        self.assertEqual(got["workflow"], '{"nodes": []}')  # kept
        self.assertEqual(_idat(out), _idat(src))
        self.assertEqual(_px(out), _px(src))
        types = [t for t, _ in _chunks(out)]
        self.assertEqual(sum(1 for t, d in _chunks(out)
                             if t == b"tEXt" and d.startswith(b"prompt\x00")), 1)
        first_idat = types.index(b"IDAT")
        text_idx = [i for i, t in enumerate(types) if t in (b"tEXt", b"iTXt")]
        self.assertTrue(all(i < first_idat for i in text_idx if
                            _chunks_list(out)[i][1].startswith((b"azizam", b"u\x00"))))
        # PIL agrees
        self.assertEqual(Image.open(io.BytesIO(out)).text["azizam"], BIG)

    def test_embed_on_stripped(self):
        out = mm.embed_png_text(mm.strip_metadata(_png_with_text(), ".png"), {"a": "b"})
        self.assertEqual(mm.png_text_chunks(out), {"a": "b"})


def _chunks_list(png):
    return list(_chunks(png))


class JpegTests(unittest.TestCase):
    def _src(self, **kw) -> bytes:
        img = _image((48, 40))
        exif = mm.exif_bytes({"prompt": "{}", "workflow": "{}"})
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, exif=exif, comment=b"secret comment",
                 icc_profile=_icc(), **kw)
        return buf.getvalue()

    def _check(self, src):
        self.assertTrue(len(Image.open(io.BytesIO(src)).getexif()) > 0)
        segs_in, scans_in = _jpeg_segments(src)
        self.assertIn(0xE1, [m for m, _ in segs_in])
        self.assertIn(0xFE, [m for m, _ in segs_in])
        out = mm.strip_metadata(src, ".jpg")
        segs, scans = _jpeg_segments(out)
        markers = [m for m, _ in segs]
        self.assertNotIn(0xE1, markers)
        self.assertNotIn(0xFE, markers)
        self.assertNotIn(0xED, markers)
        self.assertIn(0xE0, markers)  # JFIF
        self.assertIn(0xE2, markers)  # ICC
        self.assertEqual(len(Image.open(io.BytesIO(out)).getexif()), 0)
        self.assertEqual(scans, scans_in)
        self.assertEqual(_px(out), _px(src))
        self.assertNotIn(b"secret comment", out)
        return out

    def test_strip_baseline(self):
        self._check(self._src())

    def test_strip_progressive_multi_scan(self):
        src = self._src(progressive=True)
        _, scans = _jpeg_segments(src)
        self.assertGreater(len(scans), 1)
        self._check(src)

    def test_strip_with_restart_markers_and_adobe(self):
        src = self._src()
        # inject APP14 Adobe + APP13 before SOS to prove keep / drop
        adobe = b"\xff\xee" + struct.pack(">H", 14) + b"Adobe\x00\x64\x00\x00\x00\x00\x01"
        ps = b"\xff\xed" + struct.pack(">H", 11) + b"Photoshop"
        pos = src.index(b"\xff\xda")
        src = src[:pos] + adobe + ps + src[pos:]
        out = mm.strip_metadata(src, "image/jpeg")
        markers = [m for m, _ in _jpeg_segments(out)[0]]
        self.assertIn(0xEE, markers)
        self.assertNotIn(0xED, markers)

    def test_stuffing_in_scan_preserved(self):
        # noisy image produces many 0xFF00 stuffed bytes
        import random
        rnd = random.Random(3)
        img = Image.new("RGB", (64, 64))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                     for _ in range(64 * 64)])
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95, comment=b"c")
        src = buf.getvalue()
        self.assertIn(b"\xff\x00", src)
        out = mm.strip_metadata(src, ".jpeg")
        self.assertEqual(_jpeg_segments(out)[1], _jpeg_segments(src)[1])
        self.assertEqual(_px(out), _px(src))


def _icc() -> bytes:
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


class WebpTests(unittest.TestCase):
    XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">odd</x:xmpmeta>'  # odd length on purpose

    def _src(self, **kw) -> bytes:
        buf = io.BytesIO()
        _image((33, 21)).save(buf, "WEBP", exif=mm.exif_bytes({"prompt": "{}"}),
                              xmp=self.XMP + b"!" * (len(self.XMP) % 2 == 0), **kw)
        return buf.getvalue()

    def _check(self, src, lossless):
        kinds_in = [t for t, _ in _riff_chunks(src)]
        self.assertIn(b"EXIF", kinds_in)
        self.assertIn(b"XMP ", kinds_in)
        out = mm.strip_metadata(src, ".webp")
        kinds = [t for t, _ in _riff_chunks(out)]  # also asserts RIFF size
        self.assertNotIn(b"EXIF", kinds)
        self.assertNotIn(b"XMP ", kinds)
        vp8x = dict(_riff_chunks(out))[b"VP8X"]
        self.assertEqual(vp8x[0] & 0x0C, 0)
        self.assertEqual(len(Image.open(io.BytesIO(out)).getexif()), 0)
        self.assertEqual(_px(out), _px(src))
        for t, d in _riff_chunks(src):
            if t in (b"VP8 ", b"VP8L"):
                self.assertEqual(dict(_riff_chunks(out))[t], d)
        if lossless:
            self.assertIn(b"VP8L", kinds)
        return out

    def test_lossless(self):
        self._check(self._src(lossless=True), True)

    def test_lossy(self):
        self._check(self._src(quality=80), False)

    def test_animated(self):
        frames = [_image((16, 16)), Image.new("RGB", (16, 16), (200, 10, 10))]
        buf = io.BytesIO()
        frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:],
                       duration=50, lossless=True, exif=mm.exif_bytes({"prompt": "{}"}),
                       xmp=b"<x/>")
        src = buf.getvalue()
        out = mm.strip_metadata(src, "image/webp")
        kinds = [t for t, _ in _riff_chunks(out)]
        self.assertIn(b"ANIM", kinds)
        self.assertIn(b"ANMF", kinds)
        self.assertNotIn(b"EXIF", kinds)
        im_in, im_out = Image.open(io.BytesIO(src)), Image.open(io.BytesIO(out))
        self.assertEqual(im_out.n_frames, 2)
        for i in range(2):
            im_in.seek(i)
            im_out.seek(i)
            self.assertEqual(list(im_in.convert("RGBA").getdata()),
                             list(im_out.convert("RGBA").getdata()))


class ExifTests(unittest.TestCase):
    def test_exif_layout(self):
        exif = Image.Exif()
        exif.load(mm.exif_bytes({"workflow": "W", "prompt": "P", "a": "1", "b": "2"}))
        self.assertEqual(exif[0x010F], "workflow:W")
        self.assertEqual(exif[0x0110], "prompt:P")
        self.assertEqual(exif[0x010E], "a:1\nb:2")

    def test_roundtrip_jpeg_webp(self):
        entries = {"workflow": BIG, "prompt": "{\"a\": 1}", "vivid": "v"}
        blob = mm.exif_bytes(entries)
        for fmt in ("JPEG", "WEBP"):
            buf = io.BytesIO()
            _image().save(buf, fmt, exif=blob)
            got = Image.open(io.BytesIO(buf.getvalue())).getexif()
            self.assertEqual(got[0x010F], "workflow:" + BIG, fmt)
            self.assertEqual(got[0x0110], 'prompt:{"a": 1}', fmt)
            self.assertEqual(got[0x010E], "vivid:v", fmt)


# ------------------------------------------------------------------ ffmpeg

def _ff(*args):
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args],
                          capture_output=True)


def _has_encoder(name: str) -> bool:
    if not HAVE_FFMPEG:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.startswith(" "))


TAGS = ["-metadata", 'prompt={"1": {}}', "-metadata", 'workflow={"nodes": []}']


def test_missing_ffmpeg_fails_closed_even_when_no_codec_is_installed():
    from unittest.mock import patch

    with patch.object(mm.shutil, "which", return_value=None), patch.object(
        mm, "_media_buffers",
        side_effect=AssertionError("must reject before allocating media buffers"),
    ):
        try:
            mm.strip_metadata(b"tagged private media", ".mp4")
        except mm.MediaMetadataError as exc:
            assert str(exc) == "ffmpeg not found"
        else:
            raise AssertionError("missing ffmpeg must never return unstripped bytes")


@unittest.skipUnless(HAVE_FFMPEG, SKIP_FF)
class FfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=Path.home() / ".tmp" if (Path.home() / ".tmp").is_dir() else None)
        cls.tmp = Path(cls._tmp.name)
        cls.work = cls.tmp / "work"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def make(self, name: str, inputs: list[str], codecs: list[str], tags=True) -> bytes:
        path = self.tmp / name
        movflags = ["-movflags", "+use_metadata_tags"] if path.suffix in (".mp4", ".mov", ".m4a") else []
        proc = _ff(*inputs, *codecs, *(TAGS if tags else []), *movflags, str(path))
        if proc.returncode != 0:
            self.skipTest(f"cannot build {name}: {proc.stderr.decode()[-200:]}")
        return path.read_bytes()

    VIDEO = ["-f", "lavfi", "-i", "testsrc=d=0.5:s=64x64:r=10"]
    AUDIO = ["-f", "lavfi", "-i", "sine=d=0.5:r=22050"]

    def md5(self, data: bytes, suffix: str, selector: str) -> str:
        p = self.tmp / ("h" + mm._suffix(suffix))
        p.write_bytes(data)
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-map", selector,
                              "-c", "copy", "-f", "md5", "-"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def assert_stripped(self, src: bytes, suffix: str, selectors: list[str]):
        before = mm.read_media_tags(src, suffix, workdir=self.work)
        self.assertIn("prompt", before)
        self.assertIn("workflow", before)
        out = mm.strip_metadata(src, suffix, workdir=self.work)
        after = mm.read_media_tags(out, suffix, workdir=self.work)
        self.assertNotIn("prompt", after)
        self.assertNotIn("workflow", after)
        self.assertIn(after.get("encoder"), (None, "Lavf"))  # bitexact: no version
        for sel in selectors:
            self.assertEqual(self.md5(out, suffix, sel), self.md5(src, suffix, sel), sel)
        self.assertEqual(list(self.work.iterdir()), [])  # temp files cleaned
        return out

    def test_mp4_av(self):
        src = self.make("a.mp4", self.VIDEO + self.AUDIO,
                        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"])
        self.assert_stripped(src, ".mp4", ["0:v", "0:a"])
        self.assert_stripped(src, "video/mp4", ["0:v"])

    def test_mov(self):
        src = self.make("a.mov", self.VIDEO, ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
        self.assert_stripped(src, ".mov", ["0:v"])

    def test_webm(self):
        if not _has_encoder("libvpx-vp9"):
            self.skipTest("libvpx-vp9 not available")
        src = self.make("a.webm", self.VIDEO, ["-c:v", "libvpx-vp9", "-b:v", "200k"])
        self.assert_stripped(src, ".webm", ["0:v"])

    def test_mkv(self):
        src = self.make("a.mkv", self.VIDEO + self.AUDIO,
                        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "flac", "-shortest"])
        self.assert_stripped(src, ".mkv", ["0:v", "0:a"])

    def test_flac_and_stream_tags(self):
        src = self.make("a.flac", self.AUDIO, ["-c:a", "flac"])
        self.assert_stripped(src, ".flac", ["0:a"])

    def test_wav(self):
        src = self.make("a.wav", self.AUDIO, ["-c:a", "pcm_s16le"])
        out = mm.strip_metadata(src, ".wav", workdir=self.work)
        tags = mm.read_media_tags(out, ".wav", workdir=self.work)
        self.assertEqual(tags, {})
        self.assertEqual(self.md5(out, ".wav", "0:a"), self.md5(src, ".wav", "0:a"))
        # wav carries INFO tags (comment) in source
        src2 = self.make("b.wav", self.AUDIO, ["-c:a", "pcm_s16le", "-metadata", "comment=hello"], tags=False)
        self.assertEqual(mm.read_media_tags(src2, ".wav", workdir=self.work).get("comment"), "hello")
        self.assertNotIn("comment", mm.read_media_tags(
            mm.strip_metadata(src2, ".wav", workdir=self.work), ".wav", workdir=self.work))

    def test_mp3(self):
        if not _has_encoder("libmp3lame"):
            self.skipTest("libmp3lame not available")
        src = self.make("a.mp3", self.AUDIO, ["-c:a", "libmp3lame"])
        self.assert_stripped(src, ".mp3", ["0:a"])

    def test_ogg(self):
        if not _has_encoder("libvorbis"):
            self.skipTest("libvorbis not available")
        src = self.make("a.ogg", self.AUDIO, ["-c:a", "libvorbis"])
        self.assert_stripped(src, ".ogg", ["0:a"])

    def test_m4a(self):
        src = self.make("a.m4a", self.AUDIO, ["-c:a", "aac"])
        self.assert_stripped(src, ".m4a", ["0:a"])

    def test_failure_raises(self):
        with self.assertRaises(mm.MediaMetadataError):
            mm.strip_metadata(b"not a media file", ".mp4", workdir=self.work)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_missing_ffmpeg_raises(self):
        real = mm.shutil.which
        mm.shutil.which = lambda name: None
        try:
            with self.assertRaises(mm.MediaMetadataError):
                mm.strip_metadata(b"x", ".mp4", workdir=self.work)
        finally:
            mm.shutil.which = real

    def test_pcm_unchanged(self):
        self.assertEqual(mm.strip_metadata(b"\x00\x01", ".pcm"), b"\x00\x01")

    def test_embed_roundtrip(self):
        cases = [
            (".mp4", self.VIDEO, ["-c:v", "libx264", "-pix_fmt", "yuv420p"], "0:v", True),
            (".mov", self.VIDEO, ["-c:v", "libx264", "-pix_fmt", "yuv420p"], "0:v", True),
            (".mkv", self.VIDEO, ["-c:v", "libx264", "-pix_fmt", "yuv420p"], "0:v", True),
            (".flac", self.AUDIO, ["-c:a", "flac"], "0:a", True),
            (".m4a", self.AUDIO, ["-c:a", "aac"], "0:a", True),
        ]
        if _has_encoder("libvpx-vp9"):
            cases.append((".webm", self.VIDEO, ["-c:v", "libvpx-vp9", "-b:v", "200k"], "0:v", True))
        if _has_encoder("libmp3lame"):
            cases.append((".mp3", self.AUDIO, ["-c:a", "libmp3lame"], "0:a", True))
        for suffix, inp, codec, sel, custom in cases:
            with self.subTest(suffix=suffix):
                src = self.make("e" + suffix, inp, codec)
                out = mm.embed_media_tags(src, suffix, {"azizam_provenance": BIG}, workdir=self.work)
                tags = mm.read_media_tags(out, suffix, workdir=self.work)
                self.assertEqual(tags.get("azizam_provenance"), BIG)
                self.assertEqual(tags.get("prompt"), '{"1": {}}')  # preserved
                self.assertEqual(tags.get("workflow"), '{"nodes": []}')
                self.assertEqual(self.md5(out, suffix, sel), self.md5(src, suffix, sel))

    def test_embed_wav_falls_back_to_comment(self):
        src = self.make("e.wav", self.AUDIO, ["-c:a", "pcm_s16le"], tags=False)
        out = mm.embed_media_tags(src, ".wav", {"azizam_provenance": BIG}, workdir=self.work)
        tags = mm.read_media_tags(out, ".wav", workdir=self.work)
        self.assertNotIn("azizam_provenance", tags)  # custom keys do not survive in WAV
        self.assertEqual(tags.get("comment"), "azizam_provenance=" + BIG)
        self.assertEqual(self.md5(out, ".wav", "0:a"), self.md5(src, ".wav", "0:a"))

    def test_embed_image_delegation(self):
        png = _png_with_text()
        out = mm.embed_media_tags(png, "image/png", {"a": "b"})
        self.assertEqual(mm.png_text_chunks(out)["a"], "b")
        buf = io.BytesIO()
        _image().save(buf, "JPEG")
        self.assertEqual(mm.embed_media_tags(buf.getvalue(), ".jpg", {"a": "b"}), buf.getvalue())


def test_missing_ffmpeg_fails_closed_even_when_no_codec_is_installed():
    from unittest.mock import patch

    with (
        patch.object(mm.shutil, "which", return_value=None),
        patch.object(
            mm, "_media_buffers",
            side_effect=AssertionError("must reject before allocating media buffers"),
        ),
    ):
        try:
            mm.strip_metadata(b"tagged private media", ".mp4")
        except mm.MediaMetadataError as exc:
            assert str(exc) == "ffmpeg not found"
        else:
            raise AssertionError("missing ffmpeg must never return unstripped bytes")


if __name__ == "__main__":
    unittest.main()


class AnonymousBufferTests(unittest.TestCase):
    """Media bytes stay in anonymous RAM files even on Python builds without os.memfd_create."""

    def _assert_anonymous(self):
        import os
        from gateway.media_metadata import _media_buffers
        with _media_buffers(b"prompt-bearing bytes") as (src, dst):
            for handle in (src, dst):
                target = os.readlink(f"/proc/self/fd/{handle.fileno()}")
                self.assertTrue(target.startswith("/memfd:"), target)
            self.assertEqual(src.read(), b"prompt-bearing bytes")

    def test_buffers_are_memfd_backed(self):
        self._assert_anonymous()

    def test_buffers_fall_back_to_libc_when_python_omits_memfd_create(self):
        from unittest import mock
        import os
        with mock.patch.object(os, "memfd_create", create=True) as patched:
            del os.memfd_create
            self.assertFalse(hasattr(os, "memfd_create"))
            self._assert_anonymous()
        patched.assert_not_called()
