"""OpenAI Python SDK compatibility check for gateway image metadata.

This is a local deserialization test, not a live probe. It is skipped when the
optional ``openai`` package is not installed.
"""

from __future__ import annotations

import importlib
import unittest


def _images_response_type():
    candidates = (
        ("openai.types.images_response", "ImagesResponse"),
        ("openai.types.image_generate_response", "ImagesResponse"),
        ("openai.types", "ImagesResponse"),
    )
    for module_name, attr_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        cls = getattr(module, attr_name, None)
        if cls is not None:
            return cls
    return None


class TestOpenAIImageExtraFields(unittest.TestCase):
    def test_gateway_asset_fields_do_not_break_image_response_parsing(self) -> None:
        response_type = _images_response_type()
        if response_type is None:
            self.skipTest("OpenAI Python SDK ImagesResponse type not available")

        payload = {
            "created": 1,
            "data": [
                {
                    "b64_json": "aGVsbG8=",
                    "asset_id": "img_" + ("0" * 64),
                    "asset_url": "http://127.0.0.1:5000/v1/assets/img_x/content",
                    "media_type": "image/png",
                }
            ],
        }
        if hasattr(response_type, "model_validate"):
            parsed = response_type.model_validate(payload)
        else:
            parsed = response_type.parse_obj(payload)

        self.assertEqual(parsed.data[0].b64_json, "aGVsbG8=")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
