import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { deflateSync } from "node:zlib";
import type { Locator, Page, Route } from "@playwright/test";
import ltxCatalog from "../tests/fixtures/workflows-ltx-i2v.json";
import { expect, test, testOrigin } from "./fixtures/isolation";
import { fulfilBytes, fulfilJson, primeShape } from "./fixtures/intercept";

/*
 * "Choose from Library" at every media input, through the one shared input: Home's picture and
 * recording inputs, a Try workflow's picture, Photo to 3D and the Transcription upload. The member
 * Library the app calls (GET /me/api/media, its download route and the signed link the download
 * route returns) is stubbed with two items; every other request is the fixture gateway's own.
 * Each case checks that the file the page sends is the Library item's own bytes. Screenshots of the
 * picker open and after a pick go to LIBPICK_SHOTS (default test-results/library-pick).
 */

const SHOTS = process.env.LIBPICK_SHOTS ?? path.resolve(__dirname, "../test-results/library-pick");
mkdirSync(SHOTS, { recursive: true });

// Real pictures (640x640, with content a person can judge) and a real 3 s audio clip, generated once and committed.
const fixture = (name: string) => readFileSync(path.resolve(__dirname, "fixtures/library", name));
const PNG = fixture("teal-fish.png");
const WAV = fixture("spoken-note.wav");

type Item = { id: string; kind: string; type: string; prompt: string; bytes: Buffer; seconds?: number; madeDaysAgo?: number };
const picture = (n: number, prompt: string, file: string): Item => ({
  id: `img_${String(n).repeat(64)}`,
  kind: "image",
  type: "image/png",
  prompt,
  bytes: fixture(file),
});
const recording = (n: number, prompt: string, madeDaysAgo: number): Item => ({
  id: `aud_${String(n).repeat(64)}`,
  kind: "audio",
  type: "audio/wav",
  prompt,
  bytes: WAV,
  // The Library's own length for the clip (the fixture runs 2.9 s).
  seconds: 3,
  madeDaysAgo,
});
const PICTURES = [
  picture(1, "A teal fish", "teal-fish.png"),
  picture(2, "Sunset over a reef", "sunset-reef.png"),
  picture(3, "Mountain lake at noon", "mountain-lake.png"),
  picture(4, "City after dark", "city-night.png"),
];
// Two memos share a first half and differ at the end, and one title is long enough to take two lines: a row has to
// show all of each, or the clips cannot be told apart.
const LONG_MEMO = "Voice memo: groceries and the dry cleaning by six";
const RECORDINGS = [
  recording(5, "A short spoken note", 1),
  recording(6, "Voice memo: groceries for Sunday dinner", 3),
  recording(7, "Voice memo: groceries for Monday lunch", 5),
  recording(8, LONG_MEMO, 9),
  recording(9, "Meeting recap", 14),
];
const ITEMS = [...PICTURES, ...RECORDINGS];
// The same four pictures again under new names, so the dialog fills and scrolls: a Library past one screen.
const manyPictures = (count: number): Item[] =>
  Array.from({ length: count }, (_, index) => ({
    ...PICTURES[index % PICTURES.length],
    id: `img_${(index + 16).toString(16).padStart(2, "0").repeat(32)}`,
    prompt: `${PICTURES[index % PICTURES.length].prompt}${index >= PICTURES.length ? ` ${Math.floor(index / PICTURES.length) + 1}` : ""}`,
  }));
const MANY_PICTURES = manyPictures(44);

const VIEWPORTS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

/** The Library as the member API serves it, for the kind the picker asks for. */
async function routeLibrary(page: Page, items: Item[] = ITEMS) {
  const listed: string[] = [];
  await page.route("**/me/api/media?**", (route) => {
    const url = new URL(route.request().url());
    listed.push(url.search);
    const kind = url.searchParams.get("kind");
    return fulfilJson(route, {
      assets: items.filter((item) => !kind || item.kind === kind).map((item) => ({
        asset_id: item.id,
        kind: item.kind,
        media_type: item.type,
        prompt: item.prompt,
        ...(item.seconds ? { duration_s: item.seconds } : {}),
        ...(item.madeDaysAgo !== undefined
          ? { created_at: Math.floor(Date.now() / 1000) - item.madeDaysAgo * 86_400 }
          : {}),
        available: true,
        thumbnail_url: `/signed/${item.id}`,
        download_url: `/signed/${item.id}`,
      })),
      sources: [],
      models: [],
      next_cursor: null,
    });
  });
  for (const item of items) {
    await page.route(`**/me/api/media/${item.id}/download`, (route) =>
      fulfilJson(route, { download_url: `/signed/${item.id}` }),
    );
    await page.route(`**/signed/${item.id}`, (route: Route) =>
      fulfilBytes(route, { contentType: item.type, body: item.bytes }),
    );
  }
  return listed;
}

/**
 * The Library dialog is fully open: it has stopped animating, nothing of the page shows through its surface, and
 * every picture in it has loaded. A capture taken before this shows a half-faded dialog.
 */
async function libraryOpen(page: Page) {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // Focus starts on the first tile, not on the close button.
  await expect(dialog.locator("ul button").first()).toBeFocused();
  // Measured at rest: the dialog scales in, and a box read mid-animation is not where it settles.
  await expect.poll(() => dialog.evaluate((el) => el.getAnimations({ subtree: true }).length)).toBe(0);
  await expect.poll(() => dialog.evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
  // The title is left-aligned, and the close button sits inside the dialog's padding, on the title's first line,
  // clear of the title.
  const title = (await dialog.locator('[data-slot="dialog-title"]').boundingBox())!;
  const close = (await dialog.getByRole("button", { name: "Close" }).boundingBox())!;
  const frame = (await dialog.boundingBox())!;
  expect(title.x).toBeLessThan(frame.x + 30);
  expect(title.x + title.width).toBeLessThanOrEqual(close.x);
  expect(close.x + close.width).toBeLessThanOrEqual(frame.x + frame.width - 20);
  expect(close.y).toBeGreaterThanOrEqual(frame.y + 8);
  expect(Math.abs(close.y + close.height / 2 - (title.y + 12))).toBeLessThan(5);
  // No subtitle: the title says what the dialog is.
  await expect(dialog.locator('[data-slot="dialog-description"]')).toHaveCount(0);
  await expect(dialog).not.toContainText("Newest first.");
  // The title wraps only where the words run out of room: it is not balanced (the page's headings are, which would
  // end each line a hundred pixels short of the close button), and a wrapped title's first line reaches to within a
  // word of it.
  const wrap = await dialog.locator('[data-slot="dialog-title"]').evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    // The text is several nodes; a line is the nodes sharing a top edge.
    const lines = new Map<number, number>();
    for (const rect of Array.from(range.getClientRects())) {
      const top = Math.round(rect.top);
      lines.set(top, (lines.get(top) ?? 0) + rect.width);
    }
    return { style: getComputedStyle(el).textWrapStyle, count: lines.size, longest: Math.max(...lines.values()) };
  });
  expect(wrap.style).not.toBe("balance");
  if (wrap.count > 1) expect(close.x - title.x - wrap.longest).toBeLessThan(100);
  // The surface is opaque: an rgb() colour, or an rgba() whose alpha is 1.
  const surface = await dialog.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(surface).not.toMatch(/rgba\(.*,\s*0?\.\d+\)$/);
  expect(surface).not.toBe("rgba(0, 0, 0, 0)");
  await expect
    .poll(() =>
      dialog.evaluate((el) =>
        Array.from(el.querySelectorAll("img")).every((img) => img.complete && img.naturalWidth > 0),
      ),
    )
    .toBe(true);
}

