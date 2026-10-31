import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilBytes, fulfilJson } from "./fixtures/intercept";
import { cubeGlb } from "./fixtures/cube-glb";

// Photo to 3D: one picture to a GLB, shown in the viewer. The gateway's answer is stubbed with a generated cube
// (the mesh route needs a GPU service the fixture gateway does not run); the catalog row that offers the mode is
// stubbed to list it. The 503 case is not stubbed: the fixture gateway answers it itself.
// Screenshots go to test-results/try-mesh/ for the proof pass.

// These cases run under the real Content-Security-Policy from proxy.py: connect-src 'self' blob: lets model-viewer
// fetch its blob: URL. The real-model case is opt-in: set TRY_MESH_REAL_GLB to a GLB outside the repo, such as the
// output of a TRELLIS /generate call. It is never committed.

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
        {
          name: "image",
          type: "string",
          required: true,
          accepted: true,
          asset: true,
          media_type: "image/*",
          label: "picture",
          help: "The picture to turn into a 3D model. PNG, JPEG or WebP.",
        },
        {
          name: "seed",
          type: "integer",
          minimum: 0,
          maximum: 4294967295,
          accepted: true,
          label: "variation number",
        },
        {
          name: "resolution",
          type: "integer",
          enum: [512, 1024, 1536],
          default: 1024,
          accepted: true,
          label: "detail",
          help: "Geometry resolution. Higher is slower.",
        },
        {
          name: "bg_removal",
          type: "string",
          enum: ["threshold", "birefnet"],
          accepted: true,
          label: "background removal",
        },
      ],
      outputs: [{ modality: "mesh", media_type: "model/gltf-binary" }],
    },
  ],
};

async function routeCatalog(page: Page, workflows = meshCatalog) {
  await page.route("**/me/api/workflows", (route) =>
    fulfilJson(route, workflows),
  );
}

/** Opens the Photo to 3D mode: a tab on a wide screen, the Task select on a phone. */
async function openMeshMode(page: Page) {
  const width = page.viewportSize()?.width ?? 0;
  if (width >= 640)
    await page.getByRole("tab", { name: "Photo to 3D" }).click();
  else
    await page
      .getByRole("combobox", { name: "Task", exact: true })
      .selectOption("mesh");
}

const models = [
  { key: "cube", bytes: () => cubeGlb(), timeout: 30_000 },
  ...(process.env.TRY_MESH_REAL_GLB
    ? [
        {
          key: "real",
          bytes: () => readFileSync(process.env.TRY_MESH_REAL_GLB as string),
          timeout: 90_000,
        },
      ]
    : []),
];

/**
 * Where the element, its host and the canvas it shows sit, in CSS pixels, and the canvas's drawing buffer in device
 * pixels. model-viewer keeps two canvases in its shadow root and shows one of them: the 2D canvas first, then the
 * WebGL canvas once the model is visible. Only the shown one has a box, so the first canvas is not always the one.
 */
async function viewerGeometry(page: Page) {
  return page.locator("model-viewer").evaluate((viewer) => {
    const box = (element: Element | null | undefined) => {
      const rect = element?.getBoundingClientRect();
      return rect
        ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        : null;
    };
    const canvases = [
      ...(viewer.shadowRoot?.querySelectorAll("canvas") ?? []),
    ] as HTMLCanvasElement[];
    const canvas =
      canvases.find((item) => getComputedStyle(item).display !== "none") ??
      null;
    const bar =
      viewer.shadowRoot?.querySelector("#default-progress-bar > .bar") ?? null;
    return {
      element: box(viewer),
      host: box(viewer.parentElement),
      canvas: box(canvas),
      buffer: canvas ? { width: canvas.width, height: canvas.height } : null,
      dpr: window.devicePixelRatio,
      bar: box(bar),
    };
  });
}

const viewports = [
  { name: "desktop", size: { width: 1280, height: 900 } },
  { name: "phone", size: { width: 390, height: 844 } },
];

