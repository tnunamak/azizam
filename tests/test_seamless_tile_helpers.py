"""Tests for image tile helper operations."""
import io
import unittest

import proxy


def _png_from_pixels(
    pixels: list[tuple[int, int, int, int]],
    *,
    size: tuple[int, int],
) -> bytes:
    from PIL import Image

    image = Image.new("RGBA", size)
    image.putdata(pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _pixels(content: bytes) -> list[tuple[int, int, int, int]]:
    from PIL import Image

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    return [
        image.getpixel((x, y))
        for y in range(image.height)
        for x in range(image.width)
    ]


class TestSeamlessTileHelpers(unittest.TestCase):
    def test_offset_wraps_pixels(self) -> None:
        raw = _png_from_pixels(
            [
                (255, 0, 0, 255),
                (0, 255, 0, 255),
                (0, 0, 255, 255),
                (255, 255, 0, 255),
            ],
            size=(4, 1),
        )

        shifted = proxy._offset_image_bytes(raw, x_fraction=0.5, y_fraction=0)

        self.assertEqual(
            _pixels(shifted),
            [
                (0, 0, 255, 255),
                (255, 255, 0, 255),
                (255, 0, 0, 255),
                (0, 255, 0, 255),
            ],
        )

    def test_seam_cross_mask_uses_white_as_edit_region(self) -> None:
        mask = proxy._make_seam_cross_mask_png(4, 4, seam_width=2)

        values = [pixel[0] for pixel in _pixels(mask)]
        self.assertEqual(
            values,
            [
                0, 255, 255, 0,
                255, 255, 255, 255,
                255, 255, 255, 255,
                0, 255, 255, 0,
            ],
        )
