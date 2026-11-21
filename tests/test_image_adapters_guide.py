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


def test_caller_guide_links_to_authenticated_image_request():
    text = GUIDE.read_text(encoding="utf-8")
    callers = text.split("## Operators:", 1)[0]
    assert "IMAGE_OPTIONS.md#list-images-and-send-a-request" in callers


class OperatorImageAdapterGuideTest(unittest.TestCase):
    def test_operator_section_says_restart_after_registry_row(self):
        text = GUIDE.read_text(encoding="utf-8")
        _, heading, tail = text.partition("\n## Operators: make an adapter available\n")
        self.assertTrue(heading, "The guide must include operator instructions")
        section = " ".join(tail.split("\n## ", 1)[0].split())
        self.assertRegex(section, r"(?i)\brow (?:to|in) the registry[^.!?]*\brestart the gateway\b")
        self.assertRegex(section, r"(?is)\brestart the gateway\b.*?\brefresh the workflow listing\b")


    def test_operator_section_explains_adapter_refresh_and_same_name_replacement(self):
        text = " ".join(GUIDE.read_text(encoding="utf-8").split())
        self.assertIn("within about five minutes", text)
        self.assertIn("the list may stay as it was", text)
        self.assertRegex(text, r"replaced under the same name[^.]*up to six hours")

class MemberImageAdapterGuideTest(unittest.TestCase):
    def section(self):
        guide = GUIDE.read_text(encoding="utf-8")
        _, heading, tail = guide.partition("\n## Your own adapters\n")
        self.assertTrue(heading, "The guide must include a Your own adapters section")
        return " ".join(tail.split("\n## ", 1)[0].split())

    def test_member_section_exists(self):
        self.assertTrue(self.section(), "The member section must explain how to use uploads")

    def test_member_section_names_library_upload_button(self):
        self.assertIn("Upload your own style adapter", self.section())

    def test_member_section_names_supported_and_refused_formats(self):
        section = self.section()
        self.assertRegex(section, r"(?i)only.*safetensors")
        self.assertRegex(section, r"`\.pt`.*`\.ckpt`.*`\.bin`.*\brefused\b")

    def test_member_section_explains_upload_privacy(self):
        self.assertRegex(self.section(), r"(?i)uploads? (?:is|are) private")

    def test_member_section_explains_header_based_workflow_matching(self):
        self.assertRegex(self.section(), r"(?i)header[^.]*base model")
        self.assertRegex(self.section(), r"(?i)only on workflows (?:that use|for) that base model")

    def test_member_section_explains_storage_total(self):
        section = self.section()
        self.assertRegex(section, r"(?i)owner sets[^.]*per-person total")
        self.assertRegex(section, r"(?i)default total is 10 GiB")
        self.assertRegex(section, r"(?i)refused[^.]*reply tells you[^.]*used[^.]*total")
        self.assertRegex(section, r"(?i)same file[^.]*does not count twice")
        self.assertRegex(section, r"(?i)deleting[^.]*frees room")

    def test_member_section_distinguishes_label_from_upload_id(self):
        section = self.section()
        self.assertRegex(section, r"(?i)upload reply[^.]*`id`[^.]*`ast_`")
        self.assertRegex(section, r"(?i)workflow.s listing[^.]*label")
        self.assertRegex(section, r'(?i)my style[^.]*`ast_[^\x60]+`[^.]*loras[^.]*"name"\s*:\s*"ast_')

    def test_member_section_explains_unreadable_file_refusal(self):
        self.assertIn("If the gateway cannot read the file's built-in information, it refuses the upload.", " ".join(self.section().split()))

    def test_member_section_explains_unlabelled_upload_listing(self):
        self.assertRegex(self.section(), r"(?i)an upload without a label[^.]*is listed by its ID\b")

    def test_member_section_explains_local_only_and_first_copy(self):
        section = self.section()
        self.assertRegex(section.split("1.", 1)[0], r"(?i)only on workflows[^.]*gateway.s own machine")
        self.assertRegex(section, r"(?i)not on remote workflows")
        self.assertRegex(section, r"(?i)first use[^.]*copies the file")
        self.assertRegex(section, r"(?i)deleting[^.]*removes it from the choice list")