/** The dialog has finished closing: it is out of the page, so no fading ghost is left to capture. */
async function libraryClosed(page: Page) {
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

/**
 * Every site shows what it holds as the one shared summary: the Library's title as the main line (the file's real
 * name is only what is sent), where it came from with its type and size, and a single remove control named for the
 * title, with a 44 px hit area on a phone. The thumbnail, if it is a picture, has loaded.
 */
async function pickedSummary(
  page: Page,
  scope: Page | Locator,
  title: string,
  type: string,
  facts?: RegExp,
) {
  const summary = scope.locator('[data-slot="picked-summary"]');
  await expect(summary).toHaveCount(1);
  await expect(summary.locator("p > span").first()).toHaveText(title);
  // The name takes up to two lines, so a long title is not cut to one.
  const name = await summary.locator("p > span").first().evaluate((el) => ({
    lines: Math.round(el.clientHeight / parseFloat(getComputedStyle(el).lineHeight)),
  }));
  expect(name.lines).toBeLessThanOrEqual(2);
  await expect(summary).toContainText("From your Library");
  await expect(summary).toContainText(type);
  await expect(summary).not.toContainText("library-");
  // What the Library knew about the item (its length, for a clip) is in the facts line, between type and size.
  if (facts) await expect(summary).toContainText(facts);
  const remove = summary.getByRole("button", { name: `Remove ${title}` });
  const replace = summary.getByRole("button", { name: `Replace ${title}` });
  await expect(summary.getByRole("button")).toHaveCount(2);
  await expect(remove).toBeVisible();
  await expect(replace).toBeVisible();
  await expect(replace).toHaveText("Replace");
  const phone = (page.viewportSize()?.width ?? 1440) < 640;
  // The title column takes the free width. On a phone the controls sit under the facts, so the column is most of the
  // row (it was a quarter of it, with the controls beside it and the title cut to two words).
  if (phone) await expect.poll(async () => (await titleShare(summary)).share).toBeGreaterThanOrEqual(0.6);
  // Both controls are at least 40 x 40 CSS px at every width (44 on a phone), and they do not overlap.
  const minimum = phone ? 44 : 40;
  const boxes = [(await replace.boundingBox())!, (await remove.boundingBox())!];
  for (const box of boxes) {
    expect(box.width).toBeGreaterThanOrEqual(minimum);
    expect(box.height).toBeGreaterThanOrEqual(minimum);
  }
  expect(boxes[0].x + boxes[0].width).toBeLessThanOrEqual(boxes[1].x);
  // The row does not run past the screen.
  const frame = (await summary.boundingBox())!;
  expect(boxes[1].x + boxes[1].width).toBeLessThanOrEqual(frame.x + frame.width + 1);
  // On a phone the source and the facts sit on two lines, with no separator stranded between them.
  const separator = summary.locator("span.max-sm\\:hidden");
  if (phone) await expect(separator).toBeHidden();
  else await expect(separator).toBeVisible();
  await expect
    .poll(() =>
      summary.evaluate((el) => Array.from(el.querySelectorAll("img")).every((img) => img.complete && img.naturalWidth > 0)),
    )
    .toBe(true);
}

/** How much of the picked row the title column takes, and whether the title shows whole (at most two lines, none cut). */
async function titleShare(summary: Locator) {
  const row = (await summary.boundingBox())!;
  const text = (await summary.locator("p").boundingBox())!;
  const title = await summary.locator("p > span").first().evaluate((el) => ({
    lines: Math.round(el.clientHeight / parseFloat(getComputedStyle(el).lineHeight)),
    cut: el.scrollHeight > el.clientHeight + 1,
  }));
  return { share: text.width / row.width, row, text, ...title };
}

/** A solid-colour PNG, for a tile whose picture is as light or as dark as a picture gets. */
function solidPng(size: number, [red, green, blue]: [number, number, number]) {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (bytes: Buffer) => {
    let c = 0xffffffff;
    for (const byte of bytes) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc(body));
    return Buffer.concat([length, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 2, 0, 0, 0], 8);
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: size }, () => [red, green, blue]).flat())]);
  const pixels = deflateSync(Buffer.concat(Array.from({ length: size }, () => row)));
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", pixels),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** The Transcription page with a model that takes audio, and the Library stubbed. */
async function openTranscribe(page: Page, viewport: { width: number; height: number }) {
  await page.setViewportSize(viewport);
  // A catalog of one model, answered without a call to the gateway, so a slow gateway cannot hold the page on
  // "Loading models…". fulfilJson checks the shape against the real catalog; that shape is taken here, before
  // the page loads, so the check never waits on the gateway while the page does.
  await primeShape(testOrigin(test.info()), "GET", "/me/api/models/catalog");
  await page.route("**/me/api/models/catalog*", (route) =>
    fulfilJson(route, {
      aliases: [],
      models: [
        {
          id: "demo-transcribe",
          name: "demo-transcribe",
          capability: "transcribe",
          capabilities: [{ intent: "speech.transcription", input_modalities: ["audio"] }],
          locality: "local",
        },
      ],
    }),
  );
  const listed = await routeLibrary(page);
  await page.route("**/audio/transcriptions", (route) => fulfilJson(route, { text: "hello there" }));
  await page.goto("/try/?capability=transcribe");
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toContainText("Demo Transcribe");
  return listed;
}

/**
 * Audio is a list, one row per clip under a heading for the day it was made: the icon, the whole title (up to two
 * lines, never cut to one) and the length. Two clips whose titles start alike can be told apart, and a long title is
 * shown in full.
 */
async function audioList(page: Page) {
  const dialog = page.getByRole("dialog");
  for (const list of await dialog.locator("ul").all()) {
    expect(await list.evaluate((el) => getComputedStyle(el).display)).toBe("flex");
  }
  const rows = dialog.locator("ul button");
  await expect(rows).toHaveCount(RECORDINGS.length);
  const tops = new Set<number>();
  for (const recorded of RECORDINGS) {
    const row = dialog.getByRole("button", { name: recorded.prompt, exact: true });
    await expect(row.locator("svg")).toHaveCount(1);
    const title = row.locator("span.line-clamp-2");
    await expect(title).toHaveText(recorded.prompt);
    await expect(title).toHaveAttribute("title", recorded.prompt);
    // Not clipped: the text fits the lines it was given, and those are at most two.
    const fit = await title.evaluate((el) => ({
      clipped: el.scrollHeight > el.clientHeight + 1,
      lines: Math.round(el.clientHeight / parseFloat(getComputedStyle(el).lineHeight)),
    }));
    expect(fit.clipped, recorded.prompt).toBe(false);
    expect(fit.lines).toBeLessThanOrEqual(2);
    await expect(row).toContainText("0:03");
    await expect(row.locator("xpath=ancestor::section[1]/h3")).toHaveText(
      /^(Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|[A-Z][a-z]{2} \d{1,2})$/,
    );
    tops.add(Math.round((await row.boundingBox())!.y));
  }
  // One row per clip, one under another.
  expect(tops.size).toBe(RECORDINGS.length);
}

async function shot(page: Page, name: string) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

test.describe.configure({ mode: "default" });

