"""Keep member image help reachable from the member guide."""
import re
from pathlib import Path


GUIDE = Path(__file__).resolve().parents[1] / "docs/guides/FOR_MEMBERS.md"


def test_member_guide_links_to_three_image_guides():
    text = GUIDE.read_text(encoding="utf-8")
    _, heading, links = text.partition("\n## Links\n")
    assert heading, "Member guide must have a Links section"
    targets = {target.split("#", 1)[0] for target in re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", links)}
    for name in ("IMAGE_ADAPTERS.md", "IMAGE_OPTIONS.md", "CHAT_IMAGES.md"):
        target = next((target for target in (name, f"./{name}") if target in targets), None)
        assert target is not None, f"Missing relative member guide link: {name}"
        path = (GUIDE.parent / target).resolve()
        assert path.parent == GUIDE.parent.resolve() and path.is_file(), target


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
