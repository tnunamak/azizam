"""The create path hands what it does not move to the old handler at a counted number of places. The count is pinned and may only fall.

Each `LegacyRoute(...)` in `gateway/frontend_shapes/video_create.py` is one place where the data path gives up and the old
pipeline serves the request (a chain, a source, a reference it cannot read). Moving one of them to the data path lowers COUNT
in the same commit; a test that equals the count means a removed hand-off cannot be added back unnoticed.
"""

from pathlib import Path

COUNT = 4
SOURCE = Path(__file__).resolve().parent.parent / "gateway" / "frontend_shapes" / "video_create.py"


def test_the_legacy_hand_offs_of_the_create_path_are_pinned_at_the_counted_number() -> None:
    count = SOURCE.read_text(encoding="utf-8").count("LegacyRoute(")
    assert count == COUNT, f"{count} hand-offs to the old handler; the pinned count is {COUNT}: lower it when one moves, never raise it"