for (const { name, size } of viewports) {
  for (const model of models) {
    test(`${name}: a picture shows a waiting state, then the ${model.key} 3D model with download`, async ({
      page,
    }) => {
      test.setTimeout(model.timeout + 60_000);
      mkdirSync(SHOTS, { recursive: true });
      await page.setViewportSize(size);
      await routeCatalog(page);
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      let sentBody = "";
      await page.route("**/v1/meshes/generate", async (route) => {
        sentBody = route.request().postData() ?? "";
        await gate;
        await fulfilBytes(route, {
          status: 200,
          contentType: "model/gltf-binary",
          body: model.bytes(),
        });
      });

      await page.goto("/try/");
      await openMeshMode(page);
      // The picture is one picker, labelled in plain words, with no file path or raw field name in the form's text.
      const form = page.locator("form").filter({
        has: page.getByRole("button", { name: "Make 3D model" }),
      });
      await expect(form.locator('input[type="file"]')).toHaveCount(1);
      await expect(
        form
          .locator("label")
          .filter({ hasText: "Picture" })
          .locator('input[type="file"]'),
      ).toHaveCount(1);
      const formText = (await form.innerText()).replace(/\s+/g, " ");
      expect(formText).not.toMatch(/\.\/[\w-]+\.\w+|\.bin\b/);
      expect(formText).not.toMatch(/\bimage\b/);
      await page.locator('input[type="file"]').setInputFiles(PICTURE);
      await page.getByRole("button", { name: "Make 3D model" }).click();

      const waiting = page
        .getByRole("status")
        .filter({ hasText: "This can take about a minute." });
      await expect(waiting).toBeVisible();
      await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
      await page.screenshot({
        path: path.join(SHOTS, `${name}-${model.key}-waiting.png`),
      });

      release();
      await expect(page.locator("model-viewer")).toHaveCount(1);
      await expect(
        page.getByRole("status").filter({ hasText: "3D model loaded." }),
      ).toBeVisible({ timeout: model.timeout });
      await expect(
        page.getByRole("button", { name: "Reset view" }),
      ).toBeEnabled();
      // The element fills its host, and the canvas the viewer shows covers the element and draws at the element's
      // size. The drawing buffer is always element x device pixel ratio. The CSS box is the element's size at full
      // resolution; under GPU throttling model-viewer enlarges it (adaptive resolution, render-scale event) and the
      // element crops it. So the CSS check is "covers", not "equals". The viewer shows the WebGL canvas some time after
      // "loaded" (seconds, in a diagnostic run), so the check polls for the settled state instead of reading once.
      // The canvas snaps to whole device pixels, so each size may differ from its box by one pixel.
      const near = (a: number | undefined, b: number | undefined) =>
        Math.abs((a ?? -Infinity) - (b ?? Infinity)) <= 1;
      await expect
        .poll(async () => {
          const geometry = await viewerGeometry(page);
          const elementWidth = geometry.element?.width ?? 0;
          const elementHeight = geometry.element?.height ?? 0;
          return {
            fillsHost:
              near(geometry.element?.width, geometry.host?.width) &&
              near(geometry.element?.height, geometry.host?.height),
            drawsAtElementSize:
              near(geometry.buffer?.width, elementWidth * geometry.dpr) &&
              near(geometry.buffer?.height, elementHeight * geometry.dpr),
            coversElement:
              (geometry.canvas?.width ?? 0) >= elementWidth - 1 &&
              (geometry.canvas?.height ?? 0) >= elementHeight - 1,
            wide: (geometry.canvas?.width ?? 0) > 200,
          };
        })
        .toEqual({
          fillsHost: true,
          drawsAtElementSize: true,
          coversElement: true,
          wide: true,
        });
      // model-viewer's own progress bar is hidden once loaded (it can stick short of full).
      expect((await viewerGeometry(page)).bar?.height ?? 0).toBeLessThanOrEqual(
        0,
      );
      expect(sentBody).toContain('name="resolution"');
      expect(sentBody).toContain("1024");
      // Centre the viewer, clear of the phone's fixed tab bar at the bottom.
      await page
        .locator("model-viewer")
        .evaluate((viewer) => viewer.scrollIntoView({ block: "center" }));
      await page.screenshot({
        path: path.join(SHOTS, `${name}-${model.key}-loaded.png`),
      });

      const download = page.waitForEvent("download");
      await page.getByRole("link", { name: "Download GLB" }).click();
      expect((await download).suggestedFilename()).toMatch(
        /^azizam-3d-.*\.glb$/,
      );
    });
  }

  test(`${name}: the gateway's own refusal is shown as-is, with no viewer`, async ({
    page,
  }) => {
    mkdirSync(SHOTS, { recursive: true });
    await page.setViewportSize(size);
    await routeCatalog(page);
    await page.goto("/try/");
    await openMeshMode(page);
    await page.locator('input[type="file"]').setInputFiles(PICTURE);
    const reply = page.waitForResponse((response) =>
      response.url().endsWith("/v1/meshes/generate"),
    );
    await page.getByRole("button", { name: "Make 3D model" }).click();
    const response = await reply;
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const body = (await response.json()) as { error: { message: string } };
    await expect(
      page.getByRole("alert").filter({ hasText: body.error.message }),
    ).toBeVisible();
    await expect(page.locator("model-viewer")).toHaveCount(0);
    await page.screenshot({ path: path.join(SHOTS, `${name}-error.png`) });
  });
}

test("the Photo to 3D tab is absent when the catalog does not list the mode as available", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await routeCatalog(page, { ...meshCatalog, workflows: [] });
  await page.goto("/try/");
  await expect(
    page.getByRole("tab", { name: "Workflow", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: "Photo to 3D" })).toHaveCount(0);
});

test("the page is served with a policy that lets the viewer fetch its blob: model", async ({
  page,
}) => {
  const response = await page.goto("/try/");
  expect(response?.headers()["content-security-policy"] ?? "").toContain(
    "connect-src 'self' blob:",
  );
});
