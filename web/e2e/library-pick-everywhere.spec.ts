import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import type { Locator, Page, Route } from "@playwright/test";
import ltxCatalog from "../tests/fixtures/workflows-ltx-i2v.json";
import { expect, test } from "./fixtures/isolation";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";

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

type Item = { id: string; kind: string; type: string; prompt: string; bytes: Buffer };
const picture = (n: number, prompt: string, file: string): Item => ({
  id: `img_${String(n).repeat(64)}`,
  kind: "image",
  type: "image/png",
  prompt,
  bytes: fixture(file),
});
const recording = (n: number, prompt: string): Item => ({
  id: `aud_${String(n).repeat(64)}`,
  kind: "audio",
  type: "audio/wav",
  prompt,
  bytes: WAV,
});
const PICTURES = [
  picture(1, "A teal fish", "teal-fish.png"),
  picture(2, "Sunset over a reef", "sunset-reef.png"),
  picture(3, "Mountain lake at noon", "mountain-lake.png"),
  picture(4, "City after dark", "city-night.png"),
];
const RECORDINGS = [
  recording(5, "A short spoken note"),
  recording(6, "Voice memo: groceries"),
  recording(7, "Meeting recap"),
];
const ITEMS = [...PICTURES, ...RECORDINGS];
// The same four pictures again under new names, so the dialog fills and scrolls.
const MANY_PICTURES = Array.from({ length: 14 }, (_, index) => ({
  ...PICTURES[index % PICTURES.length],
  id: `img_${(index + 10).toString(16).padStart(2, "0").repeat(32)}`,
  prompt: `${PICTURES[index % PICTURES.length].prompt}${index >= PICTURES.length ? ` ${Math.floor(index / PICTURES.length) + 1}` : ""}`,
}));

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
  await expect.poll(() => dialog.evaluate((el) => el.getAnimations({ subtree: true }).length)).toBe(0);
  await expect.poll(() => dialog.evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
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
 * Every site shows what it holds as the one shared summary: the file's name, where it came from, and a single
 * remove control. The thumbnail, if it is a picture, has loaded.
 */
async function pickedSummary(scope: Page | Locator, name: string) {
  const summary = scope.locator('[data-slot="picked-summary"]');
  await expect(summary).toHaveCount(1);
  await expect(summary).toContainText(name);
  await expect(summary).toContainText("From your Library");
  await expect(summary.getByRole("button")).toHaveCount(1);
  await expect
    .poll(() =>
      summary.evaluate((el) => Array.from(el.querySelectorAll("img")).every((img) => img.complete && img.naturalWidth > 0)),
    )
    .toBe(true);
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
    await shot(page, `home-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(page, "library-picture.png");
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
    await group.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A teal fish" }).waitFor();
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=image"))).toBe(true);
    await libraryOpen(page);
    await shot(page, `try-workflow-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(group, "library-picture.png");
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
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A teal fish" }).waitFor();
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=image"))).toBe(true);
    await libraryOpen(page);
    await shot(page, `photo-to-3d-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await libraryClosed(page);
    await pickedSummary(page, "library-picture.png");
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
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/models/catalog*", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await fulfilJson(
        route,
        {
          ...body,
          models: [
            ...(body.models ?? []),
            {
              id: "demo-transcribe",
              name: "demo-transcribe",
              capability: "transcribe",
              capabilities: [{ intent: "speech.transcription", input_modalities: ["audio"] }],
              locality: "local",
            },
          ],
        },
        { response },
      );
    });
    const listed = await routeLibrary(page);
    await page.route("**/audio/transcriptions", (route) => fulfilJson(route, { text: "hello there" }));
    await page.goto("/try/?capability=transcribe");
    await expect(page.getByRole("combobox", { name: "Model", exact: true })).toContainText("Demo Transcribe");
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A short spoken note" }).waitFor();
    await expect(page.getByRole("button", { name: "A teal fish" })).toHaveCount(0);
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=audio"))).toBe(true);
    await libraryOpen(page);
    await shot(page, `transcribe-picker-${view.name}`);
    await page.getByRole("button", { name: "A short spoken note" }).click();
    await libraryClosed(page);
    await pickedSummary(page, "library-audio-clip.wav");
    await shot(page, `transcribe-picked-${view.name}`);
    const request = page.waitForRequest("**/audio/transcriptions");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    const posted = (await request).postDataBuffer() as Buffer;
    expect(posted.includes(Buffer.from('filename="library-audio-clip.wav"'))).toBe(true);
    expect(posted.includes(WAV)).toBe(true);
  });
}

for (const view of VIEWPORTS) {
  test(`The Library dialog full of pictures at ${view.name}px`, async ({ page }) => {
    await page.setViewportSize({ width: view.width, height: view.height });
    await page.route("**/me/api/workflows", (route) => fulfilJson(route, meshCatalog));
    await routeLibrary(page, MANY_PICTURES);
    await page.goto("/try/?capability=mesh");
    if (view.width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
    else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await libraryOpen(page);
    await expect(page.getByRole("dialog").locator("ul button")).toHaveCount(MANY_PICTURES.length);
    await shot(page, `library-full-${view.name}`);
  });
}
