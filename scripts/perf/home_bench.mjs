// Load one page (default Home) in a real browser and break the time down.
//
//   cd web && node ../scripts/perf/home_bench.mjs --manifest STATE/perf-manifest.json [--route /] [--who owner] [--out home.json]
//
// Profiles: desktop (no throttle) and mobile (390x844 @3x, 4x CPU slowdown, 1.6 Mbit/s down,
// 750 kbit/s up, 150 ms round trip: Lighthouse's "slow 4G"). Each runs cold (empty HTTP cache) and warm
// (second load in the same context). Every run reports:
//   content_ms   <main> has text and no busy marker; ready_ms: its last change before 700 ms of stillness
//   fcp/lcp      paint timings
//   html_ms      response end of the document
//   js           bytes on the wire / decoded, request count; script_ms: main-thread JavaScript run time;
//                parse_ms: V8 compile+parse on the main thread; main-thread task time
//   api[]        each API call: start, end, status, bytes (ms from navigation start)
//   by_type      bytes and count per resource type, plus encoding (gzip/br/none) of the largest scripts
import { createRequire } from "node:module";
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
const { chromium, request, devices } = createRequire(join(process.cwd(), "package.json"))("@playwright/test");

const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => (v.startsWith("--") ? [...a, [v.slice(2), all[i + 1]]] : a), []));
const manifest = JSON.parse(readFileSync(args.manifest, "utf8"));
const route = args.route ?? "/";
const who = args.who ?? "owner";
const REPEATS = Number(args.repeats ?? 3);

const PROFILES = {
  desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, isMobile: false, cpu: 1, net: null },
  mobile: {
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true, cpu: 4,
    userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36",
    net: { latency: 150, downloadThroughput: (1.6 * 1024 * 1024) / 8, uploadThroughput: (750 * 1024) / 8, offline: false },
  },
};

const INIT = `(() => {
  window.__p = { meaningful: null, lastMain: 0, lcp: null, fcp: null, long: [] };
  new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__p.long.push(e.duration); }).observe({ entryTypes: ["longtask"] });
  new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.name === "first-contentful-paint") window.__p.fcp = e.startTime; }).observe({ type: "paint", buffered: true });
  new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__p.lcp = e.startTime; }).observe({ type: "largest-contentful-paint", buffered: true });
  const check = () => {
    if (window.__p.meaningful !== null) return;
    const main = document.querySelector("main"); if (!main) return;
    if (main.querySelector('[aria-busy="true"], [data-loading="true"]')) return;
    if ((main.innerText || "").trim().length >= 60) window.__p.meaningful = performance.now();
  };
  new MutationObserver((rs) => {
    for (const r of rs) { if (r.type === "attributes") continue;
      const n = r.target.nodeType === 1 ? r.target : r.target.parentElement;
      if (n && n.closest && n.closest("main") && !n.closest('[aria-hidden="true"]')) window.__p.lastMain = performance.now(); }
    check();
  }).observe(document, { childList: true, subtree: true, characterData: true, attributes: true });
  setInterval(check, 25);
})();`;

const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const median = (xs) => { const s = xs.filter((x) => x != null).sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };

