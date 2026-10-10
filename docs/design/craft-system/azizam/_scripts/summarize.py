#!/usr/bin/env python3
"""Summarize measure.mjs output into summary.json + summary.md. Usage: summarize.py <dir>"""
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

D = Path(sys.argv[1])
SURFACES = ["home", "status", "library", "try", "people", "usage", "workflows", "settings"]
WIDTHS = [1440, 390]
LIST_HEAVY = {"people", "usage", "workflows"}
BODY_ROLES = {"p", "span", "div", "li", "dd", "table-cell", "time", "description", "link", "strong", "em", "small"}
NON_BODY = {"h1", "h2", "h3", "h4", "h5", "h6", "heading-class", "button", "tab/segment", "select", "label", "badge", "table-header", "dt", "code"}


def rad(v):
    first = v.split()[0] if v else "0px"
    n = float(first.replace("px", "") or 0)
    return "full" if n > 999 else f"{n:g}px"


def mode(values):
    c = Counter(values)
    return c.most_common(1)[0][0] if c else None


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return {"median": st.median(xs), "min": min(xs), "max": max(xs), "n": len(xs)}


def pixel_share(png, sidebar_w):
    im = np.asarray(Image.open(png).convert("RGB")).astype(np.float32) / 255.0
    def shares(a):
        mx, mn = a.max(axis=2), a.min(axis=2)
        l = (mx + mn) / 2
        d = mx - mn
        denom = 1 - np.abs(2 * l - 1)
        s = np.where(denom > 1e-6, d / np.maximum(denom, 1e-6), 0)
        total = a.shape[0] * a.shape[1]
        bg = np.all(np.abs(a - np.array([243, 246, 246]) / 255.0) < 1.5 / 255, axis=2)
        white = np.all(a > 254.5 / 255, axis=2)
        neutral_hsl = s < 0.12
        neutral_chroma = d < 0.08  # max-min channel spread < ~20/255
        nb = ~bg
        return {
            "pixels": int(total),
            "page_bg_share": round(float(bg.mean()), 4),
            "white_share": round(float(white.mean()), 4),
            "hsl_sat_lt_0.12": {"neutral": round(float(neutral_hsl.mean()), 4), "coloured": round(float(1 - neutral_hsl.mean()), 4)},
            "hsl_sat_lt_0.12_excl_page_bg": {"neutral": round(float(neutral_hsl[nb].mean()), 4) if nb.any() else None,
                                             "coloured": round(float(1 - neutral_hsl[nb].mean()), 4) if nb.any() else None},
            "chroma_lt_0.08": {"neutral": round(float(neutral_chroma.mean()), 4), "coloured": round(float(1 - neutral_chroma.mean()), 4)},
            "chroma_lt_0.08_excl_page_bg": {"neutral": round(float(neutral_chroma[nb].mean()), 4) if nb.any() else None,
                                            "coloured": round(float(1 - neutral_chroma[nb].mean()), 4) if nb.any() else None},
        }
    return {"whole_viewport": shares(im), "content_area_only": shares(im[:, sidebar_w:, :]) if sidebar_w else None}


