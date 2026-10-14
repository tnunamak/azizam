#!/usr/bin/env node
// Measures the public product site in showcase/ against the bars in the site
// hill: words, height, weight, first-screen hero and CTAs, horizontal
// overflow, tap targets, text contrast, headings, image alt text and banned
// tokens. It serves showcase/ statically (no gateway, no data), so pages are
// measured in their no-gateway state. Prints one JSON report; exits 1 when a
// bar fails, 2 when the browser cannot start.
//
//   node scripts/site-check.mjs [--pages home,examples] [--shots DIR]
//
// Playwright resolves from web/node_modules. Set PLAYWRIGHT_BROWSERS_PATH when
// the browser lives outside the default cache.
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(fileURLToPath(import.meta.url), '../..');
const site = join(repo, 'showcase');
const { chromium } = createRequire(join(repo, 'web', 'package.json'))('playwright');

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const PAGES = {
  home: { path: '/showcase/', words: [350, 1100], height: { desk: 6000, phone: 9000 }, bytes: 400_000 },
  examples: { path: '/showcase/examples/', words: null, height: null, bytes: null },
};
const wanted = opt('--pages', 'home,examples').split(',').filter((name) => PAGES[name]);
const shots = opt('--shots', '');
const banned = JSON.parse(readFileSync(join(repo, 'scripts', 'site-check-banned.json'), 'utf8'));

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.webp': 'image/webp', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.ico': 'image/x-icon',
};

// /showcase/static/<file> -> showcase/<file>; /showcase/<dir>/ -> showcase/<dir>/index.html.
function fileFor(urlPath) {
  if (!urlPath.startsWith('/showcase')) return null;
  let rel = urlPath.slice('/showcase'.length).replace(/^\/static\//, '/');
  if (rel === '' || rel.endsWith('/')) rel += 'index.html';
  const full = normalize(join(site, rel));
  if (!full.startsWith(site + sep)) return null;
  return existsSync(full) && statSync(full).isFile() ? full : null;
}

let served = 0;
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  const file = fileFor(path);
  if (!file) {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end('{"error":{"message":"not served in site-check"}}');
    return;
  }
  const body = readFileSync(file);
  served += body.length;
  res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
  res.end(body);
});
await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
const base = `http://127.0.0.1:${server.address().port}`;

let browser;
try {
  browser = await chromium.launch();
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: `browser did not start: ${String(error).slice(0, 300)}` }));
  server.close();
  process.exit(2);
}

// Runs in the page. Returns the measured facts for one viewport.
function measure(phone) {
  const vh = window.innerHeight;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const rgb = (c) => (c.match(/[\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const bgOf = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0.5)) return c.slice(0, 3);
      if (getComputedStyle(n).backgroundImage !== 'none') return null;
    }
    return rgb(getComputedStyle(document.documentElement).backgroundColor).slice(0, 3);
  };
  const contrast = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el) || ![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const s = getComputedStyle(el);
    const bg = bgOf(el);
    if (!bg) continue;
    const fg = rgb(s.color);
    const [a, b] = [lum(fg.slice(0, 3)), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    const size = parseFloat(s.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) contrast.push(`${el.tagName.toLowerCase()}.${el.className || ''} ${ratio.toFixed(2)} "${el.textContent.trim().slice(0, 40)}"`);
  }
  const headings = [...document.querySelectorAll('h1,h2,h3,h4')].map((h) => Number(h.tagName[1]));
  const skips = headings.filter((level, i) => i > 0 && level > headings[i - 1] + 1).length;
  const h1 = document.querySelector('h1');
  const ctas = [...document.querySelectorAll('[data-cta]')].filter(visible);
  const small = phone
    ? [...document.querySelectorAll('a, button, summary, input, select')]
        .filter((el) => visible(el) && !el.closest('p, li, td, figcaption, code'))
        .filter((el) => { const r = el.getBoundingClientRect(); return r.height < 44 || r.width < 44; })
        .map((el) => `${el.tagName.toLowerCase()} "${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30)}"`)
    : [];
  return {
    words: document.body.innerText.split(/\s+/).filter(Boolean).length,
    height: document.documentElement.scrollHeight,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    h1Count: document.querySelectorAll('h1').length,
    headingSkips: skips,
    heroInFold: Boolean(h1) && h1.getBoundingClientRect().bottom <= vh,
    ctasInFold: ctas.length >= 2 && ctas.every((el) => el.getBoundingClientRect().bottom <= vh),
    nav: Boolean(document.querySelector('header nav, nav[aria-label]')),
    imgNoAlt: [...document.querySelectorAll('img')].filter((i) => !i.hasAttribute('alt')).length,
    imgNoSize: [...document.querySelectorAll('img')].filter((i) => !i.getAttribute('width') || !i.getAttribute('height')).length,
    smallTargets: small.slice(0, 12),
    smallTargetCount: small.length,
    contrastFailures: contrast.slice(0, 12),
    contrastFailureCount: contrast.length,
    text: document.body.innerText,
  };
}