for (const view of VIEWPORTS) {
  test(`Home: a 3D model's picture comes from the Library at ${view.name}px`, async ({ isolated }) => {
    const { page, context } = await isolated.member();
    await page.setViewportSize({ width: view.width, height: view.height });
    const real = await (await context.request.get("/me/api/home")).json();
    const template = real.capabilities.find((row: { icon_key: string }) => row.icon_key === "chat") ?? real.capabilities[0];
    await page.route("**/me/api/home", (route) =>
      fulfilJson(route, {
        ...real,
        capabilities: [
          {
            ...template,
            icon_key: "mesh",
            capability: "mesh",
            capability_title: "3D models",
            title: "Image to 3D",
            use_name: "mesh-model",
            recommended_model: "mesh-model",
            use_names: ["mesh-model"],
            intent: "mesh.generate",
            input_modalities: ["image"],
            required_input_modalities: ["image"],
            output_modalities: ["model"],
            input_words: { image: "picture" },
            action_label: "Make",
            recordable_input_modalities: [],
            cost_hint: { kind: "free", price_usd: null },
          },
        ],
      }),
    );
    const listed = await routeLibrary(page);
    await page.goto("/");
    await page.getByRole("group", { name: "What to make" }).getByRole("button", { name: /^3D models/ }).click();
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await expect(page.getByRole("heading", { name: "Choose a picture from your Library" })).toBeVisible();
    await page.getByRole("button", { name: "A teal fish" }).waitFor();
    await expect(page.getByRole("button", { name: "A short spoken note" })).toHaveCount(0);
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=image"))).toBe(true);
    await libraryOpen(page);
    // Each picture has its title as a caption under it (up to two lines, with the whole title on hover).
    const caption = page.getByRole("dialog").getByRole("button", { name: "A teal fish" }).locator("span.line-clamp-2");
    await expect(caption).toHaveText("A teal fish");
    await expect(caption).toHaveAttribute("title", "A teal fish");
    // The dialog is about 720 px wide on a desktop, with five tiles to a row from the lg breakpoint.
    const frame = (await page.getByRole("dialog").boundingBox())!;
    if (view.width >= 1024) {
      expect(frame.width).toBeGreaterThan(700);
      expect(frame.width).toBeLessThanOrEqual(721);
    }
    await shot(page, `home-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(page, page, "A teal fish", "PNG");
    // The dashed frame of the empty drop zone is gone: it would make the filled row look empty.
    const zone = page.locator('[data-slot="home-media-input"]');
    expect(await zone.evaluate((el) => getComputedStyle(el).borderTopStyle)).not.toBe("dashed");
    await shot(page, `home-picked-${view.name}`);
    await page.getByRole("button", { name: "Make", exact: true }).click();
    await expect(page).toHaveURL(/\/try\/\?capability=mesh/);
  });
}

for (const view of VIEWPORTS) {
  test(`Try workflow: the starting picture comes from the Library at ${view.name}px`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/models/catalog", (route) =>
      fulfilJson(route, { schema_version: 1, aliases: [], models: [], counts: {} }),
    );
    await page.route("**/me/api/workflows", (route) => fulfilJson(route, ltxCatalog));
    const listed = await routeLibrary(page);
    await page.route("**/v1/videos", (route) =>
      fulfilJson(route, { error: { message: "The gateway could not start this video." } }, { status: 400 }),
    );
    await page.goto("/try/?workflow=video.generate.ltx-2.5-i2v");
    const group = page.getByRole("group", { name: /Starting picture/ });
    // The helper text is for a person: no field name, no request body, no gateway description. An owner's request
    // details are folded away under "API details".
    const helperText = await group.evaluate((el) => (el as HTMLElement).innerText);
    expect(helperText).not.toMatch(/Writes|Request body|Media input|input_reference|data URI/);
    await group.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A teal fish" }).waitFor();
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=image"))).toBe(true);
    await libraryOpen(page);
    await shot(page, `try-workflow-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(page, group, "A teal fish", "PNG");
    await expect(group.getByRole("img", { name: "Chosen picture" })).toBeVisible();
    await page.getByRole("textbox", { name: /Describe the video/ }).fill("A teal fish swims past a reef.");
    await shot(page, `try-workflow-picked-${view.name}`);
    const request = page.waitForRequest("**/v1/videos");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    const body = (await request).postDataJSON() as Record<string, unknown>;
    expect(String(body.input_reference)).toBe(`data:image/png;base64,${PNG.toString("base64")}`);
  });
}

const meshCatalog = {
  schema_version: 1,
  object: "workflow.catalog",
  discovery_url: "/gateway/v1/workflows",
  workflows: [
    {
      id: "mesh.generate.trellis-2",
      title: "TRELLIS.2 image to 3D",
      description: "Turns one image into a textured 3D model in GLB format.",
      capability: "mesh",
      engine_binding: "trellis",
      availability: { available: true, status: "ready" },
      inputs: [
        { name: "image", type: "string", required: true, accepted: true, asset: true, media_type: "image/*", label: "picture" },
        { name: "resolution", type: "integer", enum: [512, 1024, 1536], default: 1024, accepted: true, label: "detail" },
      ],
      outputs: [{ modality: "mesh", media_type: "model/gltf-binary" }],
    },
  ],
};

for (const view of VIEWPORTS) {
  test(`Photo to 3D: the picture comes from the Library at ${view.name}px`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
    const listed = await routeLibrary(page);
    await page.route("**/v1/meshes/generate", (route) =>
      fulfilBytes(route, {
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { message: "The GPU is busy. Try again shortly." } }),
      }),
    );
    await page.goto("/try/?capability=mesh");
    if (view.width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
    else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
    // The field keeps its name: a group labelled "Picture" holds the controls.
    const field = page.getByRole("group", { name: "Picture", exact: true });
    await expect(field.getByRole("button", { name: "Choose a file" })).toBeVisible();
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A teal fish" }).waitFor();
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=image"))).toBe(true);
    await libraryOpen(page);
    await shot(page, `photo-to-3d-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(page, page, "A teal fish", "PNG");
    await shot(page, `photo-to-3d-picked-${view.name}`);
    const request = page.waitForRequest("**/v1/meshes/generate");
    await page.getByRole("button", { name: "Make 3D model" }).click();
    const posted = (await request).postDataBuffer() as Buffer;
    expect(posted.includes(Buffer.from('filename="library-picture.png"'))).toBe(true);
    expect(posted.includes(PNG)).toBe(true);
    await expect(page.getByText("The GPU is busy. Try again shortly.")).toBeVisible();
  });
}

for (const view of VIEWPORTS) {
  test(`Transcription: the audio file comes from the Library at ${view.name}px`, async ({ page }) => {
    const listed = await openTranscribe(page, view);
    // The field keeps its name: a group labelled "Audio file" holds the controls, as a workflow input's does.
    const field = page.getByRole("group", { name: "Audio file", exact: true });
    await expect(field.getByRole("button", { name: "Choose a file" })).toBeVisible();
    await expect(field.getByRole("button", { name: "Choose from Library" })).toBeVisible();
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A short spoken note" }).waitFor();
    await expect(page.getByRole("button", { name: "A teal fish" })).toHaveCount(0);
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=audio"))).toBe(true);
    await libraryOpen(page);
    await audioList(page);
    await shot(page, `transcribe-picker-${view.name}`);
    await page.getByRole("button", { name: "A short spoken note" }).click();
    await libraryClosed(page);
    await pickedSummary(page, page, "A short spoken note", "WAV", /WAV · 0:03 · \d[\d.]* [KM]B/);
    // Recording replaces the chosen file, and the page says so rather than leaving the Record button to surprise.
    await expect(page.getByText("Recording replaces the audio file above.")).toBeVisible();
    await shot(page, `transcribe-picked-${view.name}`);
    const request = page.waitForRequest("**/audio/transcriptions");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    const posted = (await request).postDataBuffer() as Buffer;
    expect(posted.includes(Buffer.from('filename="library-audio-clip.wav"'))).toBe(true);
    expect(posted.includes(WAV)).toBe(true);
  });
}

for (const view of VIEWPORTS) {
  test(`Transcription: Replace takes another clip in one step at ${view.name}px`, async ({ page }) => {
    await openTranscribe(page, view);
    // Nothing is chosen yet: no Replace, and no note about recording over a file.
    await expect(page.getByText("Recording replaces the audio file above.")).toHaveCount(0);
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    await page.getByRole("button", { name: "A short spoken note" }).click();
    await libraryClosed(page);
    await pickedSummary(page, page, "A short spoken note", "WAV");

    // Replace opens the same dialog; closing it leaves the choice as it was.
    await page.getByRole("button", { name: "Replace A short spoken note" }).click();
    await libraryOpen(page);
    await shot(page, `transcribe-replace-${view.name}`);
    await page.keyboard.press("Escape");
    await libraryClosed(page);
    await pickedSummary(page, page, "A short spoken note", "WAV");

    // Choosing another from it swaps the clip: the page holds the new one and sends its bytes.
    await page.getByRole("button", { name: "Replace A short spoken note" }).click();
    await libraryOpen(page);
    await page.getByRole("dialog").getByRole("button", { name: "Meeting recap", exact: true }).click();
    await libraryClosed(page);
    await pickedSummary(page, page, "Meeting recap", "WAV", /WAV · 0:03/);
    await expect(page.getByText("A short spoken note")).toHaveCount(0);
    await shot(page, `transcribe-replaced-${view.name}`);
    const request = page.waitForRequest("**/audio/transcriptions");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    expect(((await request).postDataBuffer() as Buffer).includes(WAV)).toBe(true);
  });
}

test("Transcription: a keyboard open puts a visible focus ring on the first clip", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  const trigger = page.getByRole("button", { name: "Choose from Library" });
  for (let presses = 0; presses < 60; presses += 1) {
    if (await trigger.evaluate((el) => el === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(trigger).toBeFocused();
  await page.keyboard.press("Enter");
  await libraryOpen(page);
  const first = page.getByRole("dialog").locator("ul button").first();
  await expect(first).toBeFocused();
  expect(await first.evaluate((el) => el.matches(":focus-visible"))).toBe(true);
  // The ring is a drawn shadow around the row, not just a state the browser knows about.
  const ring = await first.evaluate((el) => getComputedStyle(el).boxShadow);
  expect(ring).not.toBe("none");
  expect(ring).toMatch(/\d+px/);
  await shot(page, "transcribe-keyboard-focus-1440");
  // Enter on the focused clip picks it.
  await page.keyboard.press("Enter");
  await libraryClosed(page);
  await expect(page.locator('[data-slot="picked-summary"]')).toHaveCount(1);
});

for (const view of VIEWPORTS) {
  test(`A Library of ${MANY_PICTURES.length} pictures scrolls inside the dialog at ${view.name}px`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
    await routeLibrary(page, MANY_PICTURES);
    await page.goto("/try/?capability=mesh");
    if (view.width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
    else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    const dialog = page.getByRole("dialog");
    await expect(dialog.locator("ul button")).toHaveCount(MANY_PICTURES.length);
    const frame = (await dialog.boundingBox())!;
    const header = dialog.locator('[data-slot="library-picker-header"]');
    // At the top the header has no edge; once the tiles scroll under it, it has one.
    await expect(header).not.toHaveAttribute("data-scrolled");
    const edge = () => header.evaluate((el) => getComputedStyle(el).borderBottomColor);
    expect(await edge()).toBe("rgba(0, 0, 0, 0)");
    // Three tiles to a row on a phone, five on a desktop (lg and up).
    const firstRow = await dialog.locator("ul button").evaluateAll((nodes) => {
      const top = nodes[0].getBoundingClientRect().top;
      return nodes.filter((node) => Math.abs(node.getBoundingClientRect().top - top) < 2).length;
    });
    expect(firstRow).toBe(view.width >= 1024 ? 5 : 3);
    // At the desktop width every caption shows what makes it different: "City after dark 10" is whole, not cut to
    // "City after dar…", so it can be told from "… 11".
    if (view.width >= 1024) {
      expect(frame.width).toBeGreaterThan(700);
      expect(frame.width).toBeLessThanOrEqual(721);
      const clipped = await dialog.locator("ul button span.line-clamp-2").evaluateAll((nodes) =>
        nodes
          .filter((node) => node.scrollHeight > node.clientHeight + 1 || node.scrollWidth > node.clientWidth + 1)
          .map((node) => node.textContent),
      );
      expect(clipped).toEqual([]);
      await expect(dialog.getByRole("button", { name: "City after dark 10", exact: true }).locator("span.line-clamp-2")).toHaveText(
        "City after dark 10",
      );
    }
    await shot(page, `library-full-${view.name}`);
    // The dialog has a maximum height; the tiles scroll inside it while the header and its close button stay put.
    // A phone's sheet takes the screen's height (less a sliver of page above it) and sits on the bottom edge.
    if (view.width < 640) {
      expect(frame.height).toBeGreaterThan(view.height * 0.9);
      expect(frame.y + frame.height).toBeCloseTo(view.height, 0);
    } else {
      expect(frame.height).toBeLessThanOrEqual(view.height * 0.85 + 1);
      // The top edge is anchored at 12 % of the window, not centred.
      expect(Math.abs(frame.y - view.height * 0.12)).toBeLessThanOrEqual(1);
      expect(frame.y + frame.height).toBeLessThanOrEqual(view.height);
    }
    const body = dialog.locator('[data-slot="library-picker-body"]');
    expect(await body.evaluate((el) => el.scrollHeight > el.clientHeight + 100)).toBe(true);
    // The bottom edge shows while more of the list lies below, on the desktop dialog as on the phone sheet, and goes
    // at the end.
    await expect(body).toHaveAttribute("data-more-below", "true");
    // A fade into the dialog's surface, drawn over the tiles.
    const fade = dialog.locator('[data-slot="library-picker-fade"]');
    await expect(fade).toBeVisible();
    expect(await fade.evaluate((el) => getComputedStyle(el).backgroundImage)).toContain("gradient");
    if (view.width >= 640) await shot(page, "library-bottom-shadow-1440");
    const close = dialog.getByRole("button", { name: "Close" });
    const before = (await close.boundingBox())!;
    await body.evaluate((el) => el.scrollTo({ top: el.scrollHeight }));
    await expect.poll(() => body.evaluate((el) => Math.round(el.scrollTop + el.clientHeight) >= el.scrollHeight - 1)).toBe(true);
    await expect(close).toBeInViewport({ ratio: 1 });
    await expect(body).not.toHaveAttribute("data-more-below");
    await expect(fade).toBeHidden();
    await expect(header).toHaveAttribute("data-scrolled", "true");
    await expect.poll(edge).not.toBe("rgba(0, 0, 0, 0)");
    expect(await header.evaluate((el) => getComputedStyle(el).boxShadow)).not.toBe("none");
    const after = (await close.boundingBox())!;
    expect(after.y).toBe(before.y);
    expect((await dialog.boundingBox())!.y).toBe(frame.y);
    await expect(dialog.locator('[data-slot="dialog-title"]')).toBeInViewport({ ratio: 1 });
    await shot(page, `library-scrolled-${view.name}`);
  });
}

test("The Library dialog at 360px: the title clears the close button", async ({ page }) => {
  await openTranscribe(page, { width: 360, height: 740 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  await audioList(page);
  await shot(page, "transcribe-picker-360");
  await page.getByRole("button", { name: "A short spoken note" }).click();
  await libraryClosed(page);
  await pickedSummary(page, page, "A short spoken note", "WAV", /WAV · 0:03/);
  await shot(page, "transcribe-picked-360");
  // The long memo's title fits the picked row on a phone, over two lines at most.
  await page.getByRole("button", { name: "Replace A short spoken note" }).click();
  await libraryOpen(page);
  await page.getByRole("dialog").getByRole("button", { name: LONG_MEMO, exact: true }).click();
  await libraryClosed(page);
  await pickedSummary(page, page, LONG_MEMO, "WAV");
  await shot(page, "transcribe-picked-long-360");
});

// ---- Round 5 ----

/** Photo to 3D with the Library stubbed: the page whose picker lists pictures. */
async function openPhoto(page: Page, viewport: { width: number; height: number }, items: Item[] = ITEMS) {
  await page.setViewportSize(viewport);
  await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
  await routeLibrary(page, items);
  await page.goto("/try/?capability=mesh");
  if (viewport.width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
  else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
}

const channels = (colour: string) => (colour.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
function luminance(colour: string) {
  const [r, g, b] = channels(colour).map((value) => {
    const unit = value / 255;
    return unit <= 0.03928 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string) {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

/** The focus ring as the browser draws it: the gap colour and width, then the ring colour and its outer edge. */
function parseRing(boxShadow: string) {
  const layers = boxShadow.split(/,(?![^(]*\))/).map((layer) => layer.trim());
  const parsed = layers.map((layer) => ({
    colour: layer.match(/rgba?\([^)]*\)/)![0],
    spread: Number(layer.match(/(\d+(?:\.\d+)?)px\s*$/)?.[1] ?? 0),
  }));
  // Tailwind adds an empty (transparent) shadow layer after the ring; it is not part of the ring.
  const drawn = parsed.filter((layer) => !/rgba\(0, 0, 0, 0\)/.test(layer.colour));
  const gap = drawn[0];
  const ring = drawn[drawn.length - 1];
  return { gapColour: gap.colour, gapWidth: gap.spread, ringColour: ring.colour, ringWidth: ring.spread - gap.spread };
}

test("Photo to 3D: a keyboard open rings the first picture in the dark teal of Run", async ({ page }) => {
  await openPhoto(page, { width: 1440, height: 900 });
  const trigger = page.getByRole("button", { name: "Choose from Library" });
  for (let presses = 0; presses < 80; presses += 1) {
    if (await trigger.evaluate((el) => el === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(trigger).toBeFocused();
  await page.keyboard.press("Enter");
  await libraryOpen(page);
  const dialog = page.getByRole("dialog");
  const tile = dialog.locator("ul button").first();
  await expect(tile).toBeFocused();
  const ring = parseRing(await tile.evaluate((el) => getComputedStyle(el).boxShadow));
  expect(ring.ringWidth).toBe(2);
  expect(ring.gapWidth).toBe(2);
  // The ring is the colour of Run, and it reads against the dialog it sits on.
  const run = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--primary").trim());
  expect(run).toBe("#0d4552");
  expect(channels(ring.ringColour)).toEqual([13, 69, 82]);
  const surface = await dialog.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(contrast(ring.ringColour, surface)).toBeGreaterThanOrEqual(3);
  // The gap is the dialog's own colour, so the ring does not bleed into the tile.
  expect(ring.gapColour).toBe(surface);
  // The tile's label starts well inside the ring: the ring's inner edge is 2 px outside the tile, and the first letter
  // is at least 6 px inside it, so 8 px apart.
  const labelGap = await tile.evaluate((el) => {
    const text = el.querySelector("span.line-clamp-2")!.firstChild as Text;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 1);
    return range.getBoundingClientRect().left - (el.getBoundingClientRect().left - 2);
  });
  expect(labelGap).toBeGreaterThanOrEqual(8);
  await shot(page, "photo-to-3d-keyboard-focus-1440");
  // Tab on to a second tile and back: the ring follows focus, one at a time.
  await page.keyboard.press("Tab");
  await expect(dialog.locator("ul button").nth(1)).toBeFocused();
});

test("Transcription: the rows' hover fill is the scroll-edge tone, a pointer, and the ring clears 3:1 against it", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  const dialog = page.getByRole("dialog");
  const row = dialog.getByRole("button", { name: "Meeting recap", exact: true });
  const surface = await dialog.evaluate((el) => getComputedStyle(el).backgroundColor);
  await row.hover();
  await expect.poll(() => row.evaluate((el) => getComputedStyle(el).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)");
  // Wait until the hover transition has finished, so the fill read is the settled one.
  await expect.poll(() => row.evaluate((el) => el.getAnimations().length)).toBe(0);
  const fill = await row.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(contrast(fill, surface)).toBeGreaterThanOrEqual(1.2);
  expect(await row.evaluate((el) => getComputedStyle(el).cursor)).toBe("pointer");
  expect(await dialog.getByRole("button", { name: "Close" }).evaluate((el) => getComputedStyle(el).cursor)).toBe("pointer");
  // Focus the same row by keyboard: its ring against the fill it now sits on, and against the dialog.
  await row.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(row).toBeFocused();
  const ring = parseRing(await row.evaluate((el) => getComputedStyle(el).boxShadow));
  expect(ring.ringWidth).toBe(2);
  expect(contrast(ring.ringColour, fill)).toBeGreaterThanOrEqual(3);
  expect(contrast(ring.ringColour, surface)).toBeGreaterThanOrEqual(3);
  await shot(page, "transcribe-row-hover-focus-1440");
});

for (const view of VIEWPORTS) {
  test(`Photo to 3D: Replace marks, scrolls to and focuses the current picture at ${view.name}px`, async ({ page }) => {
    await openPhoto(page, view, MANY_PICTURES);
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    // A picture far down the list, so reopening has to scroll to it.
    const far = MANY_PICTURES[MANY_PICTURES.length - 1].prompt;
    await page.getByRole("dialog").getByRole("button", { name: far, exact: true }).scrollIntoViewIfNeeded();
    await page.getByRole("dialog").getByRole("button", { name: far, exact: true }).click();
    await libraryClosed(page);
    const replace = page.getByRole("button", { name: `Replace ${far}` });
    await expect(replace).toBeFocused();
    await replace.click();
    const dialog = page.getByRole("dialog");
    const current = dialog.getByRole("button", { name: far, exact: true });
    await expect(current).toHaveAttribute("aria-current", "true");
    await expect(current.locator('[data-slot="library-current-mark"]')).toBeVisible();
    await expect(current).toBeFocused();
    await expect(current).toBeInViewport({ ratio: 1 });
    await expect(dialog.locator('[aria-current="true"]')).toHaveCount(1);
    await expect.poll(() => dialog.evaluate((el) => el.getAnimations({ subtree: true }).length)).toBe(0);
    await shot(page, `photo-to-3d-replace-current-${view.name}`);
  });

  test(`Photo to 3D: the picker's search filters by title and says when nothing matches at ${view.name}px`, async ({ page }) => {
    await openPhoto(page, view);
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    const dialog = page.getByRole("dialog");
    const search = dialog.getByRole("searchbox", { name: "Search your pictures" });
    await expect(search).toBeVisible();
    // Keyboard-reachable: Shift+Tab from the first picture lands in the search field.
    await page.keyboard.press("Shift+Tab");
    await expect(search).toBeFocused();
    // The field's ring is the shared one (2 px, the dark teal of Run, a 2 px gap), and its edge does not also turn to
    // the lighter teal: one focus indicator, not two.
    const ring = parseRing(await search.evaluate((el) => getComputedStyle(el).boxShadow));
    expect(ring.ringWidth).toBe(2);
    expect(ring.gapWidth).toBe(2);
    expect(channels(ring.ringColour)).toEqual([13, 69, 82]);
    const surface = await dialog.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(contrast(ring.ringColour, surface)).toBeGreaterThanOrEqual(3);
    const focusedEdge = await search.evaluate((el) => getComputedStyle(el).borderTopColor);
    await search.evaluate((el) => (el as HTMLInputElement).blur());
    expect(await search.evaluate((el) => getComputedStyle(el).borderTopColor)).toBe(focusedEdge);
    await search.focus();
    await expect(dialog.getByRole("button", { name: "Clear search" })).toHaveCount(0);
    // The dialog's top edge does not move as the results shrink and grow back.
    const top = (await dialog.boundingBox())!;
    await search.fill("reef");
    await expect(dialog.locator("ul button")).toHaveCount(1);
    expect((await dialog.boundingBox())!.y).toBe(top.y);
    if (view.width >= 1024) expect(Math.abs(top.y - view.height * 0.12)).toBeLessThanOrEqual(1);
    // Our own clear button, in the palette, a 40 px target (44 on a phone), takes the place of the browser's blue one.
    const clear = dialog.getByRole("button", { name: "Clear search" });
    await expect(clear).toBeVisible();
    const clearBox = (await clear.boundingBox())!;
    const minimum = view.width < 640 ? 44 : 40;
    expect(clearBox.width).toBeGreaterThanOrEqual(minimum);
    expect(clearBox.height).toBeGreaterThanOrEqual(minimum);
    // The browser's own clear button is switched off by a rule for its pseudo-element (getComputedStyle does not report
    // a pseudo-element of a form control, so the rule itself is read).
    const switchedOff = await page.evaluate(() => {
      const found: string[] = [];
      const walk = (rules: CSSRuleList) => {
        for (const rule of Array.from(rules)) {
          const styled = rule as CSSStyleRule;
          if (styled.selectorText?.includes("::-webkit-search-cancel-button") && styled.style.getPropertyValue("appearance") === "none") {
            found.push(styled.selectorText);
          }
          if ("cssRules" in rule) walk((rule as CSSGroupingRule).cssRules);
        }
      };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          walk(sheet.cssRules);
        } catch {
          // A sheet from another origin is not readable.
        }
      }
      return found;
    });
    expect(switchedOff.length).toBeGreaterThan(0);
    expect(await search.evaluate((el) => (el as HTMLInputElement).matches("[class*='webkit-search-cancel-button']"))).toBe(true);
    expect(await clear.evaluate((el) => getComputedStyle(el).color)).toBe(await dialog.getByRole("button", { name: "Close" }).evaluate((el) => getComputedStyle(el).color));
    await shot(page, `library-search-focus-${view.name}`);
    await expect(dialog.getByRole("button", { name: "Sunset over a reef", exact: true })).toBeVisible();
    await shot(page, `library-search-results-${view.name}`);
    await search.fill("zebra");
    await expect(dialog.locator("ul button")).toHaveCount(0);
    await expect(dialog.getByRole("status")).toHaveText("No pictures match “zebra”.");
    expect((await dialog.boundingBox())!.y).toBe(top.y);
    await shot(page, `library-search-empty-${view.name}`);
    // Clear search empties the field, brings every picture back and keeps focus in the field.
    await dialog.getByRole("button", { name: "Clear search" }).click();
    await expect(search).toHaveValue("");
    await expect(search).toBeFocused();
    await expect(dialog.getByRole("button", { name: "Clear search" })).toHaveCount(0);
    await expect(dialog.locator("ul button")).toHaveCount(PICTURES.length);
    expect((await dialog.boundingBox())!.y).toBe(top.y);
  });
}