def summarize(d, surface, width):
    L = d["layout"]
    text = d["text"]
    # font sizes
    fs = defaultdict(lambda: {"count": 0, "roles": Counter(), "weights": Counter()})
    for t in text:
        e = fs[t["fontSize"]]
        e["count"] += 1
        e["roles"][t["role"]] += 1
        e["weights"][t["fontWeight"]] += 1
    font_sizes = {f"{k:g}": {"count": v["count"], "roles": dict(v["roles"].most_common()), "weights": dict(v["weights"])} for k, v in sorted(fs.items())}
    def pick(pred):
        sel = [t for t in text if pred(t)]
        if not sel:
            return None
        size = mode(t["fontSize"] for t in sel)
        same = [t for t in sel if t["fontSize"] == size]
        return {"size": size, "weight": mode(t["fontWeight"] for t in same), "lineHeight": mode(t["lineHeight"] for t in same),
                "n": len(sel), "sizes": dict(Counter(t["fontSize"] for t in sel).most_common())}
    section_heading = pick(lambda t: t["role"] == "h2") or pick(lambda t: t["role"] in ("h3", "heading-class") and t["fontSize"] < 24)
    body = pick(lambda t: t["role"] in BODY_ROLES and t["tone"] == "foreground")
    secondary = pick(lambda t: t["role"] not in NON_BODY and t["tone"] == "muted")
    label = pick(lambda t: t["role"] in ("label", "dt", "table-header"))
    # controls
    ctrl_heights = defaultdict(Counter)
    tiles = Counter()
    for c in d["controls"]:
        key = c["type"] if c["type"] != "button" else f"button:{c['variant']}"
        if c["type"] == "button" and c["height"] > 64:
            tiles[c["height"]] += 1
            continue
        ctrl_heights[key][c["height"]] += 1
    for g in d["controlGroups"]:
        ctrl_heights[f"group:{g['role']}"][g["height"]] += 1
    ctrl_heights = {k: {f"{h:g}": n for h, n in sorted(v.items())} for k, v in sorted(ctrl_heights.items())}
    # radii
    radii = defaultdict(Counter)
    for c in d["controls"]:
        radii[c["type"] if c["type"] != "button" else f"button:{c['variant']}"][rad(c["borderRadius"])] += 1
    for g in d["controlGroups"]:
        radii[f"group:{g['role']}"][rad(g["borderRadius"])] += 1
    for c in d["cards"]:
        kind = "card/panel" if c["box"]["w"] >= 120 and c["box"]["h"] >= 60 else "chip/badge/small-surface"
        radii[kind][rad(c["borderRadius"])] += 1
    radii = {k: dict(v) for k, v in sorted(radii.items())}
    # cards
    big = [c for c in d["cards"] if c["box"]["w"] >= 120 and c["box"]["h"] >= 60]
    card_padding = dict(Counter(c["padding"] for c in big).most_common())
    surf = Counter()
    for c in d["cards"]:
        surf["border+shadow" if c["hasBorder"] and c["hasShadow"] else "shadow-only" if c["hasShadow"] else "border-only" if c["hasBorder"] else "fill-only"] += 1
    ctrl_surf = Counter()
    for c in d["controls"]:
        b = float(c["borderWidth"].replace("px", "") or 0) > 0 and c["borderStyle"] != "none" and c["borderColor"] not in ("rgba(0, 0, 0, 0)", "transparent")
        s = c["boxShadow"] != "none"
        ctrl_surf["border+shadow" if b and s else "shadow-only" if s else "border-only" if b else "none"] += 1
    # rows
    lists = [r for r in d["rowLists"] if r["count"] >= 3 and not r["inTablist"] and r["layout"] in ("vertical", "grid") and st.median(r["itemHeights"]) >= 28]
    rows = [{"parent": r["parent"][:50], "sample": r["sample"][:30], "items": r["count"], "layout": r["layout"], "visualRows": r["visualRows"],
             "rowHeight_median": st.median(r["heights"]), "rowGap_median": st.median(r["gaps"]) if r["gaps"] else None,
             "columnGap_median": st.median(r["columnGaps"]) if r["columnGaps"] else None, "spanPx": r["spanPx"],
             "rows_per_1000px": round(r["visualRows"] / r["spanPx"] * 1000, 1) if r["spanPx"] else None} for r in lists]
    density = None
    if surface in LIST_HEAVY:
        if rows:
            primary = max(rows, key=lambda r: (r["visualRows"], r["spanPx"]))
            density = {"primary_list": primary["sample"], "rows_per_1000px_in_list": primary["rows_per_1000px"],
                       "all_list_rows": sum(r["visualRows"] for r in rows),
                       "all_list_rows_per_1000px_of_page": round(sum(r["visualRows"] for r in rows) / L["pageHeight"] * 1000, 1)}
        # card grids (e.g., Workflows) count as repeated items too
        card_items = [c for c in big if c["tag"] in ("article", "li", "div", "section") and c["box"]["h"] >= 150]
        density = (density or {}) | {"cards_ge150px": len(card_items), "cards_per_1000px_of_page": round(len(card_items) / L["pageHeight"] * 1000, 1)}
    gaps = [g["gap"] for g in L["stackGaps"]] or L["sectionGaps"]
    content_edge = L["sidebarWidth"] + (4 if L["sidebarWidth"] else 0)
    return {
        "surface": surface, "width": width, "route": d["actualRoute"], "pageHeight": L["pageHeight"],
        "counts": {"text": len(text), "controls": len(d["controls"]), "cards": len(d["cards"])},
        "font_sizes": font_sizes, "distinct_font_sizes": len(fs), "distinct_font_weights": sorted({t["fontWeight"] for t in text}),
        "title": L["title"], "section_heading": section_heading, "body": body, "secondary": secondary, "label": label,
        "control_heights": ctrl_heights, "tile_buttons": {f"{h:g}": n for h, n in tiles.items()}, "radii": radii,
        "page_padding": {"left_from_viewport": L["contentLeftFromViewport"], "right_from_viewport": L["contentRightFromViewport"],
                          "left_from_shell_edge": L["contentLeftFromViewport"] - L["sidebarWidth"], "main_padding": L["main"]["padding"],
                          "main_max_width": L["main"]["maxWidth"], "title_top": L["titleTop"], "sidebar_width": L["sidebarWidth"]},
        "section_gaps": stats(gaps), "section_gap_values": gaps, "top_level_gaps": L["sectionGaps"],
        "heading_to_content_gaps": [g["gap"] for g in L["headingGaps"] if g["gap"] >= 0],
        "rows": rows, "density": density, "card_padding": card_padding,
        "icons": dict(sorted(d["icons"].items(), key=lambda kv: -kv[1])),
        "surface_treatment": {"cards": dict(surf), "controls": dict(ctrl_surf)},
        "text_colors": dict(sorted(d["textColors"].items(), key=lambda kv: -kv[1])),
        "bg_colors": dict(sorted(d["bgColors"].items(), key=lambda kv: -kv[1])),
    }


