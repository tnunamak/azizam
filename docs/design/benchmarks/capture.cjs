// Benchmark capture tool. Private headless Chromium, a fresh context per shot,
// no profile, no credentials, no login. Pattern follows ../references/capture.cjs.
//
// Usage:
//   node capture.cjs live  <surface> <product-view> <url> [--phone] [--no-desktop] [--full] [--wait ms] [--click "css"] [--scroll px] [--dark]
//       -> <surface>/<product-view>-desktop.webp (1440x900 viewport, DPR 1)
//          <surface>/<product-view>-phone.webp   (390x844, DPR 1, isMobile)   with --phone
//   node capture.cjs image <surface> <product-view> <image-url> [--referer url]
//       -> <surface>/<product-view>.webp at native size (no upscaling; max 2400 px wide)
//   node capture.cjs elem  <surface> <product-view> <page-url> <css-selector> [--index n] [--wait ms]
//       -> screenshot of one element (an official product image on a page) at the size it renders at 1440
//   node capture.cjs list  _ _ <page-url>
//       -> discovery only: scrolls to load lazy images and prints every image URL >= 300 px wide
//
// Every run appends a record (url, final url, HTTP status, title, date, output files)
// to capture-log/<surface>.jsonl so BOARD.md can cite exact sources.
const { chromium } = require('/home/tnunamak/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const root = __dirname;
const CHROME = '/home/tnunamak/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const UA_DESKTOP = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36';
const UA_PHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

const argv = process.argv.slice(2);
const [mode, surface, id] = argv;
const flag = (n) => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : d; };
if (!mode || !surface || !id) { console.error('see header for usage'); process.exit(2); }
const outDir = path.join(root, surface);
fs.mkdirSync(outDir, { recursive: true });

function toWebp(src, dst, maxW = 2400) {
  execFileSync('magick', [src, '-resize', `${maxW}x>`, '-quality', '82', dst]);
  fs.unlinkSync(src);
  return execFileSync('magick', ['identify', '-format', '%wx%h', dst + '[0]']).toString();
}
function log(rec) {
  fs.mkdirSync(path.join(root, 'capture-log'), { recursive: true });
  fs.appendFileSync(path.join(root, 'capture-log', `${rec.surface}.jsonl`), JSON.stringify(rec) + '\n');
}
async function settle(page, wait) {
  await page.waitForTimeout(wait);
  // Dismiss common cookie banners without accepting tracking where possible.
  for (const sel of ['button:has-text("Reject all")', 'button:has-text("Decline")', 'button:has-text("Necessary only")', 'button:has-text("Accept all")', 'button:has-text("Accept")', 'button:has-text("Got it")']) {
    const b = page.locator(sel).first();
    if (await b.isVisible().catch(() => false)) { await b.click({ timeout: 2000 }).catch(() => {}); await page.waitForTimeout(500); break; }
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: CHROME, args: ['--disable-dev-shm-usage'] });
  const date = new Date().toISOString().slice(0, 10);
  const rec = { surface, id, mode, date, files: [] };
  try {
    const wait = Number(opt('--wait', 4000));
    if (mode === 'live') {
      const url = argv[3]; rec.url = url;
      const shots = [];
      if (!flag('--no-desktop')) shots.push(['desktop', { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, userAgent: UA_DESKTOP }]);
      if (flag('--phone')) shots.push(['phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true, userAgent: UA_PHONE }]);
      for (const [kind, ctxOpts] of shots) {
        const ctx = await browser.newContext({ ...ctxOpts, colorScheme: flag('--dark') ? 'dark' : 'light' });
        const page = await ctx.newPage();
        const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        rec.status = resp?.status();
        await settle(page, wait);
        if (opt('--click')) { await page.locator(opt('--click')).first().click({ timeout: 5000 }).catch((e) => { rec.clickError = e.message.slice(0, 120); }); await page.waitForTimeout(2000); }
        if (opt('--scroll')) { await page.mouse.wheel(0, Number(opt('--scroll'))); await page.waitForTimeout(1500); }
        rec.finalUrl = page.url(); rec.title = await page.title();
        const png = path.join(outDir, `${id}-${kind}.png`);
        await page.screenshot({ path: png, fullPage: flag('--full') });
        const dims = toWebp(png, png.replace(/\.png$/, '.webp'), flag('--full') ? 1440 : 2400);
        rec.files.push({ file: `${surface}/${id}-${kind}.webp`, dims });
        await ctx.close();
      }
    } else if (mode === 'image') {
      const url = argv[3]; rec.url = url;
      const ctx = await browser.newContext({ userAgent: UA_DESKTOP, extraHTTPHeaders: opt('--referer') ? { referer: opt('--referer') } : {} });
      const resp = await ctx.request.get(url, { timeout: 45000 });
      rec.status = resp.status();
      if (!resp.ok()) throw new Error(`HTTP ${resp.status()}`);
      const ext = (resp.headers()['content-type'] || '').split('/')[1]?.split(';')[0] || 'bin';
      const tmp = path.join(outDir, `${id}.src.${ext.replace('svg+xml', 'svg')}`);
      fs.writeFileSync(tmp, await resp.body());
      const dims = toWebp(tmp, path.join(outDir, `${id}.webp`));
      rec.files.push({ file: `${surface}/${id}.webp`, dims });
      rec.referer = opt('--referer');
      await ctx.close();
    } else if (mode === 'elem') {
      const url = argv[3], sel = argv[4]; rec.url = url; rec.selector = sel;
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, userAgent: UA_DESKTOP });
      const page = await ctx.newPage();
      const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      rec.status = resp?.status();
      await settle(page, wait);
      const el = page.locator(sel).nth(Number(opt('--index', 0)));
      await el.scrollIntoViewIfNeeded(); await page.waitForTimeout(2500);
      rec.finalUrl = page.url(); rec.title = await page.title();
      const png = path.join(outDir, `${id}.png`);
      await el.screenshot({ path: png });
      const dims = toWebp(png, path.join(outDir, `${id}.webp`));
      rec.files.push({ file: `${surface}/${id}.webp`, dims });
      await ctx.close();
    } else if (mode === 'list') {
      // Discovery only, writes nothing: scroll the page so lazy images load, then print
      // every image (img, picture source, CSS background) with its natural size.
      // Usage: node capture.cjs list _ _ <page-url>
      const url = argv[3];
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, userAgent: UA_DESKTOP });
      const page = await ctx.newPage();
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await settle(page, wait);
      for (let y = 0; y < 30; y++) { await page.mouse.wheel(0, 900); await page.waitForTimeout(300); }
      await page.waitForTimeout(2000);
      const imgs = await page.evaluate(() => {
        const out = [];
        document.querySelectorAll('img').forEach((i) => out.push([i.naturalWidth, i.naturalHeight, i.currentSrc || i.src, i.alt]));
        document.querySelectorAll('*').forEach((e) => { const b = getComputedStyle(e).backgroundImage; const m = b && b.match(/url\("?([^")]+)"?\)/); if (m) out.push([0, 0, m[1], 'css-bg']); });
        return out;
      });
      for (const [w, h, src, alt] of imgs) if (!w || w >= 300) console.log(`${w}x${h}\t${src}\t${(alt || '').slice(0, 80)}`);
      await ctx.close(); await browser.close(); return;
    } else throw new Error('unknown mode ' + mode);
    console.log('OK', JSON.stringify(rec.files), rec.status, rec.title || '');
  } catch (e) {
    rec.error = e.message.slice(0, 300);
    console.log('FAILED', id, rec.error);
  }
  log(rec);
  await browser.close();
})();
