// Capture + computed-style measurement for the Azizam craft comparison.
// Usage: node measure.mjs <manifest.json> <outdir>   (server must already be running)
// Env (all optional; defaults reproduce MEASUREMENTS.md):
//   MEASURE_SURFACES=home,status   only these surfaces
//   MEASURE_WIDTHS=1440            only these widths
//   MEASURE_SHOTS=all|viewport|none  screenshots to write (default all)
// scripts/ui/craft_numbers.py runs this as its measuring step.
import { createRequire } from "node:module";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "../../../../../web");
const require = createRequire(path.join(webDir, "package.json"));
const { chromium } = require("@playwright/test");

const [manifestPath, outDir] = process.argv.slice(2);
const seed = JSON.parse(await readFile(manifestPath, "utf8"));
const base = seed.base_url;
await mkdir(outDir, { recursive: true });

const allSurfaces = [
  ["home", "/ui/home/"], ["status", "/ui/status/"], ["library", "/ui/library/"],
  ["try", "/ui/try/"], ["people", "/ui/people/"], ["usage", "/ui/usage/"],
  ["workflows", "/ui/workflows/"], ["settings", "/ui/settings/"],
];
// Measured only when named in MEASURE_SURFACES (not in MEASUREMENTS.md).
const extraSurfaces = [
  ["apps", "/ui/apps/"], ["activity", "/ui/activity/"], ["models", "/ui/models/"],
  ["engines", "/ui/engines/"], ["providers", "/ui/providers/"],
];
// Activity opens on Changes; the craft PR 6 surface is the Requests tab.
const prepare = {
  activity: async page => {
    await page.getByRole("tab", { name: "Requests", exact: true }).click();
    await settled(page);
  },
};
const only = (process.env.MEASURE_SURFACES ?? "").split(",").map(s => s.trim()).filter(Boolean);
const surfaces = only.length ? [...allSurfaces, ...extraSurfaces].filter(([name]) => only.includes(name)) : allSurfaces;
const widths = (process.env.MEASURE_WIDTHS ?? "1440,390").split(",").map(Number).filter(Boolean);
const shots = process.env.MEASURE_SHOTS ?? "all";

const NO_ANIM = `*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition:none!important;caret-color:transparent!important}`;

async function settled(page) {
  await page.waitForLoadState("networkidle", { timeout: 30_000 });
  await page.waitForFunction(() => [...document.querySelectorAll("h1")].some(e => e.getBoundingClientRect().width > 0), null, { timeout: 15_000 });
  await page.waitForFunction(() => {
    const vis = e => { const s = getComputedStyle(e); return s.display !== "none" && s.visibility !== "hidden" && e.getBoundingClientRect().width > 0; };
    return ![...document.querySelectorAll('[aria-busy="true"], [data-slot="skeleton"], .animate-pulse')].some(vis);
  }, null, { timeout: 20_000 }).catch(() => console.warn("  skeleton/busy still visible at", page.url()));
  await page.evaluate(async () => {
    await document.fonts.ready;
    const imgs = Array.from(document.images);
    imgs.forEach(i => { i.loading = "eager"; });
    await Promise.all(imgs.map(i => i.decode().catch(() => {})));
  });
  await page.waitForTimeout(500);
}

