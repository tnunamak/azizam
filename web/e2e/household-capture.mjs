import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(web, "..");
const port = Number(process.env.UI_E2E_PORT ?? "18180");
if (!Number.isInteger(port) || port < 18000 || port > 18998)
  throw new Error("UI_E2E_PORT must be an isolated port from 18000 to 18998");
const pythonPath = path.resolve(repo, "venv/bin/python");
const python = process.env.UI_E2E_PYTHON ?? (existsSync(pythonPath) ? pythonPath : "python3");
const stateRoot = path.resolve(repo, ".ui-e2e-state");
const manifestPath = path.join(stateRoot, `manifest-${port}.json`);
const outputArg = process.argv[2];
if (process.argv.length > 3) throw new Error("Usage: scripts/ui/design-review-capture [output-directory]");
const output = path.resolve(outputArg ?? process.env.UI_CAPTURE_DIR ?? path.join(os.homedir(), ".tmp/gw-product/ui-review/household"));
const scenarios = ["default", "running", "budget-exhausted", "provider-down", "stale-health", "new-member"];
const scenario = process.env.UI_HOUSEHOLD_SCENARIO ?? "default";
if (!scenarios.includes(scenario)) throw new Error(`UI_HOUSEHOLD_SCENARIO must be one of: ${scenarios.join(", ")}`);
// Browser locale and zone are fixed so relative and absolute dates render the same on every run.
const browserLocale = "en-US";
const browserTimezone = "America/Chicago";
const baseURL = `http://127.0.0.1:${port}`;
const routes = [
  ["home", "/ui/home/"], ["apps", "/ui/apps/"], ["models", "/ui/models/"],
  ["workflows", "/ui/workflows/"], ["try", "/ui/try/"], ["library", "/ui/library/"],
  ["usage", "/ui/usage/"], ["status", "/ui/status/"], ["engines", "/ui/engines/"],
  ["providers", "/ui/providers/"], ["people", "/ui/people/"],
  ["activity", "/ui/activity/"], ["settings", "/ui/settings/"],
];
const captures = [];
const skipped = [];
const mediaChecks = [];

try {
  await fetch(`${baseURL}/health`, { signal: AbortSignal.timeout(1_000) });
  throw new Error(`Port ${port} is already serving a process`);
} catch (error) {
  if (error.message?.includes("already serving")) throw error;
}
await mkdir(stateRoot, { recursive: true });
await rm(manifestPath, { force: true });
await mkdir(output, { recursive: true });
if ((await readdir(output)).length)
  throw new Error(`Output directory must be empty before capture: ${output}`);
const startServer = () => spawn(python, ["../scripts/ui/serve.py"], {
  cwd: web,
  env: { ...process.env, UI_E2E_PORT: String(port), UI_SEED_PROFILE: "household", UI_HOUSEHOLD_MANIFEST_PATH: manifestPath, UI_HOUSEHOLD_SCENARIO: scenario },
  stdio: "inherit",
});
let server = startServer();
const seenFixtureStates = new Set();
let browser;
let stopping = false;
const stop = () => {
  if (stopping) return;
  stopping = true;
  server.kill("SIGTERM");
};
process.once("SIGINT", stop);
process.once("SIGTERM", stop);

async function readySeed() {
  const readyUntil = Date.now() + 120_000;
  let ready = false;
  while (Date.now() < readyUntil) {
    if (server.exitCode !== null) throw new Error("Isolated household supervisor exited early");
    try {
      const health = await fetch(`${baseURL}/health`, { signal: AbortSignal.timeout(1_500) });
      if (health.ok && existsSync(manifestPath)) { ready = true; break; }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!ready) throw new Error("Household supervisor readiness timed out");
  const seed = JSON.parse(await readFile(manifestPath, "utf8"));
  if (seed.scenario !== scenario) throw new Error(`Seeded scenario ${seed.scenario} does not match ${scenario}`);
  if (!seed.owner_email || !seed.member_email || !seed.password || !seed.base_url)
    throw new Error("Household manifest is missing owner/member credentials or base_url");
  if (!seed.fixture_state || seenFixtureStates.has(seed.fixture_state))
    throw new Error("Try capture requires a fresh disposable fixture state");
  seenFixtureStates.add(seed.fixture_state);
  return seed;
}

async function stopServer() {
  const child = server;
  if (child.exitCode !== null || child.signalCode !== null)
    throw new Error("Household supervisor stopped unexpectedly");
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("Household supervisor did not cleanly stop; refusing to reuse state"));
    }, 20_000);
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      if (code === 0 && signal === null) resolve();
      else reject(new Error("Household supervisor did not cleanly stop; refusing to reuse state"));
    });
    child.kill("SIGTERM");
  });
}

