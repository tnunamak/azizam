import re
import sys
from html.parser import HTMLParser

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import firehose_render
from firehose_render import render_html


class _Document(HTMLParser):
    """Small semantic HTML oracle; serialized HTML remains the privacy boundary."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": [],
                "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"].append(data)

    def find(self, tag):
        return [node for node in self.nodes if node["tag"] == tag]


def _text(node):
    return "".join(node["text"]).strip()


def _assert_shell(html, *, actor=False):
    document = _Document(html)
    main, = document.find("main")
    footer, = document.find("footer")
    assert main["attrs"]["class"].split() == ["shell-main", "status-firehose"]
    assert footer["parent"] is main, "Status footer must belong to the main landmark"
    navigation = [
        (_text(node), node["attrs"].get("href"), node["attrs"].get("aria-current"))
        for node in document.find("a")
        if _text(node) in {"Home", "Status", "Sign out"}
    ]
    expected = [("Home", "/", None), ("Status", "/status/", "page")]
    if actor:
        expected.append(("Sign out", "/auth/logout-confirm", None))
    assert sorted(navigation) == sorted(expected)
    assert all(node["attrs"].get("href") != "/admin" for node in document.find("a"))


def _payload() -> dict:
    return {
        "banner": "ok",
        "generated_at": 1_763_000_000.0,
        "commit_hash": "abc123",
    }


def test_render_uses_shared_shell_with_active_status_nav_for_actor() -> None:
    html = render_html(
        _payload(),
        host="gateway.local",
        actor_label="operator@example.com",
        logout_url="/auth/logout-confirm",
    )

    _assert_shell(html, actor=True)
    assert 'class="shell-header"' in html
    assert 'class="shell-main status-firehose"' in html
    assert 'aria-label="Azizam home"' in html
    assert "shell-brand-product" in html
    assert ">Azizam</span>" in html
    assert 'href="/status/" aria-current="page">Status</a>' in html
    assert 'href="/admin"' not in html
    assert 'href="/">Home</a>' in html
    assert "operator@example.com" in html
    assert 'href="/auth/logout-confirm">Sign out</a>' in html
    assert "gateway.local" in html
    assert "Live activity" not in html
    assert "No jobs in flight" not in html
    assert html.index('<main class="shell-main status-firehose">') < html.index("<footer")
    assert html.index("<footer") < html.index("</main></body></html>")


def test_render_default_status_nav_keeps_status_page_chrome() -> None:
    html = render_html(_payload())

    _assert_shell(html)
    assert 'class="shell-header"' in html
    assert 'href="/status/" aria-current="page">Status</a>' in html
    assert 'href="/admin"' not in html
    assert 'href="/">Home</a>' in html
    assert "vivid.fish" not in html
    assert "Sign out" not in html
    assert html.endswith("</main></body></html>")


def test_status_css_uses_shell_tokens_without_legacy_dive_palette_or_stripes() -> None:
    html = render_html(_payload(), host="gateway.local")

    assert "--dive-" not in html
    assert "var(--panel)" in html
    assert "var(--line)" in html
    # border-left is now legitimately used by vf-copy-field-button (separator);
    # assert that old per-row stripe accent borders are gone instead.
    assert "border-left: 3px solid" not in html
    assert "overflow-x:auto" in html
    assert "overflow-x:hidden" in html


def test_empty_status_sections_are_hidden_until_they_have_signal() -> None:
    html = render_html(_payload(), host="gateway.local")

    document = _Document(html)
    headings = {_text(node) for tag in ("h1", "h2", "h3") for node in document.find(tag)}
    assert headings.isdisjoint({
        "Live activity", "Health", "Resources", "Capabilities", "Recent output",
        "Identity", "Diagnostics", "Errors during status build",
    })
    assert any(node["attrs"].get("href") == "/v1/status"
               and _text(node) == "/v1/status (raw JSON)" for node in document.find("a"))

    assert "Live activity" not in html
    # "Health" now appears in vendored brand CSS comments (SystemHealthBand,
    # ServiceHealthRow) — check rendered section headings are absent instead.
    assert ">Health<" not in html
    assert "Resources" not in html
    assert "Capabilities" not in html
    assert "Recent output" not in html
    assert "Identity" not in html
    assert "Diagnostics" not in html
    assert "Errors during status build" not in html
    assert "/v1/status (raw JSON)" in html


def test_quick_test_snippets_use_ai_azizam_host() -> None:
    html = firehose_render._render_quick_test()

    snippets = [_text(node) for node in _Document(html).find("pre")]
    # Six snippets cover five endpoints: both video workflows must use this host.
    assert [re.findall(r"https://[^\s]+", snippet) for snippet in snippets] == [
        ["https://gateway.example/openai/v1/chat/completions"],
        ["https://gateway.example/openai/v1/images/generations"],
        ["https://gateway.example/openai/v1/audio/speech"],
        ["https://gateway.example/openai/v1/audio/transcriptions"],
        ["https://gateway.example/openai/v1/videos"],
        ["https://gateway.example/openai/v1/videos"],
    ]
    assert "vivid.fish" not in html


def test_module_copy_does_not_say_root_serves_status() -> None:
    assert "/firehose" not in (firehose_render.__doc__ or "")
    assert "served at `/status/raw`" in (firehose_render.__doc__ or "")
    assert "gateway status dashboard" in (firehose_render.__doc__ or "")


def test_live_video_jobs_renderer_has_no_prompt_column() -> None:
    secret = "SECRET renderer prompt should not appear"
    html = firehose_render._render_video_jobs([
        {
            "status": "running",
            "percent": 50,
            "prompt_truncated": secret,
            "prompt_id": "pid-1",
        }
    ])

    cells = [_text(node) for node in _Document(html).find("td")]
    assert cells[0:2] == ["running", "50%"]
    assert cells[11] == "pid-1"
    assert len(cells) == 13
    assert "Prompt" not in html
    assert "prompt_truncated" not in html
    assert secret not in html


@pytest.mark.parametrize("renderer,filename", [
    (firehose_render._render_recent_videos, "video.mp4"),
    (firehose_render._render_recent_images, "image.png"),
], ids=["recent_video", "recent_image"])
def test_recent_media_renderers_have_no_prompt_column(renderer, filename) -> None:
    secret = f"SECRET {filename} prompt should not appear"
    html = renderer([{"filename": filename, "prompt": secret}])

    document = _Document(html)
    assert _text(document.find("td")[0]) == filename
    assert _text(document.find("th")[0]) == "Filename"
    assert "Prompt" not in html
    assert secret not in html


def test_recent_events_renderer_drops_prompt_shaped_extras() -> None:
    secret = "SECRET event prompt should not appear"
    html = firehose_render._render_recent_events([
        {
            "ts": 1,
            "type": "image_route_receipt",
            "summary": "image request",
            "prompt_preview": secret,
        }
    ])

    cells = [_text(node) for node in _Document(html).find("td")]
    assert cells[1:] == ["image_route_receipt", "image request", ""]
    assert len(cells) == 4
    assert "prompt_preview" not in html
    assert secret not in html
