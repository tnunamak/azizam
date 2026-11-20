"""Keep the image adapter guide in sync with the workflow contract."""
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from gateway import adapter_choices


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/guides/IMAGE_ADAPTERS.md"
WORKFLOW = ROOT / "manifests/workflows/krea2-turbo.yml"
LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)")


class ImageAdapterGuideTest(unittest.TestCase):
    def test_guide_tracks_image_adapter_contract(self):
        text = GUIDE.read_text(encoding="utf-8")
        for name in (adapter_choices.LORAS_INPUT, adapter_choices.ENCODER_INPUT):
            self.assertIn(f"`{name}`", text, f"Guide must name adapter input {name!r}")

        table = yaml.safe_load(adapter_choices.TABLE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(table, "Architecture assertions must not be vacuous")
        for architecture, header_values in table.items():
            self.assertIn(f"`{architecture}`", text, f"Guide must name architecture {architecture!r}")
            self.assertTrue(header_values, f"Expected header fields for {architecture!r}")
            for field in header_values:
                self.assertIn(f"`{field}`", text, f"Guide must name architecture header field {field!r}")

        inputs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["inputs"]
        loras = inputs[adapter_choices.LORAS_INPUT]
        max_items = loras["max_items"]
        weight = loras["items"]["properties"]["weight"]
        number = r"(?<![\w.])`?{}`?(?![\w.])"
        adapter_limit = number.format(re.escape(str(max_items))) + r"[ \t]+(?:\w+[ \t]+){0,2}adapters\b"
        reverse_limit = r"\badapters\b[ \t]*(?:[:=][ \t]*|\([ \t]*)" + number.format(re.escape(str(max_items)))
        self.assertNotRegex(text.lower(), adapter_limit + "|" + reverse_limit)
        low = number.format(re.escape(str(weight["minimum"])))
        high = number.format(re.escape(str(weight["maximum"])))
        self.assertNotRegex(text, low + r"\s*(?:to|through|[-–—,])\s*" + high)
        self.assertRegex(" ".join(text.split()), r"(?i)item limit and weight range.{0,80}read from (?:the|that) listing")

        destinations = [a or b for a, b in LINK.findall(text)]
        self.assertTrue(destinations, "The guide must link to the architecture registry")
        for target in destinations:
            address = urlsplit(target)
            if address.scheme or address.netloc or not address.path or address.path.startswith("/"):
                continue
            destination = (GUIDE.parent / unquote(address.path)).resolve()
            self.assertTrue(destination.is_relative_to(ROOT), f"Link leaves the repository: {target}")
            self.assertTrue(destination.is_file(), f"Broken relative link: {target}")
