// State-matrix screenshots for the Home, Status and Library surfaces.
// Usage (from web/): UI_E2E_PORT=<port> UI_HOUSEHOLD_SCENARIO=<scenario> node e2e/d1-states-capture.mjs <outdir> [--states=all|populated]
// Boots its own fixture server, never fails the run because one capture failed, and writes <outdir>/manifest.json.
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(web, "..");
const port = Number(process.env.UI_E2E_PORT ?? "18441");
if (!Number.isInteger(port) || port < 18000 || port > 18998 || [18474, 18431].includes(port))
  throw new Error("UI_E2E_PORT must be an isolated port from 18000 to 18998 (not 18431 or 18474)");
const pythonPath = path.resolve(repo, "venv/bin/python");
const python = process.env.UI_E2E_PYTHON ?? (existsSync(pythonPath) ? pythonPath : "python3");
const stateRoot = path.resolve(repo, ".ui-e2e-state");
const manifestPath = path.join(stateRoot, `manifest-${port}.json`);
const scenarios = ["default", "running", "budget-exhausted", "provider-down", "stale-health", "new-member"];
const scenario = process.env.UI_HOUSEHOLD_SCENARIO ?? "default";
if (!scenarios.includes(scenario)) throw new Error(`UI_HOUSEHOLD_SCENARIO must be one of: ${scenarios.join(", ")}`);

const args = process.argv.slice(2);
const statesArg = args.find(item => item.startsWith("--states="))?.slice("--states=".length) ?? "all";
const positional = args.filter(item => !item.startsWith("--"));
if (!["all", "populated"].includes(statesArg) || positional.length !== 1)
  throw new Error("Usage: node e2e/d1-states-capture.mjs <outdir> [--states=all|populated]");
const output = path.resolve(positional[0]);
const allStates = statesArg === "all";

const browserLocale = "en-US";
const browserTimezone = "America/Chicago";
const baseURL = `http://127.0.0.1:${port}`;
const surfaces = [["home", "/"], ["status", "/status/"], ["library", "/library/"]];
const trySurfaces = [["try-image", "/try/?capability=image"], ["try-speech", "/try/?capability=speech"]];
const widths = [1440, 390];
const themes = ["light", "dark"];
const OUTAGE_BODY = { error: { code: "internal_error", message: "The gateway could not load this data. Refresh to try again." } };
const INJECTED_TEXT = /could not load this data|internal_error|status of 500/;

const captures = [];

// ---------------------------------------------------------------- server

try {
  await fetch(`${baseURL}/health`, { signal: AbortSignal.timeout(1_000) });
  throw new Error(`Port ${port} is already serving a process`);
} catch (error) {
  if (error.message?.includes("already serving")) throw error;
}
await mkdir(stateRoot, { recursive: true });
await rm(manifestPath, { force: true });
await mkdir(output, { recursive: true });

const server = spawn(python, ["../scripts/ui/serve.py"], {
  cwd: web,
  env: { ...process.env, UI_E2E_PORT: String(port), UI_SEED_PROFILE: "household", UI_HOUSEHOLD_MANIFEST_PATH: manifestPath, UI_HOUSEHOLD_SCENARIO: scenario },
  stdio: "inherit",
});
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
  return seed;
}

// ---------------------------------------------------------------- page helpers

const headingVisible = () => {
  const visible = element => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
  };
  return [...document.querySelectorAll("h1, [role=dialog] h2, [role=status]")]
    .some(element => visible(element) && (element.matches("[role=status]") ? Boolean(element.textContent?.trim()) : true));
};
const skeletonsGone = () => {
  const visible = element => {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getBoundingClientRect().width > 0;
  };
  return ![...document.querySelectorAll('[aria-busy="true"], [data-slot="skeleton"]')].some(visible);
};

