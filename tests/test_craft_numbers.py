"""The craft-system gates (DESIGN.md, "Gates for these numbers") judge measurements, not pages.

These feed the checks synthetic measure.mjs output, so no browser or fixture is needed.
"""
import json
from pathlib import Path

from scripts.ui import craft_numbers, token_drift

CFG = json.loads(craft_numbers.CONFIG.read_text())


def control(height, kind="button", variant="secondary(outline)", **extra):
    return {"type": kind, "variant": variant, "height": height, "text": "Action", **extra}


def measured(**parts):
    base = {"controls": [], "text": [], "listRows": [], "cards": [], "layout": {}}
    return {**base, **parts}


def test_desktop_controls_must_be_32_or_40():
    data = measured(controls=[control(32), control(40), control(44), control(36)])
    problems = craft_numbers.check_controls(data, 1440, CFG)
    assert [p.split(" is ")[1] for p in problems] == ["44 tall", "36 tall"]


def test_controls_exempt_tiles_textareas_switches_links_and_measure_segments_by_group():
    data = measured(controls=[
        control(179),  # a media tile
        control(80, kind="textarea"),
        control(20, kind="switch"),
        control(28, variant="link"),
        control(28, kind="tab/segment", groupHeight=32),
        control(28, pressed=True, groupHeight=32),
    ])
    assert craft_numbers.check_controls(data, 1440, CFG) == []


def test_phone_controls_are_left_to_the_touch_target_gate():
    assert craft_numbers.check_controls(measured(controls=[control(44)]), 390, CFG) == []


def text(size, role="p", **extra):
    return {"fontSize": size, "role": role, "text": "words", **extra}


def test_type_allows_four_sizes_plus_badges_and_mark_initials():
    data = measured(text=[text(24, "h1"), text(16, "h2"), text(15), text(14),
                          text(12, "badge"), text(10, "span", inMark=True)])
    assert craft_numbers.check_type(data, 1440, CFG) == []


def test_type_flags_sizes_outside_the_table_and_a_fifth_size():
    data = measured(text=[text(24, "h1"), text(16, "h2"), text(15), text(14), text(12), text(10)])
    problems = craft_numbers.check_type(data, 1440, CFG)
    assert any("10 px text" in p for p in problems)
    assert any(p.startswith("6 text sizes") for p in problems)


def test_rows_follow_the_row_tokens_per_width():
    rows = [{"kind": "list-row", "height": 40, "text": "a"}, {"kind": "list-row", "height": 45, "text": "b"},
            {"kind": "setting-row", "height": 64, "text": "c"}, {"kind": "setting-row", "height": 56, "text": "d"}]
    desktop = craft_numbers.check_rows(measured(listRows=rows), 1440, CFG)
    assert desktop == ["list-row 'b' is 45 tall", "setting row 'd' is 56 tall"]
    phone = craft_numbers.check_rows(measured(listRows=[{"kind": "table-row", "height": 48, "text": "e"}]), 390, CFG)
    assert phone == []
    # A setting may grow on a desktop (a long label wraps) but not on a phone.
    tall = [{"kind": "setting-row", "height": 86, "text": "f"}]
    assert craft_numbers.check_rows(measured(listRows=tall), 1440, CFG) == []
    assert craft_numbers.check_rows(measured(listRows=tall), 390, CFG) == ["setting row 'f' is 86 tall"]


def test_geometry_reads_page_padding_and_section_gaps():
    data = measured(layout={"main": {"padding": "32px 48px 40px 48px"}, "sectionGaps": [32, 20]})
    assert craft_numbers.check_geometry(data, 1440, CFG) == [
        "page padding is 48, not 32", "a section gap is 20, not 32"]


def test_geometry_expects_24_under_the_page_header_and_32_between_sections():
    layout = {"main": {"padding": "32px 32px 40px 32px"},
              "sections": [{"tag": "header"}, {"tag": "section"}, {"tag": "section"}],
              "sectionGaps": [24, 32]}
    assert craft_numbers.check_geometry(measured(layout=layout), 1440, CFG) == []
    layout["sectionGaps"] = [32, 32]
    assert craft_numbers.check_geometry(measured(layout=layout), 1440, CFG) == ["a section gap is 32, not 24"]


def test_cards_need_radius_8_and_no_shadow_but_media_is_exempt():
    def card(radius, shadow=False, classes="rounded-panel border"):
        return {"box": {"w": 300, "h": 100}, "hasBorder": True, "hasShadow": shadow,
                "borderRadius": f"{radius}px", "classes": classes, "firstText": "Card"}
    data = measured(cards=[card(8), card(10), card(8, shadow=True), card(6, classes="rounded-media border")])
    assert craft_numbers.check_cards(data, 1440, CFG) == ["card 'Card' has radius 10", "card 'Card' has a shadow"]


def test_every_enforced_route_and_check_exists():
    for route, checks in CFG["enforce"].items():
        assert route in CFG["routes"]
        assert set(checks) <= set(craft_numbers.CHECKS)


def drift(tmp_path, monkeypatch, source, name="components/ui/thing.tsx", system=True):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    monkeypatch.setattr(token_drift, "ROOT", tmp_path)
    return [(rule, text) for _, rule, text in token_drift.scan_system(path)]


def test_drift_flags_desktop_44_px_and_allows_it_under_max_sm(tmp_path, monkeypatch):
    hits = drift(tmp_path, monkeypatch, 'const a = "min-h-11 h-11 sm:min-h-11 max-sm:min-h-11 max-sm:h-11 size-11";\n')
    assert hits == [("44 px off the phone", "min-h-11"), ("44 px off the phone", "h-11"),
                    ("44 px off the phone", "sm:min-h-11")]


def test_drift_flags_kit_radius_shadow_and_pixel_text_outside_overlays(tmp_path, monkeypatch):
    source = 'const a = "rounded-xl md:rounded-lg shadow-sm text-[13px] rounded-panel shadow-overlay";\n'
    hits = drift(tmp_path, monkeypatch, source)
    assert [rule for rule, _ in hits] == ["kit radius", "kit radius", "raw font size", "kit shadow"]
    overlay = drift(tmp_path, monkeypatch, 'const a = "shadow-md";\n', name="components/ui/popover.tsx")
    assert overlay == []


def test_drift_honours_a_craft_allow_comment_for_one_line(tmp_path, monkeypatch):
    source = '// craft-allow: the full-width phone action\nconst a = "h-11";\nconst b = "h-11";\n'
    assert drift(tmp_path, monkeypatch, source) == [("44 px off the phone", "h-11")]
