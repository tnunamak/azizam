// Run through web/e2e/seed-review.mjs; use only its isolated seeded gateway.
import { createRequire } from 'node:module';
import { mkdir, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(here, '../../../web/package.json'));
const { chromium } = require('@playwright/test');
const baseURL = process.env.APP_URL;
const url = new URL(baseURL);
if (url.hostname !== '127.0.0.1' || Number(url.port) < 18000 || Number(url.port) > 18998) {
  throw new Error('Capture requires an isolated gateway on ports 18000–18998');
}
const browser = await chromium.launch();
try {
  const context = await browser.newContext({
    baseURL, storageState: `test-results/auth-${url.port}.json`,
    viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1,
    colorScheme: 'light', reducedMotion: 'reduce',
  });
  await context.addInitScript(() => localStorage.setItem('theme', 'light'));
  const page = await context.newPage();
  await mkdir(here, { recursive: true });
  for (const route of ['home', 'models', 'people']) {
    await page.goto(`/ui/${route}/`);
    await page.waitForLoadState('networkidle');
    await page.locator('main').waitFor();
    await page.evaluate(() => document.fonts.ready);
    if (await page.getByText('Something went wrong', { exact: true }).count()) {
      throw new Error(`${route} shows an error`);
    }
    const png = path.join(here, `${route}-light-1440.png`);
    const webp = path.join(here, `${route}-light-1440.webp`);
    await page.screenshot({ path: png, animations: 'disabled' });
    execFileSync(path.resolve(here, '../../../venv/bin/python'), ['-c',
      'from PIL import Image; import sys; Image.open(sys.argv[1]).save(sys.argv[2], "WEBP", quality=88, method=6)',
      png, webp]);
    await unlink(png);
    console.log(`Captured ${route}: light, 1440 × 1000`);
  }
} finally {
  await browser.close();
}
