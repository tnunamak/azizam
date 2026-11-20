"""Keep the chat image quickstart aligned with the gateway contract."""

import ast
import inspect
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from gateway import chat_asset_refs


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guides" / "CHAT_IMAGES.md"


def test_chat_images_guide_matches_gateway_contract():
    assert GUIDE.is_file(), "The chat images guide is missing"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "POST /v1/assets" in guide
    examples = [
        json.loads(block)
        for block in re.findall(r"^\x60{3}json\s*\n(.*?)^\x60{3}\s*$", guide, re.M | re.S)
    ]
    ids = [
        part["image_url"]["url"]
        for example in examples
        for message in example.get("messages", [])
        for part in message.get("content", [])
        if part.get("type") == "image_url"
    ]
    assert any(chat_asset_refs.ASSET_ID.fullmatch(value) for value in ids), (
        "A chat JSON example must put a valid asset ID in image_url.url"
    )

    tree = ast.parse(inspect.getsource(chat_asset_refs))
    codes = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "RefError":
            assert call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str), (
                "Every RefError should have a literal code that can be checked against the guide"
            )
            codes.add(call.args[0].value)
    assert codes, "No RefError codes found in the gateway module"
    missing = sorted(code for code in codes if f"\x60{code}\x60" not in guide)
    assert not missing, f"Undocumented chat image refusal codes: {missing}"

    assert re.search(
        r"an unknown id.{0,100}an id the caller may not read.{0,100}the same refusal",
        " ".join(guide.lower().split()),
    ), "The guide must explain that unknown and unreadable IDs get the same refusal"

    for href in re.findall(r"\[[^\]]+\]\(\s*<?([^\s)>]+)>?(?:\s+[^)]*)?\)", guide):
        url = urlsplit(href)
        if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
            continue
        target = (GUIDE.parent / unquote(url.path)).resolve()
        assert target.is_relative_to(ROOT) and target.is_file(), f"Broken relative guide link: {href}"
