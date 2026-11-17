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
    assert "vivid" ".fish" not in html
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
    assert "vivid" ".fish" not in html


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


def test_voice_sources_preserve_payload_order_and_neutral_headings() -> None:
    html = firehose_render._render_voices({
        "sources": {"zeta_catalog": 7, "alpha_catalog": 3},
        "openai_alias_map": {"second": "voice-b", "first": "voice-a"},
        "native_voices": ["voice-b", "voice-a"],
    })

    document = _Document(html)
    cells = [_text(node) for node in document.find("td")]
    assert "7 zeta_catalog / 3 alpha_catalog" in cells
    assert [_text(node) for node in document.find("summary")] == [
        "OpenAI alias mapping (2)", "Native voices (2)",
    ]
    assert [_text(node) for node in document.find("th")][-3:] == [
        "OpenAI alias", "→", "Native voice",
    ]
    assert cells[-6:] == ["second", "→", "voice-b", "first", "→", "voice-a"]
    assert "voxtral" not in html.lower()


def test_voice_keys_and_values_are_escaped_and_aliases_capped() -> None:
    aliases = {f"<alias-{index}>": f"<voice-{index}>" for index in range(21)}
    html = firehose_render._render_voices({
        "sources": {"<source>&": "<count>&"},
        "openai_alias_map": aliases,
        "native_voices": ["<native>&"],
    })

    assert "&lt;count&gt;&amp; &lt;source&gt;&amp;" in html
    assert "&lt;native&gt;&amp;" in html
    document = _Document(html)
    alias_cells = [_text(node) for node in document.find("td")][5:]
    assert alias_cells == [
        value for index in range(20)
        for value in (f"<alias-{index}>", "→", f"<voice-{index}>")
    ]
    assert "&lt;alias-19&gt;" in html and "&lt;voice-19&gt;" in html
    assert "alias-20" not in html and "voice-20" not in html
    assert not any(node["tag"].startswith(("source", "count", "alias-", "voice-", "native"))
                   for node in document.nodes)


def test_disk_hotspot_heading_does_not_assume_directory_containment() -> None:
    html = firehose_render._render_disk({})
    headings = [_text(node) for node in _Document(html).find("h3")]
    assert headings == ["Partitions", "Hot-spot directories (Directory sizes may overlap)"]
    assert "comfy_output_root" not in html
    assert "comfy_output_video" not in html


def test_configured_device_budget_bytes_match_legacy_capture(monkeypatch):
    # Static capture of the previous table serialization; no renderer builds the oracle.
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS", {
        **firehose_render._GRAPH_WORDS, "label": "Graph", "gpu_budget_key": "graph_gb",
    })
    budgets = {
        "gpu0": dict(total_gb=1, desktop_gb=2, tts_gb=3, graph_gb=4, headroom_gb=5),
        "gpu1": dict(total_gb=6, llm_gb=7, headroom_gb=8),
        "images": dict(max_batch_size=9, vram_reserve_bytes=10, signed_url_ttl=11, cache_dir="/cache"),
    }
    expected = (
        '<table class="kv">'
        '<tr><th>GPU0 total GB</th><td>1</td></tr>'
        '<tr><th>GPU0 desktop GB</th><td>2</td></tr>'
        '<tr><th>GPU0 TTS GB</th><td>3</td></tr>'
        '<tr><th>GPU0 Graph GB</th><td>4</td></tr>'
        '<tr><th>GPU0 headroom GB</th><td>5</td></tr>'
        '<tr><th>GPU1 total GB</th><td>6</td></tr>'
        '<tr><th>GPU1 LLM GB</th><td>7</td></tr>'
        '<tr><th>GPU1 headroom GB</th><td>8</td></tr>'
        '<tr><th>Image max_batch_size</th><td>9</td></tr>'
        '<tr><th>Image VRAM reserve</th><td>10 B</td></tr>'
        '<tr><th>Image signed_url_ttl (s)</th><td>11</td></tr>'
        '<tr><th>Image cache dir</th><td>/cache</td></tr></table>'
    )
    assert firehose_render._render_budgets(budgets) == expected
    assert expected in render_html({**_payload(), "budgets": budgets})


def test_configured_runtime_bytes_match_legacy_capture(monkeypatch):
    monkeypatch.setattr(firehose_render, "_GRAPH_WORDS", {
        **firehose_render._GRAPH_WORDS, "label": "Graph", "short_label": "G", "version_key": "version",
    })
    payload = dict(version="v1", python_version="3", pytorch_version="4",
                   ram_total_bytes=5, ram_free_bytes=6, argv="args",
                   device=dict(name="card", vram_total=7, vram_free=8,
                               torch_vram_total=9, torch_vram_free=10))
    expected = (
        '<table class="kv">'
        '<tr><th>Graph version</th><td>v1</td></tr>'
        '<tr><th>Python</th><td>3</td></tr>'
        '<tr><th>PyTorch</th><td>4</td></tr>'
        '<tr><th>RAM total</th><td>5 B</td></tr>'
        '<tr><th>RAM free</th><td>6 B</td></tr>'
        '<tr><th>argv</th><td>args</td></tr>'
        '<tr><th>device[0] name</th><td>card</td></tr>'
        '<tr><th>device[0] vram_total</th><td>7 B</td></tr>'
        '<tr><th>device[0] vram_free (G view)</th><td>8 B</td></tr>'
        '<tr><th>device[0] torch_vram_total</th><td>9 B</td></tr>'
        '<tr><th>device[0] torch_vram_free</th><td>10 B</td></tr></table>'
    )
    assert firehose_render._render_graph_view(payload) == expected
    assert expected in render_html({**_payload(), firehose_render._GRAPH_WORDS["view_key"]: payload})


def test_device_display_config_controls_order_roles_and_escaping(tmp_path, monkeypatch):
    import yaml
    observation = firehose_render.gpu_observation
    config = observation.load_registry()
    config["devices"][0].update(label="<card>", order=5)
    config["devices"][1]["order"] = 0
    config["devices"][0]["roles"] = [{"label": "custom", "key": "custom_gb"}]
    path = tmp_path / "observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    display = observation.display_config(path)
    assert [row["index"] for row in display["devices"]] == [1, 0]
    monkeypatch.setattr(observation, "display_config", lambda: display)
    rendered = firehose_render._render_budgets({"gpu0": {"custom_gb": 12}})
    assert "&lt;card&gt; custom GB</th><td>12" in rendered
    assert rendered.index(display["devices"][0]["label"]) < rendered.index("&lt;card&gt;")


@pytest.mark.parametrize("defect", ["index", "order", "roles", "template", "runtime"])
def test_device_display_config_rejects_invalid_rows(tmp_path, defect):
    import yaml
    observation = firehose_render.gpu_observation
    config = observation.load_registry()
    if defect == "index":
        config["devices"][1]["index"] = config["devices"][0]["index"]
    elif defect == "order":
        config["devices"][1]["order"] = True
    elif defect == "roles":
        config["devices"][0]["roles"] = []
    elif defect == "template":
        config["devices"][0]["roles"][0]["label"] = "{unknown}"
    else:
        config["runtime_display"]["device_index"] = 999
    path = tmp_path / "observation.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="observation_"):
        observation.display_config(path)
