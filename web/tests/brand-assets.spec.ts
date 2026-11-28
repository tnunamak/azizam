// The favicon, app icons, manifest and link-preview metadata (DESIGN.md "Brand").
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";
import { expect, it } from "vitest";
import manifest from "@/app/manifest";
import { metadata, viewport } from "@/app/layout";
import { publicOrigin, siteMetadata } from "@/lib/site-metadata";
import { PRODUCT_NAME, PRODUCT_TAGLINE } from "@/lib/product";
import registry from "../../gateway/ui_pages.json";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file: string) => readFileSync(path.join(web, file));

type Png = { width: number; height: number; channels: number; pixels: Buffer };

/** Decodes an 8-bit, non-interlaced RGB or RGBA PNG (what brand-icons.mjs writes). */
function decodePng(data: Buffer): Png {
  expect(data.subarray(0, 8)).toEqual(
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
  );
  let offset = 8;
  let width = 0;
  let height = 0;
  let channels = 0;
  const idat: Buffer[] = [];
  while (offset < data.length) {
    const length = data.readUInt32BE(offset);
    const type = data.toString("ascii", offset + 4, offset + 8);
    const body = data.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = body.readUInt32BE(0);
      height = body.readUInt32BE(4);
      expect(body[8]).toBe(8); // bit depth
      expect(body[12]).toBe(0); // no interlace
      channels = { 2: 3, 6: 4 }[body[9]] ?? 0;
      expect(channels).toBeGreaterThan(0);
    } else if (type === "IDAT") idat.push(body);
    offset += 12 + length;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const pixels = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    for (let x = 0; x < stride; x++) {
      const value = raw[y * (stride + 1) + 1 + x];
      const left = x >= channels ? pixels[y * stride + x - channels] : 0;
      const up = y > 0 ? pixels[(y - 1) * stride + x] : 0;
      const upLeft =
        y > 0 && x >= channels ? pixels[(y - 1) * stride + x - channels] : 0;
      const predictor = [
        0,
        left,
        up,
        (left + up) >> 1,
        (() => {
          const p = left + up - upLeft;
          const [a, b, c] = [
            Math.abs(p - left),
            Math.abs(p - up),
            Math.abs(p - upLeft),
          ];
          return a <= b && a <= c ? left : b <= c ? up : upLeft;
        })(),
      ][filter];
      pixels[y * stride + x] = (value + predictor) & 0xff;
    }
  }
  return { width, height, channels, pixels };
}

const pngSize = (file: string) => {
  const data = read(file);
  return [data.readUInt32BE(16), data.readUInt32BE(20)];
};

it("ships every icon at its size, opaque tiles and the maskable mark in its safe circle", () => {
  // ships each icon file at its size.
  {
    expect(pngSize("src/app/apple-icon.png")).toEqual([180, 180]);
    for (const size of [192, 512]) {
      expect(pngSize(`public/icon-${size}.png`)).toEqual([size, size]);
      expect(pngSize(`public/icon-maskable-${size}.png`)).toEqual([size, size]);
    }
    expect(pngSize("public/opengraph-image.png")).toEqual([1200, 630]);
  }

  // packs the favicon at 16, 32 and 48 px.
  {
    const ico = read("src/app/favicon.ico");
    expect(ico.readUInt16LE(2)).toBe(1); // icon, not cursor
    const count = ico.readUInt16LE(4);
    const sizes = Array.from({ length: count }, (_, i) => {
      const entry = 6 + 16 * i;
      const png = ico.subarray(ico.readUInt32LE(entry + 12));
      const { width, height } = decodePng(
        png.subarray(0, ico.readUInt32LE(entry + 8)),
      );
      expect([ico[entry], ico[entry + 1]]).toEqual([width, height]);
      return width;
    });
    expect(sizes).toEqual([16, 32, 48]);
  }

  // draws the SVG favicon in copper for each colour scheme.
  {
    const svg = read("src/app/icon.svg").toString();
    expect(svg).toContain('viewBox="0 0 48 48"');
    expect(svg).toContain("fill:#c4532b");
    expect(svg).toMatch(
      /@media \(prefers-color-scheme:dark\)\{path\{fill:#f08a5d\}\}/,
    );
    expect(svg).not.toMatch(/gradient|<text/i);
  }

  // keeps the app tiles opaque and the maskable mark inside the safe circle.
  {
    for (const file of [
      "src/app/apple-icon.png",
      "public/icon-maskable-512.png",
    ]) {
      expect(decodePng(read(file)).channels).toBe(3);
    }
    const { width, height, channels, pixels } = decodePng(
      read("public/icon-maskable-512.png"),
    );
    const background = [...pixels.subarray(0, 3)];
    let farthest = 0;
    let ink = 0;
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const at = (y * width + x) * channels;
        const differs = [0, 1, 2].some(
          (c) => Math.abs(pixels[at + c] - background[c]) > 8,
        );
        if (!differs) continue;
        ink++;
        farthest = Math.max(
          farthest,
          Math.hypot(x + 0.5 - width / 2, y + 0.5 - height / 2),
        );
      }
    expect(ink).toBeGreaterThan(width * height * 0.05);
    expect(farthest / width).toBeLessThan(0.4);
  }

  // uses the same curl in the app as in the brand source.
  {
    const source = readFileSync(
      path.join(web, "../docs/design/brand/azizam-curl.svg"),
      "utf8",
    );
    const component = read("src/components/product/brand-mark.tsx").toString();
    const d = source.match(/ d="([^"]+)"/)?.[1];
    expect(d).toBeTruthy();
    expect(component).toContain(`d="${d}"`);
  }
});

