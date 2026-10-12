// Save static, script-free snapshots of signed-in pages so a URL-only scanner
// (the Impeccable detector) can read the rendered product. Usage:
// node e2e/d1-snapshots.mjs <outdir>   Env: D1_BASE, D1_MANIFEST.
import { chromium } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
const base = process.env.D1_BASE ?? "http://127.0.0.1:18431";
const manifest = JSON.parse(await readFile(process.env.D1_MANIFEST ?? `${process.env.HOME}/.tmp/azizam-redesign/d1-work/manifest-18431.json`, "utf8"));
const outdir = process.argv[2];
await mkdir(outdir, { recursive: true });
const browser = await chromium.launch();
for (const role of ["owner", "member"]) for (const theme of ["light", "dark"]) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: theme });
  await ctx.clock.setFixedTime(new Date(manifest.now * 1000));
  await ctx.addInitScript(value => localStorage.setItem("theme", value), theme);
  await ctx.addCookies(JSON.parse(await readFile(`${process.env.HOME}/.tmp/azizam-redesign/d1-work/cookies-${role}-${manifest.fixture_state}.json`, "utf8")));
  for (const [name, route, open] of [["home", "/"], ["status", "/status/"], ["library", "/library/"], ["viewer", "/library/", true]]) {
    const page = await ctx.newPage();
    await page.goto(`${base}${route}`);
    await page.waitForLoadState("networkidle");
    if (open) { await page.getByRole("button", { name: /^Open / }).first().click(); await page.waitForTimeout(800); }
    let html = await page.content();
    html = html.replace(/<script[\s\S]*?<\/script>/g, "").replace(/(href|src)="\//g, `$1="${base}/`);
    await writeFile(`${outdir}/${role}-${name}-${theme}.html`, html);
    await page.close();
  }
  await ctx.close();
}
await browser.close();
console.log("saved");