async function settled(page) {
  await page.waitForLoadState("networkidle", { timeout: 30_000 });
  await page.waitForFunction(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden" && element.getBoundingClientRect().width > 0;
    };
    return [...document.querySelectorAll("h1, [role=dialog] h2")].some(visible);
  }, null, { timeout: 15_000 });
  try {
    await page.waitForFunction(() => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && element.getBoundingClientRect().width > 0;
      };
      return ![...document.querySelectorAll('[aria-busy="true"], [data-slot="skeleton"]')].some(visible);
    }, null, { timeout: 20_000 });
  } catch {
    const pending = await page.locator('[aria-busy="true"]:visible, [data-slot="skeleton"]:visible').evaluateAll(nodes => nodes.map(node => `${node.getAttribute("aria-label") ?? node.tagName}: ${node.textContent?.trim().slice(0, 100)}`));
    throw new Error(`Page did not settle at ${page.url()}: ${pending.join("; ")}`);
  }
  await page.evaluate(async () => {
    await document.fonts.ready;
    const images = Array.from(document.images);
    images.forEach(image => { image.loading = "eager"; });
    await Promise.all(images.map(image => image.decode().catch(() => {})));
  });
  const broken = await page.evaluate(() => Array.from(document.images).filter(image => !image.complete || image.naturalWidth === 0).map(image => image.currentSrc || image.src));
  if (broken.length) throw new Error(`Images failed to decode at ${page.url()}: ${broken.join(", ")}`);
  await page.waitForTimeout(350);
}

