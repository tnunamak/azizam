import { mkdirSync } from "node:fs";
import path from "node:path";
import type { Page, Route } from "@playwright/test";
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

const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
  "base64",
);
const WAV = Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WAVEfmt "), Buffer.alloc(16)]);

const PICTURE = { id: `img_${"a".repeat(64)}`, kind: "image", type: "image/png", prompt: "A teal fish", bytes: PNG };
const RECORDING = { id: `aud_${"b".repeat(64)}`, kind: "audio", type: "audio/wav", prompt: "A short spoken note", bytes: WAV };
const ITEMS = [PICTURE, RECORDING];

const VIEWPORTS = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

/** The Library as the member API serves it, for the kind the picker asks for. */
async function routeLibrary(page: Page) {
  const listed: string[] = [];
  await page.route("**/me/api/media?**", (route) => {
    const url = new URL(route.request().url());
    listed.push(url.search);
    const kind = url.searchParams.get("kind");
    return fulfilJson(route, {
      assets: ITEMS.filter((item) => !kind || item.kind === kind).map((item) => ({
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
  for (const item of ITEMS) {
    await page.route(`**/me/api/media/${item.id}/download`, (route) =>
      fulfilJson(route, { download_url: `/signed/${item.id}` }),
    );
    await page.route(`**/signed/${item.id}`, (route: Route) =>
      fulfilBytes(route, { contentType: item.type, body: item.bytes }),
    );
  }
  return listed;
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
    await shot(page, `home-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await expect(page.getByText("library-picture.png")).toBeVisible();
    await expect(page.getByText(/From your Library/)).toBeVisible();
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
    await shot(page, `try-workflow-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await expect(group.getByText("From your Library", { exact: true })).toBeVisible();
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
    await shot(page, `photo-to-3d-picker-${view.name}`);
    await page.getByRole("button", { name: "A teal fish" }).click();
    await expect(page.getByText("From your Library", { exact: true })).toBeVisible();
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
    await page.getByRole("button", { name: "Choose from Library" }).click();
    await page.getByRole("button", { name: "A short spoken note" }).waitFor();
    await expect(page.getByRole("button", { name: "A teal fish" })).toHaveCount(0);
    const kindRequests = listed.filter((search) => search.includes("kind="));
    expect(kindRequests.length).toBeGreaterThan(0);
    expect(kindRequests.every((search) => search.includes("kind=audio"))).toBe(true);
    await shot(page, `transcribe-picker-${view.name}`);
    await page.getByRole("button", { name: "A short spoken note" }).click();
    await expect(page.getByText("From your Library", { exact: true })).toBeVisible();
    await shot(page, `transcribe-picked-${view.name}`);
    const request = page.waitForRequest("**/audio/transcriptions");
    await page.getByRole("button", { name: "Run", exact: true }).click();
    const posted = (await request).postDataBuffer() as Buffer;
    expect(posted.includes(Buffer.from('filename="library-audio-clip.wav"'))).toBe(true);
    expect(posted.includes(WAV)).toBe(true);
  });
}
