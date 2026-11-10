"""The create path hands what it does not move to the old handler at a counted number of places. The count may only fall.

Each `LegacyRoute(...)` in `gateway/frontend_shapes/video_create.py` is one place where the data path gives up and the old
pipeline serves the request (a chain, a source, a reference it cannot read). Moving one of them to the data path lowers the
number; adding one needs a reason in the design, not an edit to this file.
"""

from pathlib import Path

CEILING = 4
SOURCE = Path(__file__).resolve().parent.parent / "gateway" / "frontend_shapes" / "video_create.py"


def test_the_legacy_hand_offs_of_the_create_path_never_rise() -> None:
    count = SOURCE.read_text(encoding="utf-8").count("LegacyRoute(")
    assert count <= CEILING, f"{count} hand-offs to the old handler; the ceiling is {CEILING} and may only fall"
