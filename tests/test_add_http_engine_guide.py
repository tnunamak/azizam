"""Keep the remote-engine owner instructions readable and safe."""

from pathlib import Path
import re


GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guides" / "ADD_HTTP_ENGINE_AS_DATA.md"
HEADING = "This computer, or another computer on your network"
TECHNICAL_WORDS = re.compile(r"\b(?:buffered|synchronous|WebSocket)\b", re.IGNORECASE)


def test_remote_engine_owner_instructions():
    sections = re.split(r"(?m)^## ", GUIDE.read_text(encoding="utf-8"))
    section = next(part for part in sections if part.splitlines()[0] == HEADING)
    assert "\n### Technical note\n" in section
    owner, _ = section.split("\n### Technical note\n", 1)
    for label in ("Where does it run?", "On another computer on my network", "I trust this network"):
        assert label in owner

    prose = []
    in_fence = False
    for line in section.splitlines()[1:]:
        text = line.strip()
        if text.startswith(("\x60\x60\x60", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or text.startswith(("#", "|")):
            continue
        text = re.sub(r"^\d+\.\s+", "", text)
        text = re.sub(r"\x60+[^\x60]*\x60+", "", text)
        prose.append(text)
        for sentence in re.split(r"[.!?]\s+", text):
            assert len(sentence.split()) <= 25, sentence

    in_fence = False
    for line in owner.splitlines()[1:]:
        text = line.strip()
        if text.startswith(("\x60\x60\x60", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            assert not TECHNICAL_WORDS.search(text), line

    owner_text = " ".join(owner.lower().replace("\x60", "").split())
    assert re.search(
        r"if the engine needs a key,\s*or you cannot confirm the network,\s*use https",
        owner_text,
    )
