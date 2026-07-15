import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import firehose_render
from firehose_render import render_html


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
        logout_url="/me/logout",
    )

    assert 'class="shell-header"' in html
    assert 'class="shell-main status-firehose"' in html
    assert 'aria-label="AI Gateway home"' in html
    assert "shell-brand-product" in html
    assert ">AI Gateway</span>" in html
    assert 'href="/status" aria-current="page">Status</a>' in html
    assert 'href="/admin">Admin</a>' in html
    assert 'href="/">Home</a>' in html
    assert "operator@example.com" in html
    assert 'href="/me/logout">Sign out</a>' in html
    assert "gateway.local" in html
    assert "Live activity" not in html
    assert "No jobs in flight" not in html
    assert html.index('<main class="shell-main status-firehose">') < html.index("<footer")
    assert html.index("<footer") < html.index("</main></body></html>")


def test_render_default_status_nav_keeps_status_page_chrome() -> None:
    html = render_html(_payload())

    assert 'class="shell-header"' in html
    assert 'href="/status" aria-current="page">Status</a>' in html
    assert 'href="/admin">Admin</a>' in html
    assert 'href="/">Home</a>' in html
    assert "ai.vivid.fish" in html
    assert "openai.vivid.fish" not in html
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


def test_quick_test_snippets_use_ai_vivid_fish_host() -> None:
    html = firehose_render._render_quick_test()

    assert "https://ai.vivid.fish/openai/v1/chat/completions" in html
    assert "https://ai.vivid.fish/openai/v1/images/generations" in html
    assert "https://ai.vivid.fish/openai/v1/audio/speech" in html
    assert "https://ai.vivid.fish/openai/v1/audio/transcriptions" in html
    assert "https://ai.vivid.fish/openai/v1/videos" in html
    assert "openai.vivid.fish" not in html


def test_module_copy_does_not_say_root_serves_status() -> None:
    assert "`/` and `/firehose` both serve it" not in (firehose_render.__doc__ or "")
    assert "served at `/status`" in (firehose_render.__doc__ or "")
    assert "ai.vivid.fish status dashboard" in (firehose_render.__doc__ or "")
