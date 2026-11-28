"""Keep the operator guide's GPU refusal explanation easy to find and read."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/guides/OPERATOR.md"


def test_operator_guide_explains_busy_graphics_card():
    text = GUIDE.read_text(encoding="utf-8")
    _, heading, tail = text.partition("\n### When the graphics card is busy\n")
    assert heading, "The troubleshooting guide must explain a busy graphics card"
    section = tail.split("\n### ", 1)[0].split("\n## ", 1)[0]
    for phrase in ("30 seconds", "idle models", "queue is empty"):
        assert phrase in section
    assert len(section.split()) < 170