// Wait for the page to look finished. Never throws: problems are returned as notes.
async function settle(page, { cap = 30_000 } = {}) {
  const notes = [];
  const limit = ms => Math.min(ms, cap);
  await page.waitForLoadState("networkidle", { timeout: limit(30_000) }).catch(() => notes.push("network did not go idle"));
  const heading = await page.waitForFunction(headingVisible, null, { timeout: limit(15_000) }).then(() => true, () => false);
  if (!heading) notes.push("no visible h1, dialog h2 or [role=status]");
  const quiet = await page.waitForFunction(skeletonsGone, null, { timeout: limit(20_000) }).then(() => true, () => false);
  if (!quiet) notes.push("skeleton or aria-busy element still visible");
  try {
    await page.evaluate(async () => {
      await document.fonts.ready;
      const images = Array.from(document.images);
      images.forEach(image => { image.loading = "eager"; });
      await Promise.all(images.map(image => image.decode().catch(() => {})));
    });
    const broken = await page.evaluate(() => Array.from(document.images).filter(image => !image.complete || image.naturalWidth === 0).map(image => image.currentSrc || image.src));
    if (broken.length) notes.push(`images failed to decode: ${broken.join(", ")}`);
  } catch (error) {
    notes.push(`image check failed: ${error.message.split("\n")[0]}`);
  }
  await page.waitForTimeout(350);
  return notes;
}

// Per-page recorder of console errors and failed same-origin requests.
function attachRecorder(page, origin) {
  const entries = [];
  page.on("console", message => {
    if (message.type() === "error") entries.push({ text: `console: ${message.text()}`, url: message.location().url ?? "" });
  });
  page.on("pageerror", error => entries.push({ text: `pageerror: ${error.message}`, url: "" }));
  page.on("response", response => {
    if (response.url().startsWith(origin) && response.status() >= 400)
      entries.push({ text: `${response.status()} ${response.request().method()} ${response.url()}`, url: response.url() });
  });
  return {
    mark: () => entries.length,
    since: (index, injected) => entries.slice(index)
      .filter(entry => !(injected && (injected.url(entry.url) || INJECTED_TEXT.test(entry.text))))
      .map(entry => entry.text),
  };
}

async function newContext(seed, { width, theme }) {
  const context = await browser.newContext({
    viewport: { width, height: width === 390 ? 844 : 900 }, colorScheme: theme, reducedMotion: "reduce",
    locale: browserLocale, timezoneId: browserTimezone,
  });
  await context.clock.setFixedTime(new Date(seed.now * 1000));
  await context.addInitScript(value => localStorage.setItem("theme", value), theme);
  context.setDefaultTimeout(15_000);
  return context;
}

async function login(context, account, base) {
  const page = await context.newPage();
  try {
    await page.goto(`${base}/sign-in/`);
    await page.getByLabel("Email", { exact: true }).fill(account.email);
    await page.getByLabel("Password", { exact: true }).fill(account.password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.waitForURL(url => !url.pathname.includes("/sign-in"), { timeout: 15_000 });
  } finally {
    await page.close();
  }
}

// ---------------------------------------------------------------- injected routes

const trimmed = pathname => pathname.replace(/\/+$/, "");
// Each target names the API call a surface depends on and how to empty it. `wrap` is the envelope key, if any.
const TARGETS = {
  home: [{
    test: p => p === "/me/api/home",
    wrap: null,
    empty: j => Object.assign(j, {
      capabilities: [], apps: [], receipts: [], aliases: [], new_app_aliases: [], credential_aliases: {},
      status: { state: "unavailable", reason_code: "no_capabilities", affected_capability_ids: [], counts: { total: 0, available: 0, affected: 0 } },
    }),
  }, {
    test: p => p === "/me/api/media", wrap: null, emptyOnly: true,
    empty: j => Object.assign(j, { assets: [], next_cursor: null, has_more: false }),
  }],
  status: [
    { test: p => p === "/admin/api/status", wrap: "status",
      empty: j => Object.assign(j, { current_health: { state: "empty", tone: "neutral", label: "", detail: "", observation_available: false, services: [], signals: [] }, backends: [] }) },
    { test: p => p === "/admin/api/operator/service-registry", wrap: "service_registry",
      empty: j => Object.assign(j, { services: {} }) },
    { test: p => p === "/me/api/status", wrap: null,
      empty: j => Object.assign(j, { capabilities: [], receipts: [] }) },
    { test: p => p === "/me/api/home", wrap: null, emptyOnly: true,
      empty: j => Object.assign(j, { capabilities: [], receipts: [], aliases: [] }) },
    { test: p => p === "/admin/api/providers", wrap: null, emptyOnly: true,
      empty: j => (Array.isArray(j) ? [] : Object.assign(j, { providers: [] })) },
  ],
  library: [{
    test: p => p === "/me/api/media", wrap: null,
    empty: j => Object.assign(j, { assets: [], next_cursor: null, has_more: false }),
    expired: j => {
      (j.assets ?? []).slice(-4).forEach(asset => Object.assign(asset, { available: false, download_url: null, unavailable_reason: "Asset file is missing or expired." }));
      return j;
    },
  }],
};
const targetUrlTest = (surface, field) => url => {
  let pathname;
  try { pathname = trimmed(new URL(url).pathname); } catch { return false; }
  return TARGETS[surface].some(target => (field ? target[field] : true) && target.test(pathname));
};
const injectedFor = surface => ({ url: targetUrlTest(surface) });

async function routeTargets(page, surface, mode) {
  await page.route(targetUrlTest(surface), async route => {
    const pathname = trimmed(new URL(route.request().url()).pathname);
    const target = TARGETS[surface].find(item => item.test(pathname));
    // Secondary sources only follow "empty"; loading and errors stay on the
    // surface's own call.
    if (target.emptyOnly && mode !== "empty") return route.fallback();
    if (mode === "hold") return new Promise(() => {}); // never answered, so the page stays loading
    if (mode === "error") return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify(OUTAGE_BODY) });
    const response = await route.fetch();
    const type = response.headers()["content-type"] ?? "";
    if (!response.ok() || !type.includes("json")) return route.fulfill({ response });
    const json = await response.json();
    const root = target.wrap && json && typeof json[target.wrap] === "object" && json[target.wrap] !== null ? json[target.wrap] : json;
    const rewrite = target[mode];
    if (!rewrite) return route.fulfill({ response });
    const rewritten = rewrite(root);
    return route.fulfill({ response, json: target.wrap ? json : (rewritten ?? json) });
  });
}