test("Transcription: a 30 MB audio file is taken and sent", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.locator("input[data-media-file-input]").setInputFiles({
    name: "long-talk.wav",
    mimeType: "audio/wav",
    buffer: Buffer.concat([WAV, Buffer.alloc(30 * 1024 * 1024)]),
  });
  await expect(page.getByRole("button", { name: "Remove long-talk.wav" })).toBeVisible();
  // Next's own route announcer is an empty alert; the picker's error would carry words.
  await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
  const request = page.waitForRequest("**/audio/transcriptions");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  expect(((await request).postDataBuffer() as Buffer).length).toBeGreaterThan(30 * 1024 * 1024);
});

test("Transcription: focus goes to Replace after a pick and to Choose a file after Remove, never on arrival", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await expect(page.locator("body")).toBeFocused();
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  await page.getByRole("button", { name: "A short spoken note" }).click();
  await libraryClosed(page);
  await expect(page.getByRole("button", { name: "Replace A short spoken note" })).toBeFocused();
  await page.getByRole("button", { name: "Remove A short spoken note" }).click();
  await expect(page.getByRole("button", { name: "Choose a file" })).toBeFocused();
});

for (const width of [360, 390]) {
  test(`A Library item picked on a ${width}px phone: the title has the row's width and Replace and Remove sit under the facts`, async ({ page }) => {
    await openTranscribe(page, { width, height: 844 });
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    await page.getByRole("dialog").getByRole("button", { name: LONG_MEMO, exact: true }).click();
    await libraryClosed(page);
    const summary = page.locator('[data-slot="picked-summary"]');
    const thumb = (await summary.locator("> div > div").first().boundingBox())!;
    const { text, row, share, lines, cut } = await titleShare(summary);
    const name = (await summary.locator("p > span").first().boundingBox())!;
    const replace = (await summary.getByRole("button", { name: `Replace ${LONG_MEMO}` }).boundingBox())!;
    const remove = (await summary.getByRole("button", { name: `Remove ${LONG_MEMO}` }).boundingBox())!;
    // The thumbnail and the name start on the same line.
    expect(Math.abs(thumb.y - name.y)).toBeLessThanOrEqual(4);
    // The title column is most of the row, not a third of it, so three memos that start alike can be told apart.
    expect(share).toBeGreaterThanOrEqual(0.6);
    expect(text.width).toBeGreaterThanOrEqual(row.width - thumb.width - 12 - 1);
    // The title shows all of its words, over two lines, none cut by an ellipsis.
    expect(cut).toBe(false);
    expect(lines).toBe(2);
    // The controls are a group under the facts, in the title's column, side by side; Replace's label lines up with the
    // title's left edge (the button has 12 px of padding inside it).
    expect(replace.y).toBeGreaterThanOrEqual(text.y + text.height - 1);
    expect(Math.abs(replace.x + 12 - text.x)).toBeLessThanOrEqual(2);
    expect(remove.x).toBeGreaterThan(replace.x);
    expect(Math.abs(replace.y - remove.y)).toBeLessThanOrEqual(2);
    expect(remove.x + remove.width).toBeLessThanOrEqual(row.x + row.width + 1);
    // "From your Library" is one line, not wrapped.
    const source = await summary.getByText("From your Library").evaluate((el) => el.getBoundingClientRect().height);
    expect(source).toBeLessThan(24);
    await shot(page, `transcribe-picked-long-${width}`);
  });
}

