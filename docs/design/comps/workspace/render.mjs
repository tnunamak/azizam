// Render the Workspace comps to PNG and report text contrast.
// Usage: node render.mjs [surface ...]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire('/home/tnunamak/applications/azizam/web/package.json');
const { chromium } = require('@playwright/test');
const here = path.dirname(fileURLToPath(import.meta.url));

const surfaces = {
  home: { width: 390, height: 844 },
  status: { width: 1440, height: 1000 },
  library: { width: 1440, height: 1000 },
};
const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(surfaces);

// Lowest text contrast on the page, from computed colours (alpha blended onto the nearest opaque background).
function contrastAudit() {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const blend = (fg, bg) => ({ r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 });
  const bgOf = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0.99) return c;
      if (getComputedStyle(e).backgroundImage !== 'none' && e !== document.body) return { image: true };
    }
    return parse(getComputedStyle(document.documentElement).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
  };
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (seen.has(el)) continue;
    seen.add(el);
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || el.closest('.sr, svg')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    const bg = bgOf(el);
    if (bg.image) { out.push({ text: n.textContent.trim().slice(0, 40), ratio: 'over image' }); continue; }
    const fg = blend(parse(cs.color), bg);
    const [l1, l2] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    out.push({ text: n.textContent.trim().slice(0, 40), ratio: +((l1 + 0.05) / (l2 + 0.05)).toFixed(2), size: cs.fontSize, weight: cs.fontWeight });
  }
  const numeric = out.filter((o) => typeof o.ratio === 'number').sort((a, b) => a.ratio - b.ratio);
  return { min: numeric.slice(0, 4), overImage: out.filter((o) => o.ratio === 'over image') };
}

const browser = await chromium.launch();
for (const name of wanted) {
  const vp = surfaces[name];
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: vp, colorScheme: theme, deviceScaleFactor: 1 });
    await page.goto('file://' + path.join(here, `${name}.html`));
    await page.evaluate(() => document.fonts.ready);
    await page.waitForLoadState('networkidle');
    const fonts = await page.evaluate(() => [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family + ' ' + f.weight));
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    await page.screenshot({ path: path.join(here, `${name}-${theme}-${vp.width}.png`) });
    await page.evaluate(() => document.body.classList.add('static-capture'));
    await page.screenshot({ path: path.join(here, `${name}-${theme}-${vp.width}-full.png`), fullPage: true });
    const audit = await page.evaluate(contrastAudit);
    console.log(JSON.stringify({ name, theme, overflowX: overflow, fontsLoaded: [...new Set(fonts)].length, lowestContrast: audit.min, overImage: audit.overImage }));
    await page.close();
  }
}
await browser.close();
