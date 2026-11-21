"""Keep the chat image quickstart aligned with the gateway contract."""

import ast
import inspect
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from gateway import chat_asset_refs


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guides" / "CHAT_IMAGES.md"


CHAT_REMEDIES = {
    "invalid_image_url": "This value is not a valid stored image ID. Copy the entire ID from the upload reply.",
    "asset_missing": "Check the ID and use a key allowed to read the asset, or upload the image again under the key you will use for chat.",
    "invalid_image_format": "Export it as a supported image, such as PNG, then upload the new file and use its new ID.",
}

UPLOAD_REMEDIES = {
    "missing_file": "Send the image in the multipart `file` field.",
    "invalid_image": "Choose a non-empty image file you can open, then upload it again.",
    "invalid_image_format": "Save the image in a supported format, such as PNG, then upload it again.",
    "file_too_large": "Make the image smaller, then upload it again.",
}


def error_rows(guide, heading):
    section = re.search(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", guide, re.M | re.S)
    assert section, f"Missing error section: {heading}"
    rows = re.findall(r"^\|\s*\x60([^\x60]+)\x60\s*\|\s*([^|]+)\|\s*$", section[1], re.M)
    assert len(rows) == len(dict(rows)), f"Duplicate error codes in: {heading}"
    return section[1], dict(rows)


def assert_chat_example(guide):
    matches = re.findall(
        r"^3\. Send \*\*([A-Z]+) (\S+)\*\*[^\n]*\n\s*^\x60{3}json[ \t]*\n(.*?)^\x60{3}[ \t]*$",
        guide,
        re.M | re.S,
    )
    assert len(matches) == 1, "Expected one numbered chat request and its JSON example"
    method, path, block = matches[0]
    assert (method, path) == ("POST", "/v1/chat/completions"), "Incorrect chat example method or path"
    request = json.loads(block)
    assert isinstance(request, dict), "The chat example body must be an object"
    model = request.get("model")
    assert isinstance(model, str) and model.strip(), "The chat example needs a model"
    messages = request.get("messages")
    assert isinstance(messages, list), "The chat example needs messages"
    users = [message for message in messages if isinstance(message, dict) and message.get("role") == "user"]
    assert users, "The chat example needs a user message"
    parts = [
        part
        for message in users
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict)
    ]
    assert any(
        part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip()
        for part in parts
    ), "The chat example needs a text content part"
    assert any(
        part.get("type") == "image_url"
        and isinstance(part.get("image_url"), dict)
        and isinstance(part["image_url"].get("url"), str)
        and chat_asset_refs.ASSET_ID.fullmatch(part["image_url"]["url"])
        for part in parts
    ), "The chat example must put a valid asset ID in image_url.url"


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        (lambda guide: re.sub(r'^\s*"model": [^\n]+\n', "", guide, count=1, flags=re.M), "needs a model"),
        (lambda guide: guide.replace("/v1/chat/completions", "/v1/chat/other", 1), "method or path"),
    ],
)
def test_chat_example_rejects_bad_scratch_text(change, failure):
    guide = GUIDE.read_text(encoding="utf-8")
    changed = change(guide)
    assert changed != guide
    with pytest.raises(AssertionError, match=failure):
        assert_chat_example(changed)


def test_chat_images_guide_matches_gateway_contract():
    assert GUIDE.is_file(), "The chat images guide is missing"
    guide = GUIDE.read_text(encoding="utf-8")
    assert "POST /v1/assets" in guide
    assert_chat_example(guide)

    tree = ast.parse(inspect.getsource(chat_asset_refs))
    codes = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "RefError":
            assert call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str), (
                "Every RefError should have a literal code that can be checked against the guide"
            )
            codes.add(call.args[0].value)
    assert codes, "No RefError codes found in the gateway module"
    _, upload = error_rows(guide, "If the upload is refused")
    chat_text, chat = error_rows(guide, "If the chat request is refused")
    assert set(upload) == UPLOAD_REMEDIES.keys()
    assert "file_too_large" not in chat, "Upload size errors do not come from chat"
    assert set(chat) == codes
    assert codes == CHAT_REMEDIES.keys(), f"Chat refusal remedies need review: {codes}"
    for rows, remedies in ((upload, UPLOAD_REMEDIES), (chat, CHAT_REMEDIES)):
        for code, remedy in remedies.items():
            assert remedy in rows[code], f"Missing or misplaced remedy for {code}"
    assert "A refused image reference stops the whole chat request." in chat_text
    assert "For a chat error, look at `error.code` and `error.param`. The parameter identifies the image part to fix." in chat_text

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