// ---------------------------------------------------------------- capture plumbing

class Skip extends Error {}
const safeName = value => String(value).replace(/[^a-zA-Z0-9_-]+/g, "-");

async function shoot(page, ctx, surface, state, { notes = [], textFile = true } = {}) {
  const base = [scenario, ctx.role, surface, state, ctx.theme, ctx.width].map(safeName).join("_");
  const file = `${base}.png`;
  const viewportFile = `${base}_viewport.png`;
  const meta = { scenario, role: ctx.role, surface, state, theme: ctx.theme, width: ctx.width, file, viewport_file: viewportFile };
  if (textFile) {
    const text = await page.evaluate(() => document.querySelector("main")?.innerText ?? "").catch(() => "");
    await writeFile(path.join(output, `${base}.txt`), `${text}\n`);
    meta.text_file = `${base}.txt`;
  }
  // The viewport shot comes first so the full-page pass cannot scroll or lazy-load the first screen.
  await page.screenshot({ path: path.join(output, viewportFile), fullPage: false, animations: "disabled" });
  await page.screenshot({ path: path.join(output, file), fullPage: true, animations: "disabled" });
  ctx.pending.push({ ...meta, notes });
}

// Runs one state on a fresh page. Any failure becomes an {error} entry; the run continues.
async function attempt(ctx, surface, state, fn, { injected = null, prepare = null } = {}) {
  let page;
  const meta = { scenario, role: ctx.role, surface, state, theme: ctx.theme, width: ctx.width };
  ctx.pending = [];
  try {
    page = await ctx.context.newPage();
    const recorder = attachRecorder(page, new URL(ctx.base).origin);
    const mark = recorder.mark();
    if (prepare) await prepare(page);
    await fn(page);
    const errors = recorder.since(mark, injected);
    for (const item of ctx.pending) captures.push({ ...item, console_errors: errors, note: item.notes.join("; ") });
    if (!ctx.pending.length) captures.push({ ...meta, console_errors: errors, note: "no capture produced" });
  } catch (error) {
    const message = error.message.split("\n")[0];
    for (const item of ctx.pending) captures.push({ ...item, console_errors: [], note: item.notes.join("; ") });
    if (error instanceof Skip) captures.push({ ...meta, skipped: true, console_errors: [], note: message });
    else {
      console.error(`[d1] ${scenario} ${ctx.role} ${surface} ${state} ${ctx.theme} ${ctx.width}: ${message}`);
      captures.push({ ...meta, error: message, console_errors: [], note: "" });
    }
  } finally {
    if (page) {
      await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
      await page.close().catch(() => {});
    }
  }
}

