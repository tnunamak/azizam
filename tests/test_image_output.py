"""Tests for output_format/output_compression/quality image-shape helpers.

Pure unit tests — no FastAPI, no ComfyUI, no network. Pillow is used to
build/inspect real PNG/JPEG/WEBP bytes so the encode path is proven against
real image data, not just mocked.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.image_output import (  # noqa: E402
    background_format_conflict,
    encode_image_bytes,
    parse_output_compression,
    parse_output_format,
    parse_quality,
    quality_steps_from_manifest,
)


def _png_bytes(mode: str = "RGBA", size: tuple[int, int] = (4, 4)) -> bytes:
    from PIL import Image

    color = (10, 20, 30, 255) if mode == "RGBA" else (10, 20, 30)
    image = Image.new(mode, size, color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class TestParseOutputFormat(unittest.TestCase):
    def test_default_is_png(self) -> None:
        self.assertEqual(parse_output_format(None), ("png", None))
        self.assertEqual(parse_output_format(""), ("png", None))

    def test_accepts_known_formats_case_insensitively(self) -> None:
        self.assertEqual(parse_output_format("PNG"), ("png", None))
        self.assertEqual(parse_output_format("Jpeg"), ("jpeg", None))
        self.assertEqual(parse_output_format("webp"), ("webp", None))

    def test_jpg_alias_normalizes_to_jpeg(self) -> None:
        self.assertEqual(parse_output_format("jpg"), ("jpeg", None))

    def test_unknown_format_is_rejected(self) -> None:
        fmt, err = parse_output_format("bmp")
        self.assertEqual(fmt, "png")
        self.assertIsNotNone(err)

    def test_non_string_is_rejected(self) -> None:
        fmt, err = parse_output_format(123)
        self.assertEqual(fmt, "png")
        self.assertIsNotNone(err)


class TestParseOutputCompression(unittest.TestCase):
    def test_default_for_png_is_none(self) -> None:
        self.assertEqual(parse_output_compression(None, "png"), (None, None))

    def test_default_for_jpeg_is_100(self) -> None:
        self.assertEqual(parse_output_compression(None, "jpeg"), (100, None))

    def test_accepts_integer_in_range(self) -> None:
        self.assertEqual(parse_output_compression(50, "webp"), (50, None))

    def test_value_for_png_is_accepted_but_has_no_effect(self) -> None:
        # Not an error -- OpenAI documents png as always-lossless -- but the
        # returned compression is None so callers don't silently apply it.
        compression, err = parse_output_compression(80, "png")
        self.assertIsNone(compression)
        self.assertIsNone(err)

    def test_out_of_range_is_rejected(self) -> None:
        _, err = parse_output_compression(101, "jpeg")
        self.assertIsNotNone(err)
        _, err = parse_output_compression(-1, "jpeg")
        self.assertIsNotNone(err)

    def test_non_integer_is_rejected(self) -> None:
        _, err = parse_output_compression("high", "jpeg")
        self.assertIsNotNone(err)

    def test_bool_is_rejected(self) -> None:
        _, err = parse_output_compression(True, "jpeg")
        self.assertIsNotNone(err)

    def test_fractional_float_above_is_rejected(self) -> None:
        # A JSON body delivers numbers as Python floats. int(100.9) == 100
        # with no error -- that's silent truncation, not validation.
        compression, err = parse_output_compression(100.9, "jpeg")
        self.assertIsNone(compression)
        self.assertIsNotNone(err)

    def test_fractional_float_below_is_rejected(self) -> None:
        compression, err = parse_output_compression(-0.9, "jpeg")
        self.assertIsNone(compression)
        self.assertIsNotNone(err)

    def test_whole_number_float_is_accepted(self) -> None:
        # 100.0 is exactly 100 -- not a truncation, nothing to reject.
        self.assertEqual(parse_output_compression(100.0, "jpeg"), (100, None))

    def test_fractional_string_is_rejected(self) -> None:
        # Multipart form fields deliver strings; int("100.9") already raises
        # on its own, but pin the behavior explicitly.
        _, err = parse_output_compression("100.9", "jpeg")
        self.assertIsNotNone(err)


class TestParseQuality(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        self.assertEqual(parse_quality(None), ("auto", None))

    def test_accepts_known_values(self) -> None:
        for value in ("low", "medium", "high", "auto"):
            self.assertEqual(parse_quality(value), (value, None))

    def test_unknown_value_is_rejected(self) -> None:
        quality, err = parse_quality("ultra")
        self.assertEqual(quality, "auto")
        self.assertIsNotNone(err)

    def test_dalle_legacy_standard_maps_to_medium(self) -> None:
        self.assertEqual(parse_quality("standard"), ("medium", None))

    def test_dalle_legacy_hd_maps_to_high(self) -> None:
        self.assertEqual(parse_quality("hd"), ("high", None))

    def test_dalle_legacy_values_are_case_insensitive(self) -> None:
        self.assertEqual(parse_quality("STANDARD"), ("medium", None))
        self.assertEqual(parse_quality("HD"), ("high", None))


class TestBackgroundFormatConflict(unittest.TestCase):
    def test_transparent_jpeg_conflicts(self) -> None:
        self.assertIsNotNone(background_format_conflict("transparent", "jpeg"))

    def test_transparent_png_is_fine(self) -> None:
        self.assertIsNone(background_format_conflict("transparent", "png"))

    def test_transparent_webp_is_fine(self) -> None:
        self.assertIsNone(background_format_conflict("transparent", "webp"))

    def test_opaque_jpeg_is_fine(self) -> None:
        self.assertIsNone(background_format_conflict("opaque", "jpeg"))


class TestQualityStepsFromManifest(unittest.TestCase):
    def test_auto_never_looks_up_manifest(self) -> None:
        manifest = {"quality_steps": {"low": 4}}
        self.assertIsNone(quality_steps_from_manifest(manifest, "auto"))

    def test_missing_manifest_key_returns_none(self) -> None:
        self.assertIsNone(quality_steps_from_manifest({"id": "x"}, "high"))

    def test_none_manifest_returns_none(self) -> None:
        self.assertIsNone(quality_steps_from_manifest(None, "high"))

    def test_declared_mapping_resolves(self) -> None:
        manifest = {"quality_steps": {"low": 4, "medium": 8, "high": 16}}
        self.assertEqual(quality_steps_from_manifest(manifest, "high"), 16)

    def test_value_not_declared_for_this_quality_returns_none(self) -> None:
        manifest = {"quality_steps": {"low": 4}}
        self.assertIsNone(quality_steps_from_manifest(manifest, "high"))

    def test_non_integer_step_value_is_ignored(self) -> None:
        manifest = {"quality_steps": {"high": "lots"}}
        self.assertIsNone(quality_steps_from_manifest(manifest, "high"))


class TestEncodeImageBytes(unittest.TestCase):
    def test_png_passes_through_unchanged(self) -> None:
        raw = _png_bytes()
        encoded, mime = encode_image_bytes(raw, "png", None)
        self.assertEqual(encoded, raw)
        self.assertEqual(mime, "image/png")

    def test_jpeg_encodes_and_drops_alpha(self) -> None:
        from PIL import Image

        raw = _png_bytes("RGBA")
        encoded, mime = encode_image_bytes(raw, "jpeg", 80)
        self.assertEqual(mime, "image/jpeg")
        result = Image.open(io.BytesIO(encoded))
        self.assertEqual(result.format, "JPEG")
        self.assertEqual(result.mode, "RGB")

    def test_webp_encodes_and_keeps_alpha(self) -> None:
        from PIL import Image

        source = Image.new("RGBA", (2, 2))
        source.putdata([
            (10, 20, 30, 0),
            (40, 50, 60, 64),
            (70, 80, 90, 128),
            (100, 110, 120, 255),
        ])
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        raw = buffer.getvalue()
        for quality in (80, 100):
            with self.subTest(quality=quality):
                encoded, mime = encode_image_bytes(raw, "webp", quality)
                self.assertEqual(mime, "image/webp")
                with Image.open(io.BytesIO(encoded)) as decoded:
                    self.assertEqual(decoded.format, "WEBP")
                    self.assertEqual(decoded.size, (2, 2))
                    self.assertEqual(decoded.mode, "RGBA")
                    self.assertEqual(
                        list(decoded.getchannel("A").tobytes()),
                        [0, 64, 128, 255],
                    )

    def test_unsupported_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            encode_image_bytes(_png_bytes(), "bmp", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
