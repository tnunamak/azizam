// Renders the Azizam icon set from the mark's canonical SVGs
// (docs/design/brand/curl.py writes them; rules in docs/design/DESIGN.md, "Brand").
//
//   node scripts/brand-icons.mjs        (from web/; needs `npx playwright install chromium`)
//
// Writes the files Next.js and the gateway serve at the site root:
//   src/app/icon.svg            favicon for modern browsers, copper per colour scheme
//   src/app/favicon.ico         16, 32 and 48 px, the small-size drawing in tab copper
//   src/app/apple-icon.png      180 px, deep-water tile
//   public/icon-192.png, icon-512.png                    rounded deep-water tiles
//   public/icon-maskable-192.png, icon-maskable-512.png  full-bleed, mark in the safe circle
//   public/opengraph-image.png  1200 x 630 link preview
// The manifest (src/app/manifest.ts) points at the public/ icons.
// gateway/ui_pages.json lists the same root files; the build fails if they drift.
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const brand = path.resolve(web, "../docs/design/brand");
const font = path.resolve(
  web,
  "node_modules/@fontsource-variable/hanken-grotesk/files/hanken-grotesk-latin-wght-normal.woff2",
);

// Colours (tokens.css: --chrome-accent light and dark, --background dark).
const COPPER = "#c4532b";
const COPPER_ON_DARK = "#f08a5d";
// The .ico cannot follow the colour scheme: this copper keeps 2.9:1 or better on
// both light and dark browser tabs (DESIGN "Brand").
const TAB_COPPER = "#d65f33";
const DEEP_WATER = "#0c1f25";
const INK_ON_DARK = "#e3ecee";
const MUTED_ON_DARK = "#b4c6cb";
// The showcase's own headline: one plain line, no slogan.
const DESCRIPTOR = "One AI gateway for your apps and the people you invite."; // = PRODUCT_TAGLINE (src/lib/product.ts)

async function pathOf(name) {
  const svg = await readFile(path.join(brand, name), "utf8");
  const box = svg.match(/viewBox="([^"]+)"/)[1];
  const d = svg.match(/ d="([^"]+)"/)[1];
  return { box, d };
}

const ui = await pathOf("azizam-curl.svg");
const small = await pathOf("azizam-curl-small.svg");
// Share of the tile the mark's 48 box takes: the ink is 92 % of that box.
const TILE_SHARE = 0.7;
// Maskable: every pixel stays inside the 40 % safe circle (checked by a test).
const MASKABLE_SHARE = 0.56;

const mark = ({ box, d }, color, size) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${box}" width="${size}" height="${size}"><path fill="${color}" d="${d}"/></svg>`;

// The favicon follows the browser's colour scheme.
const iconSvg =
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${small.box}">` +
  `<style>path{fill:${COPPER}}@media (prefers-color-scheme:dark){path{fill:${COPPER_ON_DARK}}}</style>` +
  `<path d="${small.d}"/></svg>\n`;
await writeFile(path.join(web, "src/app/icon.svg"), iconSvg);

const scratch = await mkdtemp(path.join(tmpdir(), "azizam-icons-"));
const browser = await chromium.launch();
const page = await browser.newPage({
  deviceScaleFactor: 1,
  viewport: { width: 1200, height: 630 },
});

async function render(html, width, height, transparent = false) {
  const file = path.join(scratch, "page.html");
  await writeFile(
    file,
    `<!doctype html><html><head><meta charset="utf-8"><style>
      @font-face { font-family: Hanken; src: url("${pathToFileURL(font)}") format("woff2-variations"); font-weight: 100 900; }
      html, body { margin: 0; width: ${width}px; height: ${height}px; overflow: hidden; background: transparent; }
      svg { display: block; }
    </style></head><body>${html}</body></html>`,
  );
  await page.goto(pathToFileURL(file).href);
  await page.evaluate(() => document.fonts.ready);
  // One viewport for every size: Chromium will not reliably capture a 16 px one.
  return page.screenshot({
    omitBackground: transparent,
    type: "png",
    clip: { x: 0, y: 0, width, height },
  });
}