it("titles pages, colours the browser bar and adds the preview image only with a public origin", () => {
  // titles every page under one template with a description.
  {
    expect(metadata.title).toEqual({
      default: PRODUCT_NAME,
      template: `%s · ${PRODUCT_NAME}`,
    });
    expect(metadata.description).toBeTruthy();
    expect(metadata.applicationName).toBe(PRODUCT_NAME);
  }

  // colours the browser bar per colour scheme.
  {
    expect(viewport.themeColor).toEqual([
      { media: "(prefers-color-scheme: light)", color: "#eef2f3" },
      { media: "(prefers-color-scheme: dark)", color: "#08171c" },
    ]);
  }

  // adds the link-preview image only when the build knows its public origin.
  {
    const without = siteMetadata(undefined);
    expect(without.metadataBase).toBeUndefined();
    expect(without.openGraph).not.toHaveProperty("images");
    expect(without.twitter).toEqual({ card: "summary" });

    const origin = publicOrigin(" https://ai.household.example ");
    const withOrigin = siteMetadata(origin);
    expect(withOrigin.metadataBase?.href).toBe("https://ai.household.example/");
    expect(withOrigin.openGraph).toMatchObject({
      siteName: PRODUCT_NAME,
      images: [
        {
          url: "/opengraph-image.png",
          width: 1200,
          height: 630,
          alt: `${PRODUCT_NAME}. ${PRODUCT_TAGLINE}`,
        },
      ],
    });
    expect(withOrigin.twitter).toEqual({ card: "summary_large_image" });
  }

  // ignores a public origin that is not an http(s) URL.
  {
    for (const value of [
      undefined,
      "",
      "  ",
      "ai.household.example",
      "ftp://x.example",
      "javascript:alert(1)",
    ])
      expect(publicOrigin(value)).toBeUndefined();
  }
});

it("describes the app with icons that exist at their declared sizes", () => {
  const app = manifest();

  // names the app and colours its splash.
  {
    expect(app).toMatchObject({
      name: PRODUCT_NAME,
      short_name: PRODUCT_NAME,
      start_url: "/",
      display: "standalone",
      background_color: "#0c1f25",
      theme_color: "#08171c",
    });
  }

  // lists icons that exist at their declared sizes, including maskable ones.
  {
    const icons = app.icons ?? [];
    expect(icons.map((icon) => icon.purpose)).toEqual([
      "any",
      "any",
      "maskable",
      "maskable",
    ]);
    for (const icon of icons) {
      const size = Number(icon.sizes?.split("x")[0]);
      expect(pngSize(`public${icon.src}`)).toEqual([size, size]);
      // The gateway serves only the root files its registry lists.
      expect(registry.files).toContain(icon.src.slice(1));
    }
    expect(registry.files).toEqual(
      expect.arrayContaining([
        "favicon.ico",
        "icon.svg",
        "apple-icon.png",
        "manifest.webmanifest",
        "opengraph-image.png",
      ]),
    );
  }
});