// ---------------- in-page measurement ----------------
function measure(scope) {
  const vw = window.innerWidth, vh = window.innerHeight, sy = window.scrollY;
  const main = document.querySelector("#main-content");
  let roots;
  if (scope === "main") roots = [main];
  else roots = [...document.querySelectorAll("aside.shell-sidebar, header.sticky, nav[aria-label='Phone navigation']")];
  const visible = e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
    if ((s.position === "absolute" || s.position === "fixed") && r.width <= 1 && r.height <= 1) return false; // sr-only
    if (s.clip === "rect(0px, 0px, 0px, 0px)" || s.clipPath === "inset(50%)") return false;
    // hidden ancestors (display/visibility checked by rect already; check opacity chain)
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      const ps = getComputedStyle(p);
      if (Number(ps.opacity) === 0) return false;
      if (ps.clip === "rect(0px, 0px, 0px, 0px)" || ps.clipPath === "inset(50%)") return false;
      const pr = p.getBoundingClientRect();
      if (ps.overflow === "hidden" && pr.width <= 1 && pr.height <= 1) return false; // inside sr-only wrapper
    }
    return true;
  };
  const box = e => { const r = e.getBoundingClientRect(); return { x: Math.round(r.left), y: Math.round(r.top + sy), w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10, inViewport: r.top < vh && r.bottom > 0 }; };
  const cls = e => (typeof e.className === "string" ? e.className : e.getAttribute("class") || "");
  const all = roots.flatMap(r => r ? [r, ...r.querySelectorAll("*")] : []);
  const pageBg = getComputedStyle(document.body).backgroundColor;
  const probe = c => { const d = document.createElement("div"); d.className = c; d.textContent = "x"; (main || document.body).appendChild(d); const v = getComputedStyle(d).color; const b = getComputedStyle(d).backgroundColor; d.remove(); return { color: v, bg: b }; };
  const tokens = { foreground: probe("text-foreground").color, muted: probe("text-muted-foreground").color, pageBg, cardBg: probe("bg-card").bg, mutedBg: probe("bg-muted").bg };

  const CONTROL_SEL = "button, a[data-slot=button], [role=button], input:not([type=hidden]), select, textarea, [role=combobox], [role=tab], [role=radio], [role=switch], [role=checkbox], [role=menuitem]";
  const roleGuess = e => {
    const t = e.tagName.toLowerCase();
    if (/^h[1-6]$/.test(t)) return t;
    const c = cls(e);
    // A badge is a badge inside a row button or a table cell too.
    if (e.closest("[data-slot=badge]") || /\bbadge\b|rounded-full.*text-xs|pill/i.test(c)) return "badge";
    const ctrl = e.closest("[role=tab], [role=radio]");
    if (ctrl) return "tab/segment";
    if (e.closest("button, a[data-slot=button], [role=button]")) return "button";
    if (e.closest("[role=combobox], select")) return "select";
    if (t === "label" || e.closest("label") || e.getAttribute("data-slot") === "label") return "label";
    if (e.closest("th")) return "table-header";
    if (e.closest("td, [role=cell], [role=gridcell]")) return "table-cell";
    if (/\b(title|heading)\b|CardTitle|card-title/i.test(c) || e.getAttribute("data-slot")?.includes("title")) return "heading-class";
    if (e.getAttribute("data-slot")?.includes("description")) return "description";
    if (t === "a" || e.closest("a")) return "link";
    if (t === "kbd" || t === "code" || e.closest("code, kbd, pre")) return "code";
    if (e.closest("dt")) return "dt";
    if (e.closest("dd")) return "dd";
    if (t === "p") return "p";
    if (t === "li" || e.closest("li")) return "li";
    return t;
  };

  // --- text ---
  const text = [];
  for (const e of all) {
    if (["SCRIPT", "STYLE", "SVG", "NOSCRIPT"].includes(e.tagName) || e.closest("svg")) continue;
    const direct = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join("").replace(/\s+/g, " ").trim();
    if (!direct) continue;
    if (!visible(e)) continue;
    const s = getComputedStyle(e);
    text.push({
      tag: e.tagName.toLowerCase(), text: direct.slice(0, 40), role: roleGuess(e),
      fontSize: parseFloat(s.fontSize), fontWeight: Number(s.fontWeight), lineHeight: s.lineHeight === "normal" ? "normal" : parseFloat(s.lineHeight),
      letterSpacing: s.letterSpacing, color: s.color, fontFamily: s.fontFamily.split(",")[0].replace(/["']/g, "").trim(),
      tone: s.color === tokens.foreground ? "foreground" : s.color === tokens.muted ? "muted" : "other",
      textTransform: s.textTransform, box: box(e), inMark: !!e.closest("[data-slot=person-mark]"),
    });
  }

  // --- controls ---
  const controls = [];
  const seen = new Set();
  for (const e of roots.flatMap(r => r ? [...r.querySelectorAll(CONTROL_SEL)] : [])) {
    if (seen.has(e) || !visible(e)) continue;
    seen.add(e);
    const s = getComputedStyle(e);
    const t = e.tagName.toLowerCase();
    const role = e.getAttribute("role");
    const c = cls(e);
    let type;
    if (role === "tab" || role === "radio") type = "tab/segment";
    else if (role === "switch") type = "switch";
    else if (role === "checkbox" || (t === "input" && ["checkbox", "radio"].includes(e.type))) type = "checkbox";
    else if (role === "combobox" || t === "select" || e.getAttribute("data-slot") === "select-trigger") type = "select";
    else if (t === "input") type = "input";
    else if (t === "textarea") type = "textarea";
    else type = "button";
    let variant = e.getAttribute("data-variant");
    if (type === "button" && !variant) {
      if (/\bbg-primary\b/.test(c)) variant = "default"; else if (/\bborder\b/.test(c)) variant = "outline"; else if (/\btext-link\b|underline/.test(c)) variant = "link"; else variant = "unstyled/ghost-like";
    }
    const vmap = { default: "primary", outline: "secondary(outline)", secondary: "secondary", ghost: "ghost", link: "link", destructive: "destructive" };
    const group = e.closest("[role=tablist], [role=radiogroup], [role=group]");
    controls.push({
      type, variant: variant ? (vmap[variant] || variant) : null, size: e.getAttribute("data-size"),
      groupHeight: group && visible(group) ? Math.round(group.getBoundingClientRect().height * 10) / 10 : null,
      pressed: e.hasAttribute("aria-pressed"),
      tag: t, role, text: (e.getAttribute("aria-label") || e.textContent || e.placeholder || "").replace(/\s+/g, " ").trim().slice(0, 40),
      height: Math.round(e.getBoundingClientRect().height * 10) / 10, width: Math.round(e.getBoundingClientRect().width * 10) / 10,
      borderRadius: s.borderRadius, padding: `${s.paddingTop} ${s.paddingRight} ${s.paddingBottom} ${s.paddingLeft}`,
      borderWidth: s.borderTopWidth, borderColor: s.borderTopColor, borderStyle: s.borderTopStyle, boxShadow: s.boxShadow, background: s.backgroundColor,
      fontSize: parseFloat(s.fontSize), fontWeight: Number(s.fontWeight), classes: c.slice(0, 200), box: box(e),
    });
  }
  // groups
  const groups = [];
  for (const e of roots.flatMap(r => r ? [...r.querySelectorAll("[role=tablist], [role=radiogroup], [role=group], [data-slot=toggle-group]")] : [])) {
    if (!visible(e)) continue;
    const s = getComputedStyle(e);
    groups.push({ role: e.getAttribute("role"), height: Math.round(e.getBoundingClientRect().height), width: Math.round(e.getBoundingClientRect().width), borderRadius: s.borderRadius, padding: s.padding, background: s.backgroundColor, border: `${s.borderTopWidth} ${s.borderTopStyle} ${s.borderTopColor}`, items: e.querySelectorAll("[role=tab], [role=radio], button").length, box: box(e) });
  }

  // --- cards / panels ---
  const cards = [];
  const transparent = v => v === "rgba(0, 0, 0, 0)" || v === "transparent";
  for (const e of all) {
    if (e === main) continue;
    if (e.matches(CONTROL_SEL) || e.closest("svg")) continue;
    const s = getComputedStyle(e);
    const rad = parseFloat(s.borderTopLeftRadius);
    if (!(rad > 0)) continue;
    const hasBorder = parseFloat(s.borderTopWidth) > 0 && s.borderTopStyle !== "none" && !transparent(s.borderTopColor) && parseFloat(s.borderBottomWidth) > 0;
    const hasBg = !transparent(s.backgroundColor) && s.backgroundColor !== pageBg;
    const hasShadow = s.boxShadow !== "none";
    if (!(hasBorder || hasBg || hasShadow)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 48 || r.height < 32) continue;
    if (!visible(e)) continue;
    cards.push({
      tag: e.tagName.toLowerCase(), slot: e.getAttribute("data-slot"), classes: cls(e).slice(0, 160),
      padding: `${s.paddingTop} ${s.paddingRight} ${s.paddingBottom} ${s.paddingLeft}`, borderRadius: s.borderRadius,
      border: hasBorder ? `${s.borderTopWidth} ${s.borderTopStyle} ${s.borderTopColor}` : "none", boxShadow: s.boxShadow, background: s.backgroundColor,
      hasBorder, hasBg, hasShadow, box: box(e), firstText: (e.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40),
    });
  }

  // --- rows ---
  const rowLists = [];
  const counted = new Set();
  const parents = new Map();
  for (const e of all) {
    if (!(e.matches("tr, li, [role=row]"))) continue;
    if (!visible(e)) continue;
    const p = e.parentElement;
    if (!parents.has(p)) parents.set(p, []);
    parents.get(p).push(e);
  }
  const listFrom = (kind, items, p) => {
    items.forEach(i => counted.add(i));
    const rs = items.map(i => i.getBoundingClientRect());
    const heights = rs.map(r => Math.round(r.height * 10) / 10);
    const rawGaps = rs.slice(1).map((r, i) => Math.round((r.top - rs[i].bottom) * 10) / 10);
    const sameRow = rawGaps.filter(g => g < -1).length; // grid/horizontal layouts
    // Group into visual rows by top edge so grids report row pitch, not item overlap.
    const rowsByTop = [];
    rs.forEach(r => { const g = rowsByTop.find(x => Math.abs(x.top - r.top) < 2); if (g) { g.bottom = Math.max(g.bottom, r.bottom); g.n++; } else rowsByTop.push({ top: r.top, bottom: r.bottom, n: 1 }); });
    rowsByTop.sort((a, b) => a.top - b.top);
    const visualRows = rowsByTop.length;
    const gaps = rowsByTop.slice(1).map((r, i) => Math.round((r.top - rowsByTop[i].bottom) * 10) / 10);
    const rowHeights = rowsByTop.map(r => Math.round((r.bottom - r.top) * 10) / 10);
    const columnGaps = sameRow ? rs.slice(1).map((r, i) => Math.abs(r.top - rs[i].top) < 2 ? Math.round((r.left - rs[i].right) * 10) / 10 : null).filter(v => v !== null) : [];
    rowLists.push({ kind, parent: `${p.tagName.toLowerCase()}.${cls(p).split(/\s+/).slice(0, 4).join(".")}`, count: items.length, visualRows, perRow: Math.max(...rowsByTop.map(r => r.n)), itemHeights: heights, heights: rowHeights, gaps, columnGaps, layout: sameRow ? (visualRows > 1 ? "grid" : "horizontal") : "vertical", inTablist: !!p.closest("[role=tablist], [role=radiogroup], [role=group]"),
      spanPx: Math.round(Math.max(...rs.map(r => r.bottom)) - Math.min(...rs.map(r => r.top))), sample: (items[0].innerText || "").replace(/\s+/g, " ").trim().slice(0, 40), box: box(p) });
  };
  for (const [p, items] of parents) if (items.length >= 2) listFrom(items[0].tagName.toLowerCase() === "tr" ? "table" : items[0].matches("li") ? "list" : "role-row", items, p);
  for (const p of all) {
    if (p.closest("svg")) continue;
    const kids = [...p.children].filter(k => !counted.has(k) && visible(k));
    if (kids.length < 3) continue;
    const sig = k => `${k.tagName}|${cls(k)}`;
    const by = new Map();
    kids.forEach(k => { const s = sig(k); by.set(s, [...(by.get(s) || []), k]); });
    for (const [s, items] of by) {
      if (items.length < 3) continue;
      const r0 = items[0].getBoundingClientRect();
      if (r0.height < 16) continue;
      if (items.every(i => i.matches("svg, span:empty"))) continue;
      listFrom("sibling-group", items, p);
    }
  }

  // --- layout ---
  let layout = null;
  if (scope === "main" && main) {
    const ms = getComputedStyle(main);
    const mr = main.getBoundingClientRect();
    const h1 = [...main.querySelectorAll("h1")].find(visible);
    const chain = [];
    let maxW = null, contentBox = null;
    for (let e = h1 || main.firstElementChild; e && e !== main.parentElement; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.maxWidth !== "none" && !maxW) { maxW = s.maxWidth; contentBox = e; }
    }
    // content left/right edges: union of visible direct-descendant content
    let container = main;
    while (container.children.length === 1 && container.firstElementChild) container = container.firstElementChild;
    const kids = [...container.children].filter(visible);
    const kr = kids.map(k => k.getBoundingClientRect());
    const left = kr.length ? Math.min(...kr.map(r => r.left)) : mr.left;
    const right = kr.length ? Math.max(...kr.map(r => r.right)) : mr.right;
    const sections = kids.map((k, i) => ({ tag: k.tagName.toLowerCase(), classes: cls(k).slice(0, 100), top: Math.round(kr[i].top + sy), height: Math.round(kr[i].height), sample: (k.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40) }));
    const gaps = kr.slice(1).map((r, i) => Math.round(r.top - kr[i].bottom));
    // Section gaps at any depth: vertically stacked siblings where a sibling holds a section heading.
    const stackGaps = [];
    for (const p of main.querySelectorAll("*")) {
      const ks = [...p.children].filter(k => visible(k) && k.getBoundingClientRect().height >= 20);
      if (ks.length < 2) continue;
      if (!ks.some(k => k.matches("h2, h3, header, section") || k.querySelector(":scope > h2, :scope > h3, :scope > header, :scope > div > h2, :scope > div > h3, :scope > header h2, :scope > header h3"))) continue;
      const rr = ks.map(k => k.getBoundingClientRect());
      for (let i = 1; i < ks.length; i++) {
        if (rr[i].top < rr[i - 1].bottom - 1) continue; // side by side
        const headed = x => x.matches("h2, h3, header, section") || x.querySelector("h2, h3");
        if (!headed(ks[i]) && !headed(ks[i - 1])) continue;
        if (ks[i].matches("h2, h3") || ks[i - 1].matches("h2, h3")) continue; // heading-to-content gap, not section gap
        stackGaps.push({ parent: `${p.tagName.toLowerCase()}.${cls(p).slice(0, 60)}`, gap: Math.round(rr[i].top - rr[i - 1].bottom), from: (ks[i - 1].innerText || "").replace(/\s+/g, " ").trim().slice(0, 30), to: (ks[i].innerText || "").replace(/\s+/g, " ").trim().slice(0, 30) });
      }
    }
    // Heading to first content gap
    const headingGaps = [...main.querySelectorAll("h2, h3")].filter(visible).map(h => { const n = h.nextElementSibling; let host = h; let nx = n; while (!nx && host.parentElement && host.parentElement !== main) { host = host.parentElement; nx = host.nextElementSibling; } return nx && visible(nx) ? { tag: h.tagName.toLowerCase(), text: h.innerText.trim().slice(0, 30), gap: Math.round(nx.getBoundingClientRect().top - host.getBoundingClientRect().bottom) } : null; }).filter(Boolean);
    const sidebar = document.querySelector("aside.shell-sidebar");
    const sbw = sidebar && visible(sidebar) ? sidebar.getBoundingClientRect().width : 0;
    layout = {
      viewport: { w: vw, h: vh }, pageHeight: document.documentElement.scrollHeight,
      sidebarWidth: Math.round(sbw),
      main: { x: Math.round(mr.left), w: Math.round(mr.width), padding: `${ms.paddingTop} ${ms.paddingRight} ${ms.paddingBottom} ${ms.paddingLeft}`, maxWidth: ms.maxWidth },
      contentMaxWidth: maxW, contentMaxWidthEl: contentBox ? `${contentBox.tagName.toLowerCase()}.${cls(contentBox).slice(0, 80)}` : null,
      contentLeftFromViewport: Math.round(left), contentRightFromViewport: Math.round(vw - right),
      contentLeftFromMainEdge: Math.round(left - mr.left), contentRightFromMainEdge: Math.round(mr.right - right),
      titleTop: h1 ? Math.round(h1.getBoundingClientRect().top + sy) : null,
      titleTopFromMain: h1 ? Math.round(h1.getBoundingClientRect().top - mr.top) : null,
      title: h1 ? { text: h1.innerText.trim().slice(0, 60), fontSize: parseFloat(getComputedStyle(h1).fontSize), fontWeight: Number(getComputedStyle(h1).fontWeight), lineHeight: parseFloat(getComputedStyle(h1).lineHeight), letterSpacing: getComputedStyle(h1).letterSpacing } : null,
      sectionContainer: `${container.tagName.toLowerCase()}.${cls(container).slice(0, 80)}`,
      sections, sectionGaps: gaps, stackGaps, headingGaps,
    };
  }

  // --- rows of the list template: ListRow and table body rows ---
  const listRows = [];
  for (const e of roots.flatMap(r => r ? [...r.querySelectorAll("[data-slot=list-row], [data-slot=table] > tbody > tr, [data-slot=setting-row]")] : [])) {
    if (!visible(e)) continue;
    listRows.push({ kind: e.getAttribute("data-slot") || "table-row", height: Math.round(e.getBoundingClientRect().height * 10) / 10, text: (e.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40), box: box(e) });
  }
  // --- regions outside the colour budget: media and charts (viewport px) ---
  const masks = [];
  for (const e of roots.flatMap(r => r ? [...r.querySelectorAll("img, video, canvas, picture, .recharts-wrapper, [data-chart], [data-slot=chart], [data-media]")] : [])) {
    if (!visible(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= vh) continue;
    masks.push({ x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), tag: e.tagName.toLowerCase() });
  }

  // --- svg ---
  const icons = {};
  for (const e of all) {
    if (e.tagName.toLowerCase() !== "svg" || !visible(e)) continue;
    const r = e.getBoundingClientRect();
    const k = `${Math.round(r.width)}x${Math.round(r.height)}`;
    icons[k] = (icons[k] || 0) + 1;
  }

  // --- colors ---
  const textColors = {}, bgColors = {};
  for (const t of text) textColors[t.color] = (textColors[t.color] || 0) + 1;
  for (const e of all) {
    const s = getComputedStyle(e);
    if (transparent(s.backgroundColor) || !visible(e)) continue;
    bgColors[s.backgroundColor] = (bgColors[s.backgroundColor] || 0) + 1;
  }
  return { scope, url: location.pathname, tokens, layout, text, controls, controlGroups: groups, cards, rowLists, listRows, masks, icons, textColors, bgColors };
}

