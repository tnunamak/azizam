"""Keep member image help reachable from the member guide."""
import re
from pathlib import Path


GUIDE = Path(__file__).resolve().parents[1] / "docs/guides/FOR_MEMBERS.md"


def assert_guide_link(guide, target):
    name, _, fragment = target.partition("#")
    path = (guide.parent / name).resolve()
    assert path.parent == guide.parent.resolve() and path.is_file(), target
    if fragment:
        headings = re.findall(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)
        slugs = {
            re.sub(r"[^\w\- ]", "", re.sub(r"\s+#+\s*$", "", heading).lower()).replace(" ", "-")
            for heading in headings
        }
        assert fragment in slugs, f"Missing heading fragment: {target}"


def test_member_guide_links_to_three_image_guides():
    text = GUIDE.read_text(encoding="utf-8")
    _, heading, links = text.partition("\n## Links\n")
    assert heading, "Member guide must have a Links section"
    targets = re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", links)
    for name in ("IMAGE_ADAPTERS.md", "IMAGE_OPTIONS.md", "CHAT_IMAGES.md"):
        matches = [target for target in targets if target.partition("#")[0] in (name, f"./{name}")]
        assert matches, f"Missing relative member guide link: {name}"
        for target in matches:
            assert_guide_link(GUIDE, target)


def test_member_guide_fragment_must_match_destination_heading(tmp_path):
    import pytest

    dest = tmp_path / "example.md"
    dest.write_text("## Your own adapters!\n", encoding="utf-8")
    assert_guide_link(tmp_path / "member.md", "example.md#your-own-adapters")
    with pytest.raises(AssertionError, match="Missing heading fragment"):
        assert_guide_link(tmp_path / "member.md", "example.md#wrong-heading")


def test_member_guide_explains_separate_adapter_upload_setting():
    text = GUIDE.read_text(encoding="utf-8")
    _, heading, tail = text.partition("\n## What your access plan controls\n")
    assert heading, "Member guide must explain access plans"
    section = " ".join(tail.split("\n## ", 1)[0].split())
    sentences = re.split(r"[.!?]\s*", section)
    assert any(
        all(word in sentence.lower() for word in ("upload", "adapter", "owner", "separate setting", "off by default"))
        for sentence in sentences
    ), "Explain the separate owner setting and that adapter uploads are off by default"
