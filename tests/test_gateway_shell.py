"""Tests for the shared gateway shell chrome.

The deployment brand is data, not baked into product code: a generic install
must never render "vivid.fish" unless the deployment opts in via the wordmark.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.gateway_shell import render_shell_header  # noqa: E402


class ShellBrandTests(unittest.TestCase):
    def test_default_header_has_no_deployment_brand(self) -> None:
        html = render_shell_header(brand="AI Gateway")
        # A generic install must not leak Tim's deployment brand.
        self.assertNotIn("vivid", html.lower())
        self.assertNotIn("shell-brand-word", html)
        self.assertIn("AI Gateway", html)

    def test_wordmark_renders_stylized_deployment_mark(self) -> None:
        html = render_shell_header(brand="AI Gateway", wordmark=("vivid", "fish"))
        self.assertIn("shell-brand-word", html)
        self.assertIn("<em>vivid</em>", html)
        self.assertIn(">fish</span>", html)
        # Product label still present alongside the wordmark.
        self.assertIn("AI Gateway", html)

    def test_wordmark_is_deployment_neutral(self) -> None:
        # Any deployment can set its own mark; nothing hardcodes vivid.fish.
        html = render_shell_header(brand="My Stack", wordmark=("acme", "dev"))
        self.assertIn("<em>acme</em>", html)
        self.assertIn(">dev</span>", html)
        self.assertIn("My Stack", html)
        self.assertNotIn("vivid", html.lower())

    def test_wordmark_parts_are_escaped(self) -> None:
        html = render_shell_header(
            brand="Brand", wordmark=("<script>", "</script>")
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class BottomTabsTests(unittest.TestCase):
    def test_default_tabs_list_only_real_destinations(self) -> None:
        # A tab that scrolls nowhere is nav that lies. The default set lists ONLY
        # destinations that exist today (Home); the design's Keys/What-works/
        # Activity tabs are added back when each target section ships (Phase 2).
        from gateway.gateway_shell import render_bottom_tabs

        html = render_bottom_tabs(active="home", admin=False)
        self.assertIn('class="tabbar"', html)
        self.assertIn(">Home</span>", html)
        # No phantom destinations.
        for phantom in ("Keys", "What works", "Activity", "You"):
            self.assertNotIn(f">{phantom}</span>", html)
        self.assertNotIn("/#keys", html)
        self.assertNotIn("/#caps", html)
        self.assertNotIn("/#activity", html)
        # Active tab is marked.
        self.assertIn('class="tabbar-item is-active"', html)
        self.assertIn('aria-current="page"', html)

    def test_admin_gets_a_real_admin_tab(self) -> None:
        from gateway.gateway_shell import render_bottom_tabs

        html = render_bottom_tabs(active="me", admin=True)
        self.assertIn(">Admin</span>", html)
        self.assertIn('href="/admin"', html)
        self.assertNotIn(">You</span>", html)

    def test_tabbar_is_mobile_only_and_safe_area_aware(self) -> None:
        # The shared CSS hides the bar on desktop and reserves space on mobile.
        from gateway.gateway_shell import SHELL_CSS

        self.assertIn(".tabbar", SHELL_CSS)
        self.assertIn("body.has-tabbar", SHELL_CSS)
        self.assertIn("env(safe-area-inset-bottom", SHELL_CSS)
        self.assertIn("@media (min-width: 721px)", SHELL_CSS)

    def test_shell_main_descendants_cannot_force_document_overflow(self) -> None:
        # The portal home nests many display:grid/display:flex containers
        # (.shell-main > section, .vf-section, .apps-list, .app-card-body,
        # .key-list, ...). A flex/grid item's default min-width is `auto`
        # (its content's max-content size), not 0 — so ANY one of those
        # nesting levels can force its ancestor track, and therefore the
        # whole document, wider than the viewport at mobile widths, even
        # though the text itself would wrap fine given room. Patching this
        # selector-by-selector as new chains are discovered is exactly the
        # failure mode that let it recur four times in one session — this
        # single universal rule removes the trap at every current AND
        # future nesting depth in one place. Explicit fixed-width children
        # (flex: 0 0 auto, an explicit min-width, or an overflow-x: auto
        # code block) are unaffected: min-width: 0 only relaxes the
        # *default* floor, it never overrides an explicit one.
        #
        # Reproduced live at 375x812/390x844 (scrollWidth 484-490 vs
        # clientWidth 375/390) across four narrower fix attempts before
        # this universal rule; re-verified live scrollWidth == clientWidth
        # at both breakpoints after it (see the session's final report).
        from gateway.gateway_shell import SHELL_CSS

        self.assertRegex(
            SHELL_CSS,
            r"\.shell-main,\s*\.shell-main \*\s*\{[^}]*min-width:\s*0;[^}]*\}",
        )

    def test_no_narrow_reintroduced_min_width_zero_patch_shadows_the_universal_rule(self) -> None:
        # Guards against regressing back to selector-by-selector patching:
        # once the universal .shell-main * rule exists, a narrower
        # `min-width: 0` added later for one specific selector is dead
        # weight (or worse, a sign the universal rule was scoped away by
        # accident). This does not forbid EVERY narrow min-width: 0 in the
        # file (some predate the universal rule and are harmless no-ops),
        # but it does assert the specific selectors this session added and
        # then folded into the universal rule stay folded, so a future
        # editor doesn't reintroduce narrow patches under the mistaken
        # belief the universal rule doesn't cover them.
        from gateway.gateway_shell import SHELL_CSS

        self.assertNotRegex(
            SHELL_CSS,
            r"\.shell-main > section > \*\s*\{[^}]*min-width:\s*0;[^}]*\}",
        )


if __name__ == "__main__":
    unittest.main()