const browser = await chromium.launch({ headless: true });
try {
  for (const width of widths) {
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 }, deviceScaleFactor: 1, colorScheme: "light", reducedMotion: "reduce", locale: "en-US", timezoneId: "America/Chicago" });
    await context.clock.setFixedTime(new Date(seed.now * 1000));
    await context.addInitScript(() => localStorage.setItem("theme", "light"));
    const lp = await context.newPage();
    await lp.goto(`${base}/ui/sign-in/`);
    await lp.getByLabel("Email", { exact: true }).fill(seed.owner_email);
    await lp.getByLabel("Password", { exact: true }).fill(seed.password);
    await lp.getByRole("button", { name: "Sign in", exact: true }).click();
    await lp.waitForURL(u => !u.pathname.includes("/sign-in"), { timeout: 15_000 });
    await lp.close();
    const page = await context.newPage();
    let shellDone = false;
    for (const [name, route] of surfaces) {
      console.log(`[measure] ${name} @${width}`);
      await page.goto(`${base}${route}`);
      await page.addStyleTag({ content: NO_ANIM });
      await settled(page);
      if (prepare[name]) await prepare[name](page);
      await page.evaluate(() => window.scrollTo(0, 0));
      const data = await page.evaluate(measure, "main");
      data.surface = name; data.width = width; data.actualRoute = new URL(page.url()).pathname;
      data.bodyTextLength = (await page.locator("#main-content").innerText()).length;
      await writeFile(path.join(outDir, `${name}-${width}.json`), JSON.stringify(data, null, 1));
      if (!shellDone) {
        const shell = await page.evaluate(measure, "shell");
        shell.width = width;
        await writeFile(path.join(outDir, `shell-${width}.json`), JSON.stringify(shell, null, 1));
        shellDone = true;
      }
      if (shots !== "none") await page.screenshot({ path: path.join(outDir, `${name}-${width}.png`), fullPage: false, animations: "disabled" });
      if (shots === "all") await page.screenshot({ path: path.join(outDir, `${name}-${width}-full.png`), fullPage: true, animations: "disabled" });
    }
    await context.close();
  }
} finally {
  await browser.close();
}