async function load(browser, context, profile, label) {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Performance.enable");
  if (profile.cpu > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: profile.cpu });
  if (profile.net) await cdp.send("Network.emulateNetworkConditions", profile.net);
  const reqs = new Map();
  let t0 = 0;
  const now = () => Date.now() - t0;
  cdp.on("Network.requestWillBeSent", (e) => reqs.set(e.requestId, { url: e.request.url, type: e.type, start: now(), t: e.timestamp }));
  cdp.on("Network.responseReceived", (e) => { const r = reqs.get(e.requestId); if (r) { Object.assign(r, { status: e.response.status, enc: e.response.headers["content-encoding"] ?? e.response.headers["Content-Encoding"] ?? "none", cached: e.response.fromDiskCache || e.response.fromServiceWorker, cc: e.response.headers["cache-control"] ?? "", ttfb: now() }); } });
  cdp.on("Network.loadingFinished", (e) => { const r = reqs.get(e.requestId); if (r) { r.end = now(); r.wire = e.encodedDataLength; } });
  cdp.on("Network.dataReceived", (e) => { const r = reqs.get(e.requestId); if (r) r.decoded = (r.decoded ?? 0) + e.dataLength; });
  await page.addInitScript(INIT);
  const m0 = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
  const traceFile = join(mkdtempSync(join(tmpdir(), "homebench-")), "trace.json");
  await browser.startTracing(page, { path: traceFile, categories: ["devtools.timeline", "v8", "disabled-by-default-v8.compile", "blink.user_timing"] });
  t0 = Date.now();
  let error = null;
  try {
    await page.goto(manifest.base_url + route, { waitUntil: "load", timeout: 90000 });
    await page.waitForFunction(() => window.__p.meaningful !== null, null, { timeout: 60000 }).catch(() => { error = "no content in 60 s"; });
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const still = await page.evaluate(() => performance.now() - window.__p.lastMain).catch(() => 1e9);
      if (still >= 700) break;
      await page.waitForTimeout(100);
    }
  } catch (e) { error = String(e).split("\n")[0]; }
  const p = await page.evaluate(() => window.__p).catch(() => ({}));
  const nav = await page.evaluate(() => { const n = performance.getEntriesByType("navigation")[0]; return n ? { html_ms: n.responseEnd, dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd } : {}; }).catch(() => ({}));
  const m1 = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
  await browser.stopTracing();
  await page.close();
  // main-thread cost from the trace
  const events = JSON.parse(readFileSync(traceFile, "utf8")).traceEvents ?? [];
  const mainPid = (events.find((e) => e.name === "thread_name" && e.args?.name === "CrRendererMain") ?? {}).pid;
  const mainTids = new Set(events.filter((e) => e.name === "thread_name" && e.args?.name === "CrRendererMain").map((e) => `${e.pid}:${e.tid}`));
  const dur = (names) => sum(events.filter((e) => e.ph === "X" && names.includes(e.name) && mainTids.has(`${e.pid}:${e.tid}`)).map((e) => e.dur)) / 1000 / profile.cpu; // trace durations are throttled time already; report as measured
  const raw = (names) => sum(events.filter((e) => e.ph === "X" && names.includes(e.name) && mainTids.has(`${e.pid}:${e.tid}`)).map((e) => e.dur)) / 1000;
  const list = [...reqs.values()].filter((r) => r.end != null);
  const by = {};
  for (const r of list) { const k = r.type; by[k] ??= { n: 0, wire_kb: 0 }; by[k].n += 1; by[k].wire_kb += (r.wire ?? 0) / 1024; }
  for (const v of Object.values(by)) v.wire_kb = Math.round(v.wire_kb);
  const scripts = list.filter((r) => r.type === "Script").sort((a, b) => (b.decoded ?? 0) - (a.decoded ?? 0));
  const api = list.filter((r) => /^\/(me\/api|admin\/api|v1|auth\/api)/.test(new URL(r.url).pathname))
    .map((r) => ({ path: new URL(r.url).pathname + new URL(r.url).search.slice(0, 30), start: r.start, end: r.end, status: r.status, kb: Math.round((r.wire ?? 0) / 1024) })).sort((a, b) => a.start - b.start);
  return {
    label, error, content_ms: p.meaningful == null ? null : Math.round(p.meaningful), ready_ms: Math.round(p.lastMain ?? 0), fcp_ms: Math.round(p.fcp ?? 0), lcp_ms: Math.round(p.lcp ?? 0),
    html_ms: Math.round(nav.html_ms ?? 0), load_ms: Math.round(nav.load ?? 0), long_tasks: (p.long ?? []).length, long_task_ms: Math.round(sum(p.long ?? [])),
    requests: list.length, wire_kb: Math.round(sum(list.map((r) => r.wire ?? 0)) / 1024),
    js_requests: scripts.length, js_wire_kb: Math.round(sum(scripts.map((r) => r.wire ?? 0)) / 1024), js_decoded_kb: Math.round(sum(scripts.map((r) => r.decoded ?? 0)) / 1024),
    js_uncompressed: scripts.filter((r) => r.enc === "none" && (r.wire ?? 0) > 20000).length, css_kb: Math.round(sum(list.filter((r) => r.type === "Stylesheet").map((r) => r.wire ?? 0)) / 1024),
    script_ms: Math.round((m1.ScriptDuration - m0.ScriptDuration) * 1000), task_ms: Math.round((m1.TaskDuration - m0.TaskDuration) * 1000),
    layout_ms: Math.round((m1.LayoutDuration - m0.LayoutDuration) * 1000), style_ms: Math.round((m1.RecalcStyleDuration - m0.RecalcStyleDuration) * 1000),
    compile_ms: Math.round(raw(["v8.compile", "V8.CompileCode", "v8.compileModule"])), evaluate_ms: Math.round(raw(["EvaluateScript", "v8.run"])),
    cached: list.filter((r) => r.cached).length, by_type: by, all_scripts: scripts.map((r) => ({ f: r.url.split("/_next/static/")[1] ?? r.url, wire_kb: Math.round((r.wire ?? 0) / 1024), decoded_kb: Math.round((r.decoded ?? 0) / 1024), start: r.start, end: r.end })).sort((a, b) => a.start - b.start),
    top_scripts: scripts.slice(0, 5).map((r) => ({ f: r.url.split("/").pop().slice(0, 40), wire_kb: Math.round((r.wire ?? 0) / 1024), enc: r.enc, cc: r.cc.slice(0, 40) })), api,
  };
}