const report = { ok: true, pages: {}, failures: [] };
const fail = (msg) => { report.ok = false; report.failures.push(msg); };
if (shots) mkdirSync(shots, { recursive: true });

for (const name of wanted) {
  const spec = PAGES[name];
  if (!fileFor(spec.path)) { fail(`${name}: ${spec.path} does not exist in showcase/`); continue; }
  const out = {};
  for (const [tag, width, height, phone] of [['desk', 1440, 900, false], ['phone', 390, 844, true]]) {
    for (const scheme of ['light', 'dark']) {
      const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme });
      const page = await ctx.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e).slice(0, 160)));
      served = 0;
      await page.goto(base + spec.path, { waitUntil: 'load' });
      await page.waitForTimeout(400);
      const m = await page.evaluate(measure, phone);
      m.bytes = served;
      m.jsErrors = errors;
      if (shots) await page.screenshot({ path: join(shots, `${name}-${tag}-${scheme}.png`), fullPage: true });
      const key = `${tag}-${scheme}`;
      const hits = banned.patterns.filter((p) => new RegExp(p, 'i').test(m.text));
      delete m.text;
      m.bannedHits = hits;
      out[key] = m;
      const where = `${name} ${key}`;
      if (m.overflowX) fail(`${where}: horizontal scroll`);
      if (m.h1Count !== 1) fail(`${where}: ${m.h1Count} h1 elements`);
      if (m.headingSkips) fail(`${where}: ${m.headingSkips} heading level skips`);
      if (m.imgNoAlt) fail(`${where}: ${m.imgNoAlt} images without alt`);
      if (m.contrastFailureCount) fail(`${where}: ${m.contrastFailureCount} text contrast failures`);
      if (hits.length) fail(`${where}: banned tokens ${hits.join(', ')}`);
      if (errors.length) fail(`${where}: ${errors.length} script errors`);
      if (name === 'home') {
        if (!m.nav) fail(`${where}: no top nav`);
        if (!m.heroInFold) fail(`${where}: h1 not in the first screen`);
        if (!m.ctasInFold) fail(`${where}: two [data-cta] links not both in the first screen`);
        if (phone && m.smallTargetCount) fail(`${where}: ${m.smallTargetCount} tap targets under 44 px`);
        if (m.imgNoSize) fail(`${where}: ${m.imgNoSize} images without width and height`);
        if (spec.height && m.height > spec.height[tag]) fail(`${where}: height ${m.height} > ${spec.height[tag]}`);
        if (spec.bytes && m.bytes > spec.bytes) fail(`${where}: ${m.bytes} bytes > ${spec.bytes}`);
        if (spec.words && scheme === 'light' && tag === 'desk' && (m.words < spec.words[0] || m.words > spec.words[1])) {
          fail(`${where}: ${m.words} words, want ${spec.words[0]}-${spec.words[1]}`);
        }
      }
      await ctx.close();
    }
  }
  report.pages[name] = out;
}

await browser.close();
server.close();
console.log(JSON.stringify(report, null, 1));
process.exit(report.ok ? 0 : 1);