out = {"surfaces": {}, "shell": {}}
for w in WIDTHS:
    shell = json.loads((D / f"shell-{w}.json").read_text())
    out["shell"][str(w)] = {"font_sizes": dict(Counter(f"{t['fontSize']:g}" for t in shell["text"])),
                            "weights": dict(Counter(t["fontWeight"] for t in shell["text"])),
                            "control_heights": dict(Counter(f"{c['type']}:{c['height']:g}" for c in shell["controls"])),
                            "radii": dict(Counter(rad(c["borderRadius"]) for c in shell["controls"])),
                            "icons": shell["icons"]}
for s in SURFACES:
    for w in WIDTHS:
        d = json.loads((D / f"{s}-{w}.json").read_text())
        r = summarize(d, s, w)
        if w == 1440:
            r["pixels"] = pixel_share(D / f"{s}-1440.png", d["layout"]["sidebarWidth"])
        out["surfaces"][f"{s}-{w}"] = r

# cross-surface at 1440
agg_fs, agg_fw, agg_ch, agg_r = Counter(), Counter(), Counter(), Counter()
for s in SURFACES:
    d = json.loads((D / f"{s}-1440.json").read_text())
    for t in d["text"]:
        agg_fs[t["fontSize"]] += 1
        agg_fw[t["fontWeight"]] += 1
    for c in d["controls"]:
        if not (c["type"] == "button" and c["height"] > 64):
            agg_ch[c["height"]] += 1
        agg_r[rad(c["borderRadius"])] += 1
    for c in d["cards"]:
        agg_r[rad(c["borderRadius"])] += 1
    for g in d["controlGroups"]:
        agg_r[rad(g["borderRadius"])] += 1
out["across_8_surfaces_1440"] = {
    "distinct_font_sizes": len(agg_fs), "font_sizes": {f"{k:g}": v for k, v in sorted(agg_fs.items())},
    "distinct_font_weights": len(agg_fw), "font_weights": dict(sorted(agg_fw.items())),
    "distinct_control_heights": len(agg_ch), "control_heights": {f"{k:g}": v for k, v in sorted(agg_ch.items())},
    "distinct_radii": len(agg_r), "radii": dict(sorted(agg_r.items(), key=lambda kv: (kv[0] == "full", float(kv[0][:-2]) if kv[0] != "full" else 0))),
}
(D / "summary.json").write_text(json.dumps(out, indent=1))

# markdown
def fmt(d):
    if not d:
        return "-"
    return ", ".join(f"{k}x{v}" if isinstance(v, int) else f"{k}: {v}" for k, v in d.items())

def ty(x):
    return f"{x['size']:g}/{x['weight']}/{x['lineHeight']}" if x else "-"

md = ["# Azizam measured UI summary", "",
      "Light theme, owner (Sam) session, household seed (default scenario), DPR 1. Sizes in CSS px. Typography as size/weight/line-height.",
      "Generated by `_scripts/measure.mjs` (Playwright computed styles) and `_scripts/summarize.py`.", "",
      "Pixel note: the palette is teal-tinted (page #f3f6f6, ink #10232a, muted #3d4f55 all have HSL saturation >0.12), so the requested HSL-saturation<0.12 test classes most UI greys as coloured. The chroma test (max-min RGB channel spread < 0.08 = ~20/255) is the perceptually meaningful neutral/accent split; both are reported. Library/Home viewports include photographs.", "",
      "## Across all 8 surfaces at 1440", ""]
a = out["across_8_surfaces_1440"]
md += [f"- Distinct font sizes: **{a['distinct_font_sizes']}** ({fmt(a['font_sizes'])})",
       f"- Distinct font weights: **{a['distinct_font_weights']}** ({fmt(a['font_weights'])})",
       f"- Distinct control heights (excl. >64px media tiles): **{a['distinct_control_heights']}** ({fmt(a['control_heights'])})",
       f"- Distinct radii (controls+groups+cards/chips): **{a['distinct_radii']}** ({fmt(a['radii'])})", ""]