test("Transcription: at 1440px the controls sit on the right of the title, and it has the rest of the row", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  await page.getByRole("dialog").getByRole("button", { name: LONG_MEMO, exact: true }).click();
  await libraryClosed(page);
  const summary = page.locator('[data-slot="picked-summary"]');
  const { text, row } = await titleShare(summary);
  const replace = (await summary.getByRole("button", { name: `Replace ${LONG_MEMO}` }).boundingBox())!;
  const remove = (await summary.getByRole("button", { name: `Remove ${LONG_MEMO}` }).boundingBox())!;
  // The group is beside the text, on its row; the text runs up to it.
  expect(replace.x).toBeGreaterThanOrEqual(text.x + text.width - 1);
  expect(replace.y).toBeLessThan(text.y + text.height);
  expect(remove.x + remove.width).toBeLessThanOrEqual(row.x + row.width + 1);
  expect(Math.abs(replace.y - remove.y)).toBeLessThanOrEqual(2);
});

// ---- Round 6 ----


/**
 * Reads pixels off a screenshot of the page: each point (in CSS pixels) becomes the "rgb(r, g, b)" drawn there, so the
 * contrast is measured on what a person sees, not on what the styles say.
 */
async function pixelsAt(page: Page, points: { x: number; y: number }[]) {
  const shotPng = await page.screenshot();
  const viewport = page.viewportSize()!;
  return page.evaluate(
    async ({ base64, points: wanted, width }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${base64}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext("2d")!;
      context.drawImage(image, 0, 0);
      const scale = image.naturalWidth / width;
      return wanted.map(({ x, y }) => Array.from(context.getImageData(Math.round(x * scale), Math.round(y * scale), 1, 1).data.slice(0, 3)));
    },
    { base64: shotPng.toString("base64"), points, width: viewport.width },
  );
}