const square = (size, background, radius, content) =>
  `<div style="width:${size}px;height:${size}px;background:${background};border-radius:${radius}px;display:flex;align-items:center;justify-content:center">${content}</div>`;

// favicon.ico: three PNG entries in one ICO container.
const sizes = [16, 32, 48];
const pngs = [];
for (const size of sizes)
  pngs.push(await render(mark(small, TAB_COPPER, size), size, size, true));
const header = Buffer.alloc(6 + 16 * sizes.length);
header.writeUInt16LE(0, 0);
header.writeUInt16LE(1, 2);
header.writeUInt16LE(sizes.length, 4);
let offset = header.length;
sizes.forEach((size, i) => {
  const entry = 6 + 16 * i;
  header.writeUInt8(size, entry);
  header.writeUInt8(size, entry + 1);
  header.writeUInt16LE(1, entry + 4); // colour planes
  header.writeUInt16LE(32, entry + 6); // bits per pixel
  header.writeUInt32LE(pngs[i].length, entry + 8);
  header.writeUInt32LE(offset, entry + 12);
  offset += pngs[i].length;
});
await writeFile(
  path.join(web, "src/app/favicon.ico"),
  Buffer.concat([header, ...pngs]),
);

// Apple touch icon: opaque and square; iOS rounds the corners itself.
await writeFile(
  path.join(web, "src/app/apple-icon.png"),
  await render(
    square(
      180,
      DEEP_WATER,
      0,
      mark(ui, COPPER_ON_DARK, Math.round(180 * TILE_SHARE)),
    ),
    180,
    180,
  ),
);

for (const size of [192, 512]) {
  // "any" icons: a rounded tile, as desktops show them unmasked.
  await writeFile(
    path.join(web, `public/icon-${size}.png`),
    await render(
      square(
        size,
        DEEP_WATER,
        Math.round(size * 0.225),
        mark(ui, COPPER_ON_DARK, Math.round(size * TILE_SHARE)),
      ),
      size,
      size,
      true,
    ),
  );
  // Maskable icons: full bleed; the launcher cuts the shape.
  await writeFile(
    path.join(web, `public/icon-maskable-${size}.png`),
    await render(
      square(
        size,
        DEEP_WATER,
        0,
        mark(ui, COPPER_ON_DARK, Math.round(size * MASKABLE_SHARE)),
      ),
      size,
      size,
    ),
  );
}

// Open Graph: the lockup on deep water and one plain line about the product.
// The curl's ink spans x 10 to 38 of its 48 box: pull the box back to the text
// edge on the left and toward the name on the right.
await writeFile(
  path.join(web, "public/opengraph-image.png"),
  await render(
    `<div style="width:1200px;height:630px;background:${DEEP_WATER};display:flex;flex-direction:column;justify-content:center;padding:0 112px;box-sizing:border-box;font-family:Hanken">
      <div style="display:flex;align-items:center;gap:32px">
        <div style="margin-left:${-Math.round((150 * 10) / 48)}px;margin-right:${-Math.round((150 * 10) / 48)}px">${mark(ui, COPPER_ON_DARK, 150)}</div>
        <span style="color:${INK_ON_DARK};font-size:128px;font-weight:600;letter-spacing:-0.025em;line-height:1">Azizam</span>
      </div>
      <p style="margin:48px 0 0;color:${MUTED_ON_DARK};font-size:36px;font-weight:450;letter-spacing:-0.005em;line-height:1.3;white-space:nowrap">${DESCRIPTOR}</p>
    </div>`,
    1200,
    630,
  ),
);

await browser.close();
await rm(scratch, { recursive: true, force: true });
console.log("brand icons written");