async function login(context, account, base) {
  const page = await context.newPage();
  await page.goto(`${base}/ui/sign-in/`);
  await page.getByLabel("Email", { exact: true }).fill(account.email);
  await page.getByLabel("Password", { exact: true }).fill(account.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(url => !url.pathname.includes("/sign-in"), { timeout: 15_000 });
  await page.close();
}

async function prepareLibraryMedia(page, entry) {
  const play = page.getByRole("button", { name: /^Play / }).first();
  if (await play.count()) {
    await play.scrollIntoViewIfNeeded();
    await play.click();
    const audio = page.locator("article audio").first();
    await page.waitForFunction(() => [...document.querySelectorAll("article audio")].some(item => Number.isFinite(item.duration) && item.duration > 0), null, { timeout: 20_000 });
    const duration = await audio.evaluate(item => item.duration);
    const pause = page.getByRole("button", { name: /^Pause / }).first();
    await pause.click();
    await audio.evaluate(item => { item.pause(); item.currentTime = 0; });
    mediaChecks.push({ view: entry.view, route: "/ui/library/", media: "audio", result: "decoded-and-played", duration_seconds: duration });
  } else {
    mediaChecks.push({ view: entry.view, route: "/ui/library/", media: "audio", result: "unavailable" });
  }

  const video = page.locator("article video").first();
  if (await video.count()) {
    await video.scrollIntoViewIfNeeded();
    const handle = await video.elementHandle();
    await page.waitForFunction(item => item.readyState >= 2 && Number.isFinite(item.duration) && item.duration > 0, handle, { timeout: 20_000 });
    // Wait for the browser to present the first frame, not a fixed delay.
    const duration = await video.evaluate(item => new Promise(resolve => {
      item.pause();
      if (item.currentTime === 0 && !item.seeking) return requestAnimationFrame(() => resolve(item.duration));
      item.addEventListener("seeked", () => requestAnimationFrame(() => resolve(item.duration)), { once: true });
      item.currentTime = 0;
    }));
    await page.evaluate(() => window.scrollTo(0, 0));
    mediaChecks.push({ view: entry.view, route: "/ui/library/", media: "video", result: "frame-decoded-and-paused", duration_seconds: duration });
  } else {
    mediaChecks.push({ view: entry.view, route: "/ui/library/", media: "video", result: "unavailable" });
  }
}

async function capture(page, entry) {
  await settled(page);
  if (entry.route === "library" && !entry.interaction) {
    await prepareLibraryMedia(page, entry);
    await settled(page);
  }
  const actualRoute = new URL(page.url()).pathname;
  const safe = [scenario === "default" ? null : scenario, entry.view, entry.route, actualRoute, entry.interaction ?? "page", entry.theme, entry.width, entry.state]
    .filter(value => value !== null).map(value => String(value).replace(/[^a-zA-Z0-9_-]+/g, "-")).join("_");
  const file = `${safe}.png`;
  const textFile = `${safe}.txt`;
  await writeFile(path.join(output, textFile), `${await page.locator("body").innerText()}\n`);
  await page.screenshot({ path: path.join(output, file), fullPage: true, animations: "disabled" });
  captures.push({ ...entry, requested_route: entry.route, route: actualRoute, file, text_file: textFile });
}

try {
  await mkdir(output, { recursive: true });
  const seed = await readySeed();
  const base = seed.base_url;
  browser = await chromium.launch({ headless: true });
  for (const [view, email] of [["owner", seed.owner_email], ["member", seed.member_email]]) {
    for (const theme of ["light", "dark"]) {
      for (const width of [1440, 390]) {
        const context = await newContext(seed, { width, theme });
        await login(context, { email, password: seed.password }, base);
        const page = await context.newPage();
        for (const [route, url] of routes) {
          const failures = [];
          const collect = response => {
            if (response.url().includes("/admin/api/") && [401, 403].includes(response.status())) failures.push(response.status());
          };
          page.on("response", collect);
          await page.goto(`${base}${url}`);
          await settled(page);
          page.off("response", collect);
          const actualRoute = new URL(page.url()).pathname;
          const state = failures.includes(403)
            ? "restricted-403"
            : failures.includes(401)
              ? "restricted-401"
              : actualRoute !== new URL(`${base}${url}`).pathname
                ? "redirected"
                : "settled";
          await capture(page, { route, view, theme, width, state });
          if (route === "people") await capturePeopleViews(page, { view, theme, width });
          if (route === "activity") await captureActivityViews(page, { view, theme, width });
        }
        if (view === "owner") await captureInteractions(page, { view, theme, width, base });
        await context.close();
      }
    }
  }
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) await captureTryResult({ theme, width });
  }
  const report = { scenario, generated_at: new Date(seed.now * 1000).toISOString(), timezone: browserTimezone, locale: browserLocale, base_url: base, captures, skipped_states: skipped, media_checks: mediaChecks };
  await writeFile(path.join(output, "manifest.json"), `${JSON.stringify(report, null, 2)}\n`);
  const ownerSkips = skipped.filter(item => item.view === "owner");
  if (ownerSkips.length) throw new Error(`Required owner capture states were unavailable; see ${path.join(output, "manifest.json")}`);
  console.log(`[capture] Wrote ${captures.length} screenshots and manifest to ${output}`);
} finally {
  await browser?.close();
  stop();
  await new Promise(resolve => {
    if (server.exitCode !== null) return resolve();
    server.once("exit", resolve);
    setTimeout(() => { server.kill("SIGKILL"); resolve(); }, 10_000).unref();
  });
}

async function captureInteractions(page, { view, theme, width, base }) {
  const shot = async (route, interaction) => capture(page, { route, view, interaction, theme, width, state: "settled" });
  const skip = (route, interaction, reason) => skipped.push({ route, view, interaction, theme, width, reason });
  await page.goto(`${base}/ui/status/`);
  await settled(page);
  const showDisabled = page.getByRole("switch", { name: "Show disabled" });
  if (await showDisabled.count()) {
    if (await showDisabled.getAttribute("aria-checked") !== "true") await showDisabled.click();
    await shot("status", "show-disabled");
  } else skip("status", "show-disabled", "Show disabled control was not rendered");
  const alert = page.locator("section").filter({ has: page.getByRole("heading", { name: "Needs attention" }) }).getByRole("link", { name: "Open" }).first();
  if (await alert.count()) { await alert.click(); await shot("status", "alert"); }
  else {
    await shot("status", "alert-state");
    skip("status", "alert", "No Needs attention alert with an Open action was rendered");
  }

  await page.goto(`${base}/ui/engines/`);
  await settled(page);
  const showNotInUse = page.getByRole("switch", { name: "Show not in use" });
  if (await showNotInUse.count()) {
    if (await showNotInUse.getAttribute("aria-checked") !== "true") await showNotInUse.click();
    await shot("engines", "show-not-in-use");
  } else skip("engines", "show-not-in-use", "Show not in use control was not rendered");

  await page.goto(`${base}/ui/apps/`);
  await settled(page);
  const newApp = page.getByRole("button", { name: "New app", exact: true }).first();
  if (await newApp.count()) { await newApp.click(); await shot("apps", "new-app"); }
  else skip("apps", "new-app", "New app control was not rendered");

  await page.goto(`${base}/ui/people/?tab=invites`);
  await settled(page);
  const invite = page.getByRole("button", { name: /invite someone/i }).first();
  if (await invite.count()) { await invite.click(); await shot("people", "invite"); }
  else skip("people", "invite", "Invite someone control was not rendered");

  await page.goto(`${base}/ui/library/`);
  await settled(page);
  const detail = page.getByRole("button", { name: /^Open / }).first();
  if (await detail.count()) { await detail.click(); await shot("library", "detail"); }
  else skip("library", "detail", "No Library asset was rendered");
}