test("Photo to 3D: the current-pick badge reads against a light and a dark picture, and its tile has a dark teal border", async ({ page }) => {
  const light: Item = { id: `img_${"a".repeat(64)}`, kind: "image", type: "image/png", prompt: "Pale beach", bytes: solidPng(64, [250, 250, 245]) };
  const dark: Item = { id: `img_${"b".repeat(64)}`, kind: "image", type: "image/png", prompt: "Night sea", bytes: solidPng(64, [10, 12, 20]) };
  await openPhoto(page, { width: 1440, height: 900 }, [light, dark]);
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  // Each pass takes a picture and reopens the Library over it (Replace), which leaves the dialog open for the next.
  for (const [item, tone] of [
    [dark, "dark"],
    [light, "light"],
  ] as const) {
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: item.prompt, exact: true }).click();
    await libraryClosed(page);
    await page.getByRole("button", { name: `Replace ${item.prompt}` }).click();
    // Replace opens on the current picture (it takes focus), not on the first.
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: item.prompt, exact: true })).toBeFocused();
    await expect.poll(() => dialog.evaluate((el) => el.getAnimations({ subtree: true }).length)).toBe(0);
    await expect.poll(() => dialog.evaluate((el) => Array.from(el.querySelectorAll("img")).every((img) => img.complete && img.naturalWidth > 0))).toBe(true);
    const tile = dialog.getByRole("button", { name: item.prompt, exact: true });
    const mark = tile.locator('[data-slot="library-current-mark"]');
    await expect(mark).toBeVisible();
    // The selected tile itself is marked: a 2 px dark teal border all round it, in Run's colour.
    const frame = tile.locator("span").first();
    const edge = await frame.evaluate((el) => {
      const style = getComputedStyle(el);
      return { width: style.borderTopWidth, colour: style.borderTopColor };
    });
    expect(edge.width).toBe("2px");
    expect(channels(edge.colour)).toEqual([13, 69, 82]);
    const surface = await dialog.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(contrast(edge.colour, surface)).toBeGreaterThanOrEqual(3);
    // The badge's pixels: the disc (above the check mark), the ring just outside it, and the picture beside the ring.
    const box = (await mark.boundingBox())!;
    const middle = box.y + box.height / 2;
    const [disc, ring, picture] = (
      await pixelsAt(page, [
        { x: box.x + box.width / 2, y: box.y + 2 },
        { x: box.x - 1, y: middle },
        { x: box.x - 3.5, y: middle },
      ])
    ).map((pixel) => `rgb(${pixel.join(", ")})`);
    expect(contrast(disc, ring), `${tone} tile: disc against its ring`).toBeGreaterThanOrEqual(3);
    // Against the picture the badge shows as its ring on a dark one, and as its disc on a light one.
    expect(Math.max(contrast(ring, picture), contrast(disc, picture)), `${tone} tile: badge against the picture`).toBeGreaterThanOrEqual(3);
    if (tone === "dark") expect(contrast(ring, picture)).toBeGreaterThanOrEqual(3);
    if (tone === "light") expect(contrast(disc, picture)).toBeGreaterThanOrEqual(3);
    // The disc is the dark teal, the ring is white.
    expect(channels(disc)).toEqual([13, 69, 82]);
    expect(channels(ring)).toEqual([255, 255, 255]);
    await shot(page, `current-badge-${tone}-1440`);
  }
});

