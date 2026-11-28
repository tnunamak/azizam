"""Thumbnail pixel limits with ordinary images and an artificially low cap."""
from PIL import Image, ImageOps
import pytest

from gateway import thumbnails


def _png(tmp_path, size):
    source = tmp_path / "source.png"
    with Image.new("RGB", size) as image:
        image.save(source, "PNG")
    return source


def test_small_png_gets_thumbnail(tmp_path):
    source = _png(tmp_path, (64, 64))
    target = thumbnails.thumbnail_path(source)

    assert thumbnails.thumbnail_for(source) == target
    assert target.is_file()


@pytest.mark.parametrize(("size", "accepted"), [
    ((8, 8), True),
    ((10, 10), True),
    ((16, 16), False),
])
def test_thumbnail_pixel_cap_includes_boundary(tmp_path, monkeypatch, size, accepted):
    monkeypatch.setattr(thumbnails, "_MAX_SOURCE_PIXELS", 100)
    source = _png(tmp_path, size)
    target = thumbnails.thumbnail_path(source)

    if accepted:
        assert thumbnails.thumbnail_for(source) == target
        assert target.is_file()
    else:
        assert thumbnails.thumbnail_for(source) is None
        assert not target.exists()


def _png_header_claiming(tmp_path, width, height):
    """A tiny PNG whose IHDR claims width x height; the image data is one empty chunk."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    source = tmp_path / "claims.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                       + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))
    return source


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_header_claiming_too_many_pixels_is_never_decoded(tmp_path, monkeypatch):
    # 12000 x 12000 = 144 MP: above our cap, below the size where Pillow itself refuses to open.
    source = _png_header_claiming(tmp_path, 12000, 12000)
    with Image.open(source) as opened:
        assert opened.size == (12000, 12000)
    decoded = []
    monkeypatch.setattr(ImageOps, "exif_transpose", lambda image: decoded.append(image) or image)

    assert thumbnails.thumbnail_for(source) is None
    assert decoded == []
    assert not thumbnails.thumbnail_path(source).exists()
