// Viewport captures for a before/after contact sheet: 8 surfaces x 2 widths x 2 themes.
// Usage: node contact.mjs <manifest.json> <outdir>   (household server must already be running)
// Writes <surface>-<width>-<theme>.jpg (quality 80). Compose with contact_sheet.py.
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(path.resolve(here, "../../../../../web"), "package.json"));
const { chromium } = require("@playwright/test");

const [manifestPath, outDir] = process.argv.slice(2);
const seed = JSON.parse(await readFile(manifestPath, "utf8"));
await mkdir(outDir, { recursive: true });

const surfaces = [
  ["home", "/ui/home/"], ["status", "/ui/status/"], ["library", "/ui/library/"],
  ["try", "/ui/try/"], ["people", "/ui/people/"], ["usage", "/ui/usage/"],
  ["workflows", "/ui/workflows/"], ["settings", "/ui/settings/"],
];
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
  await page.waitForTimeout(400);
}

const browser = await chromium.launch({ headless: true });
try {
  // Sign in once (the gateway limits sign-in attempts) and reuse the cookies.
  const signIn = await browser.newContext();
  const lp = await signIn.newPage();
  await lp.goto(`${seed.base_url}/ui/sign-in/`);
  await lp.getByLabel("Email", { exact: true }).fill(seed.owner_email);
  await lp.getByLabel("Password", { exact: true }).fill(seed.password);
  await lp.getByRole("button", { name: "Sign in", exact: true }).click();
  await lp.waitForURL(u => !u.pathname.includes("/sign-in"), { timeout: 15_000 });
  const cookies = await signIn.cookies();
  await signIn.close();
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 }, deviceScaleFactor: 1, colorScheme: theme, reducedMotion: "reduce", locale: "en-US", timezoneId: "America/Chicago" });
      await context.clock.setFixedTime(new Date(seed.now * 1000));
      await context.addInitScript(value => localStorage.setItem("theme", value), theme);
      await context.addCookies(cookies);
      const page = await context.newPage();
      for (const [name, route] of surfaces) {
        console.log(`[contact] ${name} @${width} ${theme}`);
        await page.goto(`${seed.base_url}${route}`);
        await page.addStyleTag({ content: NO_ANIM });
        await settled(page);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: path.join(outDir, `${name}-${width}-${theme}.jpg`), type: "jpeg", quality: 80, animations: "disabled" });
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
}
