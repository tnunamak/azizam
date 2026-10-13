// Load every UI page, signed in, against a perf fixture gateway (scripts/perf/serve.py).
//
//   cd web && node ../scripts/perf/ui_bench.mjs --manifest STATE/perf-manifest.json --out ui.json
//
// Per page and persona (owner, member) it records, from navigation start:
//   meaningful_ms  first moment <main> holds >= 60 characters with no aria-busy element
//   settled_ms     last /me/api|/admin/api|/v1 response finished (the page waits for API quiet, 30 s cap)
//   ready_ms       max(meaningful_ms, settled_ms): when the data the page shows is on screen — the ranked number
//   requests, bytes (response bodies), api_requests, long_tasks (count / total ms), fcp_ms
// Each page is loaded REPEATS times in a fresh context (cold JS cache is not simulated: one
// warm-up load fills the HTTP cache); the median is reported.
import { createRequire } from "node:module";
import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Resolve Playwright from the working directory (web/), not from this script's directory.
const { chromium, request } = createRequire(join(process.cwd(), "package.json"))("@playwright/test");

const REPEATS = 3;
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, v, i, all) => (v.startsWith("--") ? [...acc, [v.slice(2), all[i + 1]]] : acc), []));
const manifest = JSON.parse(readFileSync(args.manifest, "utf8"));
const base = manifest.base_url;

function pages() {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "web", "src", "app");
  const out = [];
  const walk = (dir, route) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) {
        if (!name.startsWith("_") && !name.startsWith("[")) walk(full, `${route}/${name}`);
      } else if (name === "page.tsx") out.push(route || "/");
    }
  };
  walk(root, "");
  return out.filter((r) => !["/design", "/setup", "/invite", "/sign-in"].includes(r)).sort();
}

const INIT = `
  (() => {
    window.__perf = { meaningful: null, long: [], fcp: null };
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.long.push(e.duration); })
      .observe({ entryTypes: ["longtask"] });
    new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.name === "first-contentful-paint") window.__perf.fcp = e.startTime; })
      .observe({ type: "paint", buffered: true });
    const check = () => {
      if (window.__perf.meaningful !== null) return;
      const main = document.querySelector("main");
      if (!main) return;
      if (main.querySelector('[aria-busy="true"], [data-loading="true"]')) return;
      if ((main.innerText || "").trim().length >= 60) window.__perf.meaningful = performance.now();
    };
    new MutationObserver(check).observe(document, { childList: true, subtree: true, characterData: true, attributes: true });
    setInterval(check, 25);
  })();
`;

const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };

async function loadOnce(browser, state, route) {
  const context = await browser.newContext({ storageState: state, viewport: { width: 1440, height: 900 } });
  await context.addInitScript(INIT);
  const page = await context.newPage();
  const apiDone = [];
  let requests = 0, bytes = 0, api = 0, inflight = 0;
  const isApi = (u) => /^\/(me\/api|admin\/api|v1|auth\/api)/.test(new URL(u).pathname);
  page.on("request", (r) => { if (isApi(r.url())) inflight += 1; });
  page.on("requestfailed", (r) => { if (isApi(r.url())) inflight -= 1; });
  page.on("requestfinished", (r) => { if (isApi(r.url())) inflight -= 1; });
  const t0 = { v: 0 };
  page.on("response", async (response) => {
    requests += 1;
    const url = new URL(response.url());
    try { const len = Number(response.headers()["content-length"]); bytes += Number.isFinite(len) ? len : (await response.body()).length; } catch {}
    if (/^\/(me\/api|admin\/api|v1|auth\/api)/.test(url.pathname)) { api += 1; apiDone.push(Date.now()); }
  });
  const start = Date.now();
  t0.v = start;
  let error = null;
  try {
    await page.goto(base + route, { waitUntil: "load", timeout: 60000 });
    await page.waitForFunction(() => window.__perf.meaningful !== null, null, { timeout: 30000 }).catch((e) => { error = "no meaningful content in 30 s"; });
    // quiet = no API request in flight for 400 ms
    let quietSince = null;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (inflight === 0) { quietSince ??= Date.now(); if (Date.now() - quietSince >= 400) break; } else quietSince = null;
      await page.waitForTimeout(50);
    }
  } catch (e) { error = String(e).split("\n")[0]; }
  const perf = await page.evaluate(() => window.__perf).catch(() => ({ meaningful: null, long: [], fcp: null }));
  const final = new URL(page.url()).pathname;
  await context.close();
  const settled = apiDone.length ? Math.max(...apiDone) - start : 0;
  return {
    ready_ms: perf.meaningful === null ? null : Math.max(perf.meaningful, settled),
    meaningful_ms: perf.meaningful, fcp_ms: perf.fcp, settled_ms: settled,
    requests, api_requests: api, bytes, long_tasks: perf.long.length, long_task_ms: Math.round(perf.long.reduce((a, b) => a + b, 0)),
    final, error,
  };
}

const browser = await chromium.launch();
const rows = [];
for (const [who, email] of [["owner", manifest.owner_email], ["member", manifest.member_email]]) {
  const api = await request.newContext({ baseURL: base });
  const login = await api.post("/auth/api/login", { data: { email, password: manifest.password } });
  if (!login.ok()) throw new Error(`login ${who}: ${login.status()}`);
  const state = await api.storageState();
  await api.dispose();
  for (const route of pages()) {
    await loadOnce(browser, state, route); // warm the HTTP cache and the server
    const runs = [];
    for (let i = 0; i < REPEATS; i++) runs.push(await loadOnce(browser, state, route));
    const ok = runs.filter((r) => r.meaningful_ms !== null);
    rows.push({
      route, who, final: runs[0].final, error: ok.length ? null : runs[0].error,
      meaningful_ms: ok.length ? Math.round(median(ok.map((r) => r.meaningful_ms))) : null,
      ready_ms: ok.length ? Math.round(median(ok.map((r) => r.ready_ms))) : null,
      settled_ms: median(runs.map((r) => r.settled_ms ?? 0)), fcp_ms: Math.round(median(runs.map((r) => r.fcp_ms ?? 0))),
      requests: median(runs.map((r) => r.requests)), api_requests: median(runs.map((r) => r.api_requests)),
      bytes: median(runs.map((r) => r.bytes)), long_tasks: median(runs.map((r) => r.long_tasks)), long_task_ms: median(runs.map((r) => r.long_task_ms)),
    });
    console.error(`${who} ${route} -> meaningful ${rows.at(-1).ready_ms} ms (shell ${rows.at(-1).meaningful_ms})`);
  }
}
await browser.close();
writeFileSync(args.out, JSON.stringify({ rows }, null, 1));
const sorted = [...rows].sort((a, b) => (b.ready_ms ?? 1e9) - (a.ready_ms ?? 1e9));
console.log("| page | who | ready ms | shell ms | requests | api | KB | long tasks (ms) |\n|---|---|--:|--:|--:|--:|--:|--:|");
for (const r of sorted) console.log(`| ${r.route}${r.final !== r.route ? " -> " + r.final : ""} | ${r.who} | ${r.ready_ms ?? "NONE"} | ${r.meaningful_ms} | ${r.requests} | ${r.api_requests} | ${Math.round(r.bytes / 1024)} | ${r.long_tasks} (${r.long_task_ms}) |`);