for (const target of [
  { name: "a picture tile", width: 1440, height: 900 },
  { name: "a picture tile on a phone", width: 390, height: 844 },
] as const) {
  test(`A double-click on ${target.name} picks it once, and the pick survives the second click`, async ({ page }) => {
    await openPhoto(page, target);
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    await page.getByRole("dialog").getByRole("button", { name: "A teal fish", exact: true }).dblclick();
    await libraryClosed(page);
    const summary = page.locator('[data-slot="picked-summary"]');
    await expect(summary).toHaveCount(1);
    // Once the pick has finished adding, it is still held: the second click took nothing away.
    await expect(page.getByRole("status").filter({ hasText: /^Adding / })).toHaveCount(0);
    await expect(summary).toHaveCount(1);
    await expect(summary.locator("p > span").first()).toHaveText("A teal fish");
    await expect(page.getByRole("button", { name: "Choose a file" })).toHaveCount(0);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await shot(page, `double-click-after-${target.width}`);
  });
}

test("A double-click on a clip row picks it once and the pick survives (Transcription)", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  await page.getByRole("dialog").getByRole("button", { name: "A short spoken note", exact: true }).dblclick();
  await libraryClosed(page);
  await expect(page.getByRole("status").filter({ hasText: /^Adding / })).toHaveCount(0);
  await expect(page.locator('[data-slot="picked-summary"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Remove A short spoken note" })).toBeVisible();
});

test("A click on Remove on the very spot of the tile click, at once, is the tail of a double-click and is ignored; a click elsewhere removes it", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  const tile = page.getByRole("dialog").getByRole("button", { name: "A short spoken note", exact: true });
  const box = (await tile.boundingBox())!;
  const spot = { x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2) };
  // The second click of a double-click: a mouse click (detail 2) at the tile's spot, delivered the instant the Remove
  // control exists, which is inside the window however slow the machine.
  const stray = page.evaluate(async ({ x, y }) => {
    const find = () => document.querySelector<HTMLElement>('button[aria-label="Remove A short spoken note"]');
    while (!find()) await new Promise((resolve) => requestAnimationFrame(resolve));
    find()!.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 2, clientX: x, clientY: y }));
    return true;
  }, spot);
  await page.mouse.click(spot.x, spot.y);
  expect(await stray).toBe(true);
  await libraryClosed(page);
  const summary = page.locator('[data-slot="picked-summary"]');
  await expect(summary).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Choose a file" })).toHaveCount(0);
  // A click on Remove from where the pointer really is now (not the tile's spot) is the person's own, with no wait.
  await page.getByRole("button", { name: "Remove A short spoken note" }).click();
  await expect(summary).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Choose a file" })).toBeFocused();
});

test("A key press on Remove right after a pick removes it", async ({ page }) => {
  await openTranscribe(page, { width: 1440, height: 900 });
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  await page.getByRole("dialog").getByRole("button", { name: "A short spoken note", exact: true }).click();
  await libraryClosed(page);
  const replace = page.getByRole("button", { name: "Replace A short spoken note" });
  await expect(replace).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Remove A short spoken note" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-slot="picked-summary"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Choose a file" })).toBeFocused();
});

