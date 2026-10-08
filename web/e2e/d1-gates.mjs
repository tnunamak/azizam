// T-4 mechanical gates for Home, Status and Library against a running household server.
// Usage: node e2e/d1-gates.mjs <outdir>   Env: D1_BASE, D1_MANIFEST (see d1-shots.mjs).
// Checks per role x width x theme x surface (and the Library viewer):
//   axe WCAG 2 AA (incl. colour contrast), horizontal overflow, clipped text,
//   touch targets >= 44 px at 390, visible focus on every tab stop, console errors.
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.D1_BASE ?? "http://127.0.0.1:18431";
const manifest = JSON.parse(await readFile(process.env.D1_MANIFEST ?? `${process.env.HOME}/.tmp/azizam-redesign/d1-work/manifest-18431.json`, "utf8"));
const outdir = process.argv[2];
await mkdir(outdir, { recursive: true });
const browser = await chromium.launch();
const results = [];

async function context(role, width, theme) {
  const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, colorScheme: theme, reducedMotion: "reduce", locale: "en-US", timezoneId: "America/Chicago" });
  await ctx.clock.setFixedTime(new Date(manifest.now * 1000));
  await ctx.addInitScript(value => localStorage.setItem("theme", value), theme);
  const stateFile = `${process.env.HOME}/.tmp/azizam-redesign/d1-work/cookies-${role}-${manifest.fixture_state}.json`;
  try {
    await ctx.addCookies(JSON.parse(await readFile(stateFile, "utf8")));
  } catch {
    const page = await ctx.newPage();
    await page.goto(`${base}/ui/sign-in/`);
    await page.getByLabel("Email", { exact: true }).fill(role === "owner" ? manifest.owner_email : manifest.member_email);
    await page.getByLabel("Password", { exact: true }).fill(manifest.password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.waitForURL(url => !url.pathname.includes("/sign-in"), { timeout: 60000 });
    await page.close();
    await writeFile(stateFile, JSON.stringify(await ctx.cookies()));
  }
  return ctx;
}

async function inspect(page, label, width) {
  const errors = [];
  const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const violations = axe.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.slice(0, 4).map(n => `${n.target.join(" ")} :: ${n.failureSummary?.split("\n")[1]?.trim() ?? ""}`) }));
  const layout = await page.evaluate(({ width }) => {
    const out = { overflowX: document.documentElement.scrollWidth > window.innerWidth + 1, clipped: [], smallTargets: [] };
    for (const el of document.querySelectorAll("main *, header *, nav *, [role=dialog] *")) {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") continue;
      const rect = el.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      const clipsX = ["hidden", "clip"].includes(style.overflowX) && el.scrollWidth > el.clientWidth + 1;
      const ellipsis = style.textOverflow === "ellipsis";
      const clamp = style.webkitLineClamp && style.webkitLineClamp !== "none";
      const textual = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
      const visuallyHidden = style.clip !== "auto" && style.position === "absolute";
      if (clipsX && textual && !ellipsis && !clamp && !visuallyHidden && !el.closest(".sr-only"))
        out.clipped.push(`${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 3).join(".")}: ${el.textContent.trim().slice(0, 40)}`);
      if (width <= 390) {
        const interactive = el.matches("a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=switch], [role=tab], [role=combobox], [role=slider]");
        if (interactive && !el.closest(".sr-only") && !el.disabled && (rect.height < 44 || rect.width < 44)) {
          // Inline text links inside sentences are exempt (WCAG 2.5.8 inline exception).
          const inline = el.tagName === "A" && getComputedStyle(el).display === "inline";
          if (!inline) out.smallTargets.push(`${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 30)}" ${Math.round(rect.width)}x${Math.round(rect.height)}`);
        }
      }
    }
    return out;
  }, { width });
  // Focus: every tab stop shows an outline or a ring.
  const focusless = [];
  await page.evaluate(() => document.activeElement?.blur());
  for (let i = 0; i < 60; i += 1) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      const s = getComputedStyle(el);
      const visible = (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) >= 1) || (s.boxShadow && s.boxShadow !== "none");
      const r = el.getBoundingClientRect();
      return { visible, name: `${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 30)}"`, onscreen: r.width > 0 && r.height > 0 };
    });
    if (!info) break;
    if (info.onscreen && !info.visible) focusless.push(info.name);
  }
  return { label, violations, ...layout, focusless: [...new Set(focusless)], errors };
}

for (const role of ["owner", "member"]) {
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) {
      const ctx = await context(role, width, theme);
      for (const [surface, route] of [["home", "/ui/home/"], ["status", "/ui/status/"], ["library", "/ui/library/"]]) {
        const page = await ctx.newPage();
        const consoleErrors = [];
        page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
        page.on("response", r => { if (r.status() >= 400 && r.url().startsWith(base)) consoleErrors.push(`${r.status()} ${r.url().replace(base, "")}`); });
        await page.goto(`${base}${route}`);
        await page.waitForLoadState("networkidle");
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(400);
        const result = await inspect(page, `${role} ${surface} ${theme} ${width}`, width);
        result.errors = consoleErrors;
        results.push(result);
        if (surface === "library") {
          const open = page.getByRole("button", { name: /^Open / }).first();
          if (await open.count()) {
            await open.click();
            await page.waitForTimeout(800);
            const viewer = await inspect(page, `${role} library-viewer ${theme} ${width}`, width);
            viewer.errors = [];
            results.push(viewer);
          }
        }
        await page.close();
      }
      await ctx.close();
    }
  }
}
await browser.close();
await writeFile(path.join(outdir, "gates.json"), JSON.stringify(results, null, 2));
const summary = results.map(r => `${r.label}: axe ${r.violations.length} [${r.violations.map(v => v.id).join(",")}] overflowX ${r.overflowX} clipped ${r.clipped.length} smallTargets ${r.smallTargets.length} focusless ${r.focusless.length} errors ${r.errors.length}`);
await writeFile(path.join(outdir, "gates.txt"), summary.join("\n") + "\n");
console.log(summary.join("\n"));