md += ["## Shell", ""]
for w, sh in out["shell"].items():
    md += [f"- **{w}**: font sizes {fmt(sh['font_sizes'])}; weights {fmt(sh['weights'])}; controls {fmt(sh['control_heights'])}; radii {fmt(sh['radii'])}; icons {fmt(sh['icons'])}"]
md += [""]
for key, r in out["surfaces"].items():
    p = r["page_padding"]
    sg = r["section_gaps"]
    md += [f"## {r['surface']} @ {r['width']}  (`{r['route']}`, page height {r['pageHeight']})", "", "| Metric | Value |", "|---|---|"]
    rows = [
        ("Distinct font sizes", f"{r['distinct_font_sizes']}; weights {r['distinct_font_weights']}"),
        ("Font sizes (count, roles)", "<br>".join(f"{k}px x{v['count']} ({', '.join(f'{rr}:{n}' for rr, n in list(v['roles'].items())[:5])})" for k, v in r["font_sizes"].items())),
        ("Page title", f"{r['title']['fontSize']:g}/{r['title']['fontWeight']}/{r['title']['lineHeight']:g}, tracking {r['title']['letterSpacing']}" if r["title"] else "-"),
        ("Section heading (h2)", ty(r["section_heading"])),
        ("Body (foreground text)", ty(r["body"]) + (f" (sizes {fmt({f'{k:g}': v for k, v in r['body']['sizes'].items()})})" if r["body"] else "")),
        ("Secondary (muted text)", ty(r["secondary"]) + (f" (sizes {fmt({f'{k:g}': v for k, v in r['secondary']['sizes'].items()})})" if r["secondary"] else "")),
        ("Label / th / dt", ty(r["label"])),
        ("Control heights", "<br>".join(f"{k}: {fmt(v)}" for k, v in r["control_heights"].items()) or "-"),
        ("Media tile buttons (>64px)", fmt(r["tile_buttons"])),
        ("Radii by element", "<br>".join(f"{k}: {fmt(v)}" for k, v in r["radii"].items()) or "-"),
        ("Page padding L/R (from viewport)", f"{p['left_from_viewport']} / {p['right_from_viewport']} (L from shell edge {p['left_from_shell_edge']}; main padding {p['main_padding']}; max-width {p['main_max_width']})"),
        ("Title top (from page top)", p["title_top"]),
        ("Section gaps", f"median {sg['median']}, min {sg['min']}, max {sg['max']} (n={sg['n']}; values {r['section_gap_values']})" if sg else "-"),
        ("Heading -> content gaps", r["heading_to_content_gaps"] or "-"),
        ("Repeated rows", "<br>".join(f"{x['sample'] or x['parent']}: {x['items']} items, {x['layout']}, row h {x['rowHeight_median']}, gap {x['rowGap_median']}" + (f", col gap {x['columnGap_median']}" if x['columnGap_median'] is not None else "") + f", {x['rows_per_1000px']} rows/1000px" for x in r["rows"]) or "-"),
        ("Density (list-heavy)", ", ".join(f"{k}: {v}" for k, v in r["density"].items()) if r["density"] else "n/a"),
        ("Card padding", fmt(r["card_padding"])),
        ("Icon sizes", fmt(r["icons"])),
        ("Border vs shadow", f"cards {fmt(r['surface_treatment']['cards'])}; controls {fmt(r['surface_treatment']['controls'])}"),
        ("Text colours", fmt(r["text_colors"])),
        ("Background colours", fmt(r["bg_colors"])),
    ]
    if "pixels" in r:
        px = r["pixels"]["whole_viewport"]
        ca = r["pixels"]["content_area_only"]
        rows.append(("Pixels (1440 viewport)", f"HSL sat<0.12 neutral {px['hsl_sat_lt_0.12']['neutral']:.1%} / coloured {px['hsl_sat_lt_0.12']['coloured']:.1%}; excl. page bg neutral {px['hsl_sat_lt_0.12_excl_page_bg']['neutral']:.1%}; "
                     f"chroma<0.08 neutral {px['chroma_lt_0.08']['neutral']:.1%} (excl. page bg {px['chroma_lt_0.08_excl_page_bg']['neutral']:.1%}); page bg {px['page_bg_share']:.1%}, white {px['white_share']:.1%}"))
        if ca:
            rows.append(("Pixels (content area, sidebar excluded)", f"HSL neutral {ca['hsl_sat_lt_0.12']['neutral']:.1%} (excl. page bg {ca['hsl_sat_lt_0.12_excl_page_bg']['neutral']:.1%}); chroma neutral {ca['chroma_lt_0.08']['neutral']:.1%} (excl. page bg {ca['chroma_lt_0.08_excl_page_bg']['neutral']:.1%})"))
    md += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows] + [""]
(D / "summary.md").write_text("\n".join(md))
print("ok")