for (const [width, inline] of [
  [434, true],
  [420, true],
  [400, false],
] as const) {
  test(`A picked row ${width}px wide keeps Replace and Remove ${inline ? "beside the title" : "under the facts"}`, async ({ page }) => {
    await openTranscribe(page, { width: 1440, height: 900 });
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    await page.getByRole("dialog").getByRole("button", { name: LONG_MEMO, exact: true }).click();
    await libraryClosed(page);
    const summary = page.locator('[data-slot="picked-summary"]');
    // The row answers to its own width: pin it to a width (the desktop Workflow field is about 434 px).
    await summary.evaluate((el, pinned) => {
      el.style.width = `${pinned}px`;
      el.style.maxWidth = `${pinned}px`;
    }, width);
    await expect.poll(async () => Math.round((await summary.boundingBox())!.width)).toBe(width);
    const { text, row } = await titleShare(summary);
    const replace = (await summary.getByRole("button", { name: `Replace ${LONG_MEMO}` }).boundingBox())!;
    const remove = (await summary.getByRole("button", { name: `Remove ${LONG_MEMO}` }).boundingBox())!;
    expect(Math.abs(replace.y - remove.y)).toBeLessThanOrEqual(2);
    expect(remove.x + remove.width).toBeLessThanOrEqual(row.x + row.width + 1);
    if (inline) {
      expect(replace.x).toBeGreaterThanOrEqual(text.x + text.width - 1);
      expect(replace.y).toBeLessThan(text.y + text.height);
    } else {
      expect(replace.y).toBeGreaterThanOrEqual(text.y + text.height - 1);
    }
  });
}

test("The desktop dialog's top edge stays where it is while a search shrinks the list, and its bottom moves instead", async ({ page }) => {
  await openPhoto(page, { width: 1440, height: 900 }, MANY_PICTURES);
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  const dialog = page.getByRole("dialog");
  const search = dialog.getByRole("searchbox", { name: "Search your pictures" });
  const full = (await dialog.boundingBox())!;
  const searchTop = (await search.boundingBox())!.y;
  expect(Math.abs(full.y - 108)).toBeLessThanOrEqual(1);
  const tops: number[] = [];
  for (const [text, tiles] of [
    ["reef", 11],
    ["Sunset over a reef 2", 1],
    ["zebra", 0],
    ["", MANY_PICTURES.length],
  ] as const) {
    await search.fill(text);
    await expect(dialog.locator("ul button")).toHaveCount(tiles);
    const box = (await dialog.boundingBox())!;
    tops.push(box.y);
    // The search field itself does not move either.
    expect((await search.boundingBox())!.y).toBe(searchTop);
    if (text === "zebra") expect(box.height).toBeLessThan(full.height - 200);
    if (text === "") expect(box.height).toBeCloseTo(full.height, -1);
  }
  expect(new Set(tops.map(Math.round)).size).toBe(1);
  expect(Math.round(tops[0])).toBe(Math.round(full.y));
});

for (const view of VIEWPORTS) {
  test(`small days share a row of the one grid on a wide screen, and each day has its own block on a phone, at ${view.name}px`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
    // Two pictures today and one yesterday fit one row on a wide screen; four the day before start the next row.
    const ages = [0, 0, 1, 2, 2, 2, 2];
    await routeLibrary(page, ages.map((age, index) => ({ ...MANY_PICTURES[index], madeDaysAgo: age })));
    await page.goto("/try/?capability=mesh");
    if (view.width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
    else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    const sections = page.getByRole("dialog").locator("section[data-day]");
    await expect(sections).toHaveCount(3);
    const box = async (index: number) => (await sections.nth(index).boundingBox())!;
    const [today, yesterday, before] = [await box(0), await box(1), await box(2)];
    if (view.width >= 640) {
      // Today and yesterday side by side, each under its own heading; the third day starts the next row.
      expect(Math.abs(today.y - yesterday.y)).toBeLessThanOrEqual(1);
      expect(yesterday.x).toBeGreaterThan(today.x + today.width);
    } else {
      // A phone, as in the Google Photos picker: every day is a full-width block under the one before.
      expect(yesterday.y).toBeGreaterThan(today.y + today.height);
      expect(Math.abs(yesterday.x - today.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(yesterday.width - today.width)).toBeLessThanOrEqual(1);
    }
    expect(before.y).toBeGreaterThan(today.y + today.height);
    expect(Math.abs(before.x - today.x)).toBeLessThanOrEqual(1);
    // A day's name starts on the edge of its first picture, as the audio rows' names do on their icons.
    const firstTile = (await sections.nth(0).locator("ul button").first().boundingBox())!;
    const headingText = await sections.nth(0).locator("h3 time").boundingBox();
    expect(Math.abs(headingText!.x - firstTile.x)).toBeLessThanOrEqual(1);
    // Every tile has the one grid's width, and the columns line up across days.
    const tiles = await page.getByRole("dialog").locator("section[data-day] ul button").evaluateAll((nodes) =>
      nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        return { x: Math.round(rect.x), width: Math.round(rect.width) };
      }),
    );
    expect(new Set(tiles.map((tile) => tile.width)).size).toBe(1);
    const columns = new Set(tiles.map((tile) => tile.x));
    expect(columns.size).toBe(view.width >= 1024 ? 4 : 3);
  });
}

test("a day's heading stays at the top of the scroll area while its pictures pass under it", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
  // Four days, the third with enough pictures to run past the top of the scroll area.
  const ages = [0, 0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 9, 9, 9];
  await routeLibrary(page, ages.map((age, index) => ({ ...MANY_PICTURES[index], madeDaysAgo: age })));
  await page.goto("/try/?capability=mesh");
  await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
  await page.getByRole("button", { name: "Choose from Library" }).click();
  await libraryOpen(page);
  const dialog = page.getByRole("dialog");
  const body = dialog.locator('[data-slot="library-picker-body"]');
  const header = dialog.locator('[data-slot="library-picker-header"]');
  await expect(dialog.locator("h3[data-stuck]")).toHaveCount(0);
  // Scrolled into the third day's pictures: its heading is pinned at the top, with the edge under it.
  const third = dialog.locator("section[data-day]").nth(2);
  await body.evaluate((el, top) => el.scrollTo(0, top), await third.evaluate((el) => (el as HTMLElement).offsetTop + 120));
  const pinned = third.locator("h3");
  await expect(pinned).toHaveAttribute("data-stuck", "true");
  await expect(dialog.locator("h3[data-stuck]")).toHaveCount(1);
  const bodyTop = (await body.boundingBox())!.y;
  expect(Math.abs((await pinned.boundingBox())!.y - bodyTop)).toBeLessThanOrEqual(1);
  expect(await pinned.evaluate((el) => getComputedStyle(el).borderBottomColor)).not.toBe("rgba(0, 0, 0, 0)");
  // The fade under it starts below the hairline, so it does not paint over it.
  expect(await pinned.evaluate((el) => parseFloat(getComputedStyle(el, "::after").top))).toBeGreaterThanOrEqual(
    await pinned.evaluate((el) => el.clientHeight + 1),
  );
  // The heading's surface is opaque, and the header does not draw a second edge above it.
  expect(await pinned.evaluate((el) => getComputedStyle(el).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)");
  expect(await header.evaluate((el) => getComputedStyle(el).borderBottomColor)).toBe("rgba(0, 0, 0, 0)");
});