const openAsset = page => page.locator("main").getByRole("button", { name: /^Open / }).first();

// ---------------------------------------------------------------- states

async function gotoSettled(page, ctx, url) {
  await page.goto(`${ctx.base}${url}`);
  return settle(page);
}

const statePopulated = (ctx, surface, url, state = "populated") => attempt(ctx, surface, state, async page => {
  const notes = await gotoSettled(page, ctx, url);
  await shoot(page, ctx, surface, state, { notes });
  return notes;
});

async function stateLibraryDetail(ctx) {
  await attempt(ctx, "library", "library-detail", async page => {
    const notes = await gotoSettled(page, ctx, "/library/");
    const open = openAsset(page);
    if (!(await open.count())) throw new Skip("No button named \"Open ...\" in main");
    await open.click();
    await page.locator("[role=dialog]").first().waitFor({ state: "visible", timeout: 10_000 });
    await page.waitForTimeout(600);
    await shoot(page, ctx, "library", "library-detail", { notes });
    return notes;
  });
}

async function stateLibraryKind(ctx, kind) {
  const lower = kind.toLowerCase();
  await attempt(ctx, "library", `library-${lower}`, async page => {
    const notes = await gotoSettled(page, ctx, "/library/");
    let group = page.getByRole("group", { name: "Kind" });
    // On a phone the kinds live inside Filters (D1.3).
    let inFilters = false;
    if (!(await group.count())) {
      const filters = page.getByRole("button", { name: /^Filters/ });
      if (await filters.count()) {
        await filters.first().click();
        group = page.getByRole("dialog").getByRole("group", { name: "Kind" });
        inFilters = true;
      }
    }
    if (!(await group.count())) throw new Skip("No group labelled \"Kind\"");
    const button = group.getByRole("button", { name: kind, exact: true });
    if (!(await button.count())) throw new Skip(`No button named exactly \"${kind}\" in the Kind group`);
    await button.click();
    if (inFilters) await page.keyboard.press("Escape");
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => notes.push("network did not go idle after filter"));
    await page.waitForTimeout(350);
    await shoot(page, ctx, "library", `library-${lower}-grid`, { notes: [...notes] });
    const open = openAsset(page);
    if (!(await open.count())) throw new Skip(`Grid captured, but no \"Open ...\" button for ${lower} assets`);
    // Audio tiles centre their own play button over the tile; open from a corner.
    await open.click(kind === "Audio" ? { position: { x: 12, y: 12 } } : undefined);
    await page.locator("[role=dialog]").first().waitFor({ state: "visible", timeout: 10_000 });
    if (kind === "Video") {
      await page.waitForFunction(() => {
        const video = document.querySelector("[role=dialog] video");
        return video && video.readyState >= 2;
      }, null, { timeout: 15_000 }).catch(async () => {
        const state = await page.evaluate(() => {
          const video = document.querySelector("[role=dialog] video");
          return video ? `readyState ${video.readyState}, networkState ${video.networkState}, error ${video.error?.code ?? "none"}` : "no video element";
        }).catch(() => "unknown");
        notes.push(`dialog video not ready after 15 s (${state})`);
      });
    }
    await page.waitForTimeout(600);
    await shoot(page, ctx, "library", `library-${lower}`, { notes });
    return notes;
  });
}

const stateLoading = (ctx, surface, url) => attempt(ctx, surface, "loading", async page => {
  await routeTargets(page, surface, "hold");
  await page.goto(`${ctx.base}${url}`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1_500);
  await shoot(page, ctx, surface, "loading", { notes: ["data requests held open"] });
}, { injected: injectedFor(surface) });

const stateError = (ctx, surface, url) => attempt(ctx, surface, "error", async page => {
  await routeTargets(page, surface, "error");
  await page.goto(`${ctx.base}${url}`);
  const notes = await settle(page, { cap: 10_000 });
  await shoot(page, ctx, surface, "error", { notes });
  return notes;
}, { injected: injectedFor(surface) });