async function newContext(seed, { width, theme }) {
  const context = await browser.newContext({
    viewport: { width, height: width === 390 ? 844 : 1000 }, colorScheme: theme, reducedMotion: "reduce",
    locale: browserLocale, timezoneId: browserTimezone,
  });
  // Freeze Date at the seeded instant; timers and animations keep running.
  await context.clock.setFixedTime(new Date(seed.now * 1000));
  await context.addInitScript(value => localStorage.setItem("theme", value), theme);
  return context;
}

// Running a request changes later pages (Activity, Usage, Home), so Try runs after every other capture.
async function captureTryResult({ theme, width }) {
  await stopServer();
  if (stopping) throw new Error("Household capture was interrupted");
  await rm(manifestPath, { force: true });
  server = startServer();
  const seed = await readySeed();
  const context = await newContext(seed, { width, theme });
  await login(context, { email: seed.owner_email, password: seed.password }, seed.base_url);
  const page = await context.newPage();
  await page.goto(`${seed.base_url}/ui/try/`);
  await settled(page);
  const run = page.getByRole("button", { name: "Run", exact: true }).first();
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll("button")].find(item => item.textContent?.trim() === "Run");
    return button && !button.disabled;
  }, null, { timeout: 20_000 }).catch(() => {});
  if (!(await run.count()) || !(await run.isEnabled())) throw new Error("Try result capture could not start because Run stayed unavailable");
  await run.click();
  await page.getByRole("tabpanel").locator('section[aria-live="polite"] h2').waitFor({ state: "visible", timeout: 30_000 });
  await capture(page, { route: "try", view: "owner", interaction: "result", theme, width, state: "settled" });
  await context.close();
}

async function capturePeopleViews(page, { view, theme, width }) {
  for (const tab of ["Invites", "Access plans"]) {
    const control = page.getByRole("tab", { name: tab, exact: true });
    if (!(await control.count())) {
      skipped.push({ route: "/ui/people/", view, interaction: `tab-${tab.toLowerCase().replaceAll(" ", "-")}`, theme, width, state: "unavailable", reason: "Tab was not rendered for this viewer" });
      continue;
    }
    await control.click();
    await capture(page, { route: "people", view, interaction: `tab-${tab.toLowerCase().replaceAll(" ", "-")}`, theme, width, state: "settled" });
  }
}

async function captureActivityViews(page, { view, theme, width }) {
  for (const tab of ["Changes", "Requests"]) {
    const control = page.getByRole("tab", { name: tab, exact: true });
    if (!(await control.count())) {
      skipped.push({ route: "/ui/activity/", view, interaction: `tab-${tab.toLowerCase()}`, theme, width, state: "unavailable", reason: "Tab was not rendered for this viewer" });
      continue;
    }
    await control.click();
    await capture(page, { route: "activity", view, interaction: `tab-${tab.toLowerCase()}`, theme, width, state: "settled" });
    if (tab === "Requests") {
      const receipts = page.getByRole("button", { name: /^Request details:/ });
      const failure = receipts.filter({ hasText: "Failed" }).first();
      const isFailure = (await failure.count()) > 0;
      const receipt = isFailure ? failure : receipts.first();
      if (await receipt.count()) {
        await receipt.click();
        await capture(page, { route: "activity", view, interaction: isFailure ? "failure-receipt" : "receipt", theme, width, state: "settled" });
        if (!isFailure) skipped.push({ route: "activity", view, interaction: "failure-receipt", theme, width, reason: "No failed Activity receipt was rendered" });
      } else skipped.push({ route: "activity", view, interaction: "receipt", theme, width, reason: "No request receipt row was rendered" });
    }
  }
}
