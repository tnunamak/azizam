// Quick design-loop screenshots against a running household server.
// Usage: node e2e/d1-shots.mjs <outdir> <spec...>   spec = role:path:width:theme[:name]
// Env: D1_BASE (default http://127.0.0.1:18431), D1_FULL=1 for full-page.
import { chromium } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.D1_BASE ?? "http://127.0.0.1:18431";
const manifest = JSON.parse(await readFile(process.env.D1_MANIFEST ?? `${process.env.HOME}/.tmp/azizam-redesign/d1-work/manifest-18431.json`, "utf8"));
const [outdir, ...specs] = process.argv.slice(2);
await mkdir(outdir, { recursive: true });
const browser = await chromium.launch();
const contexts = new Map();
async function contextFor(role, width, theme) {
  const key = `${role}-${width}-${theme}`;
  if (contexts.has(key)) return contexts.get(key);
  const context = await browser.newContext({ viewport: { width: Number(width), height: Number(width) === 390 ? 844 : 1000 }, colorScheme: theme, reducedMotion: "reduce", locale: "en-US", timezoneId: "America/Chicago" });
  await context.clock.setFixedTime(new Date(manifest.now * 1000));
  await context.addInitScript(value => localStorage.setItem("theme", value), theme);
  const email = role === "owner" ? manifest.owner_email : role === "restricted" ? manifest.restricted_email : manifest.member_email;
  // Reuse one sign-in per role and fixture run; the gateway rate-limits logins.
  const stateFile = `${process.env.HOME}/.tmp/azizam-redesign/d1-work/cookies-${role}-${manifest.fixture_state}.json`;
  try {
    const cookies = JSON.parse(await readFile(stateFile, "utf8"));
    await context.addCookies(cookies);
    contexts.set(key, context);
    return context;
  } catch {}
  const page = await context.newPage();
  await page.goto(`${base}/ui/sign-in/`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(manifest.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(url => !url.pathname.includes("/sign-in"), { timeout: 60000 });
  await page.close();
  const { writeFile } = await import("node:fs/promises");
  await writeFile(stateFile, JSON.stringify(await context.cookies()));
  contexts.set(key, context);
  return context;
}
for (const spec of specs) {
  const [role, route, width, theme, name, action] = spec.split(":");
  const context = await contextFor(role, width, theme);
  const page = await context.newPage();
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`${base}/ui${route}`);
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);
  if (action) {
    for (const step of action.split(",")) {
      const [kind, ...rest] = step.split("=");
      const arg = rest.join("=");
      if (kind === "click") await page.getByRole("button", { name: new RegExp(arg) }).first().click();
      if (kind === "clicklink") await page.getByRole("link", { name: new RegExp(arg) }).first().click();
      if (kind === "wait") await page.waitForTimeout(Number(arg));
      if (kind === "press") await page.keyboard.press(arg);
    }
    await page.waitForTimeout(600);
  }
  const file = path.join(outdir, `${name ?? `${role}${route.replaceAll("/", "_")}`}-${width}-${theme}.png`);
  await page.screenshot({ path: file, fullPage: process.env.D1_FULL === "1" });
  console.log(file, errors.length ? `console errors: ${errors.slice(0, 3).join(" | ")}` : "");
  await page.close();
}
await browser.close();
