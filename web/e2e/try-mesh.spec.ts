import { mkdirSync } from "node:fs";
import path from "node:path";
import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { cubeGlb } from "./fixtures/cube-glb";

// Photo to 3D: one picture to a GLB, shown in the viewer. The gateway's answer is stubbed with a generated cube
// (the mesh route needs a GPU service the fixture gateway does not run); the catalog row that offers the mode is
// stubbed to list it. The 503 case is not stubbed: the fixture gateway answers it itself.
// Screenshots go to test-results/try-mesh/ for the proof pass.

// The production Content-Security-Policy (proxy.py _SECURITY_HEADERS) has no connect-src, so it falls back to
// default-src 'self', which blocks model-viewer's fetch of the blob: URL, and it has no wasm-unsafe-eval. Until the
// gateway allows blob: connections and WebAssembly, these cases bypass the page's CSP so they test the viewer itself.
test.use({ bypassCSP: true });

const PICTURE = path.resolve(__dirname, "fixtures/avatar-picture.png");
const SHOTS = path.resolve(__dirname, "../test-results/try-mesh");

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
        { name: "image", type: "string", required: true, accepted: true, asset: true, media_type: "image/*", label: "picture", help: "The picture to turn into a 3D model. PNG, JPEG or WebP." },
        { name: "seed", type: "integer", minimum: 0, maximum: 4294967295, accepted: true, label: "variation number" },
        { name: "resolution", type: "integer", enum: [512, 1024, 1536], default: 1024, accepted: true, label: "detail", help: "Geometry resolution. Higher is slower." },
        { name: "bg_removal", type: "string", enum: ["threshold", "birefnet"], accepted: true, label: "background removal" },
      ],
      outputs: [{ modality: "mesh", media_type: "model/gltf-binary" }],
    },
  ],
};

async function routeCatalog(page: Page, workflows = meshCatalog) {
  await page.route("**/me/api/workflows", (route) => fulfilJson(route, workflows));
}

/** Opens the Photo to 3D mode: a tab on a wide screen, the Task select on a phone. */
async function openMeshMode(page: Page) {
  const width = page.viewportSize()?.width ?? 0;
  if (width >= 640) await page.getByRole("tab", { name: "Photo to 3D" }).click();
  else await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("mesh");
}

const viewports = [
  { name: "desktop", size: { width: 1280, height: 900 } },
  { name: "phone", size: { width: 390, height: 844 } },
];

for (const { name, size } of viewports) {
  test(`${name}: a picture shows a waiting state, then the 3D model with download`, async ({ page }) => {
    mkdirSync(SHOTS, { recursive: true });
    await page.setViewportSize(size);
    await routeCatalog(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let sentBody = "";
    await page.route("**/v1/meshes/generate", async (route) => {
      sentBody = route.request().postData() ?? "";
      await gate;
      await fulfilBytes(route, { status: 200, contentType: "model/gltf-binary", body: cubeGlb() });
    });

    await page.goto("/try/");
    await openMeshMode(page);
    await page.locator('input[type="file"]').setInputFiles(PICTURE);
    await page.getByRole("button", { name: "Make 3D model" }).click();

    const waiting = page.getByRole("status").filter({ hasText: "This can take about a minute." });
    await expect(waiting).toBeVisible();
    await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
    await page.screenshot({ path: path.join(SHOTS, `${name}-waiting.png`) });

    release();
    await expect(page.locator("model-viewer")).toHaveCount(1);
    await expect(page.getByRole("status").filter({ hasText: "3D model loaded." })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("button", { name: "Reset view" })).toBeEnabled();
    expect(sentBody).toContain('name="resolution"');
    expect(sentBody).toContain("1024");
    await page.screenshot({ path: path.join(SHOTS, `${name}-loaded.png`) });

    const download = page.waitForEvent("download");
    await page.getByRole("link", { name: "Download GLB" }).click();
    expect((await download).suggestedFilename()).toMatch(/^azizam-3d-.*\.glb$/);
  });

  test(`${name}: the gateway's own refusal is shown as-is, with no viewer`, async ({ page }) => {
    mkdirSync(SHOTS, { recursive: true });
    await page.setViewportSize(size);
    await routeCatalog(page);
    await page.goto("/try/");
    await openMeshMode(page);
    await page.locator('input[type="file"]').setInputFiles(PICTURE);
    const reply = page.waitForResponse((response) => response.url().endsWith("/v1/meshes/generate"));
    await page.getByRole("button", { name: "Make 3D model" }).click();
    const response = await reply;
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const body = (await response.json()) as { error: { message: string } };
    await expect(page.getByRole("alert").filter({ hasText: body.error.message })).toBeVisible();
    await expect(page.locator("model-viewer")).toHaveCount(0);
    await page.screenshot({ path: path.join(SHOTS, `${name}-error.png`) });
  });
}

test("the Photo to 3D tab is absent when the catalog does not list the mode as available", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await routeCatalog(page, { ...meshCatalog, workflows: [] });
  await page.goto("/try/");
  await expect(page.getByRole("tab", { name: "Workflow", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Photo to 3D" })).toHaveCount(0);
});