const stateRewritten = (ctx, surface, url, state) => attempt(ctx, surface, state, async page => {
  await routeTargets(page, surface, state === "expired" ? "expired" : "empty");
  const notes = await gotoSettled(page, ctx, url);
  await shoot(page, ctx, surface, state, { notes });
  return notes;
});

// ---------------------------------------------------------------- run

async function captureSignedIn(seed, role, email, themeWidths) {
  for (const { theme, width } of themeWidths) {
    const context = await newContext(seed, { width, theme });
    const ctx = { role, theme, width, context, base: seed.base_url, pending: [] };
    try {
      await login(context, { email, password: seed.password }, seed.base_url);
    } catch (error) {
      captures.push({ scenario, role, surface: "all", state: "login", theme, width, error: `login failed: ${error.message.split("\n")[0]}`, console_errors: [], note: "" });
      await context.close().catch(() => {});
      continue;
    }
    if (role === "restricted") {
      for (const [surface, url] of surfaces) await statePopulated(ctx, surface, url, "restricted");
      await context.close();
      continue;
    }
    for (const [surface, url] of surfaces) await statePopulated(ctx, surface, url);
    // The two core member tasks, as they open from Home (red team round 2
    // coverage gap): Try for a picture and for reading text aloud.
    for (const [surface, url] of trySurfaces) await statePopulated(ctx, surface, url);
    await stateLibraryDetail(ctx);
    await stateLibraryKind(ctx, "Video");
    await stateLibraryKind(ctx, "Audio");
    if (allStates) {
      for (const [surface, url] of surfaces) await stateLoading(ctx, surface, url);
      for (const [surface, url] of surfaces) await stateError(ctx, surface, url);
      for (const [surface, url] of surfaces) await stateRewritten(ctx, surface, url, "empty");
      await stateRewritten(ctx, "library", "/library/", "expired");
    }
    await context.close();
  }
}

async function captureSignedOut(seed, themeWidths) {
  for (const { theme, width } of themeWidths) {
    const context = await newContext(seed, { width, theme });
    const ctx = { role: "signed-out", theme, width, context, base: seed.base_url, pending: [] };
    for (const [surface, url] of surfaces) await statePopulated(ctx, surface, url, "signed-out");
    await context.close();
  }
}

let seed;
let fatal = null;
try {
  seed = await readySeed();
  browser = await chromium.launch({ headless: true });
  const themeWidths = themes.flatMap(theme => widths.map(width => ({ theme, width })));
  await captureSignedIn(seed, "owner", seed.owner_email, themeWidths);
  await captureSignedIn(seed, "member", seed.member_email, themeWidths);
  if (allStates) {
    await captureSignedOut(seed, themeWidths);
    if (seed.restricted_email) await captureSignedIn(seed, "restricted", seed.restricted_email, themeWidths);
    else captures.push({ scenario, role: "restricted", surface: "all", state: "restricted", skipped: true, console_errors: [], note: "Manifest has no restricted_email" });
  }
} catch (error) {
  fatal = error;
  console.error(`[d1] fatal: ${error.message}`);
} finally {
  await browser?.close().catch(() => {});
  const report = {
    scenario, states: statesArg, generated_at: seed ? new Date(seed.now * 1000).toISOString() : null,
    timezone: browserTimezone, locale: browserLocale, base_url: seed?.base_url ?? baseURL,
    fatal: fatal ? fatal.message : null, captures,
  };
  await writeFile(path.join(output, "manifest.json"), `${JSON.stringify(report, null, 2)}\n`).catch(() => {});
  stop();
  await new Promise(resolve => {
    if (server.exitCode !== null || server.signalCode !== null) return resolve();
    const timer = setTimeout(() => { server.kill("SIGKILL"); resolve(); }, 20_000);
    server.once("exit", () => { clearTimeout(timer); resolve(); });
  });
}
const shots = captures.filter(item => item.file).length;
const failed = captures.filter(item => item.error).length;
const skips = captures.filter(item => item.skipped).length;
console.log(`[d1] ${scenario}: ${shots} screenshots, ${skips} skipped, ${failed} errors; manifest at ${path.join(output, "manifest.json")}`);
if (fatal) process.exitCode = 1;