const browser = await chromium.launch();
const api = await request.newContext({ baseURL: manifest.base_url });
const login = await api.post("/auth/api/login", { data: { email: manifest[`${who}_email`], password: manifest.password } });
if (!login.ok()) throw new Error(`login ${login.status()}`);
const storage = await api.storageState();
await api.dispose();

const out = {};
for (const [name, profile] of Object.entries(PROFILES).filter(([n]) => !args.profiles || args.profiles.split(",").includes(n))) {
  const opts = { storageState: storage, viewport: profile.viewport, deviceScaleFactor: profile.deviceScaleFactor, isMobile: profile.isMobile, hasTouch: !!profile.hasTouch, userAgent: profile.userAgent };
  const cold = [], warm = [];
  for (let i = 0; i < REPEATS; i++) {
    const context = await browser.newContext(opts); // empty cache every time
    cold.push(await load(browser, context, profile, `${name}-cold`));
    warm.push(await load(browser, context, profile, `${name}-warm`));
    await context.close();
    console.error(`${name} ${i}: cold ${cold.at(-1).content_ms} ms, warm ${warm.at(-1).content_ms} ms`);
  }
  const pick = (runs) => runs.sort((a, b) => (a.content_ms ?? 1e9) - (b.content_ms ?? 1e9))[Math.floor(runs.length / 2)]; // the median run, whole
  out[name] = { cold: pick(cold), warm: pick(warm) };
}
await browser.close();
writeFileSync(args.out ?? "home-bench.json", JSON.stringify({ route, who, profiles: out }, null, 1));
const rows = ["| profile | cache | content | ready | FCP | LCP | HTML | requests | wire KB | JS KB (wire/decoded, req) | uncompressed JS | script ms | task ms | long tasks |", "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"];
for (const [n, v] of Object.entries(out)) for (const c of ["cold", "warm"]) { const r = v[c];
  rows.push(`| ${n} | ${c} | ${r.content_ms} | ${r.ready_ms} | ${r.fcp_ms} | ${r.lcp_ms} | ${r.html_ms} | ${r.requests} | ${r.wire_kb} | ${r.js_wire_kb}/${r.js_decoded_kb}, ${r.js_requests} | ${r.js_uncompressed} | ${r.script_ms} | ${r.task_ms} | ${r.long_tasks} (${r.long_task_ms}) |`); }
console.log(rows.join("\n"));
