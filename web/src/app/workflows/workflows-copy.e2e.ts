import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

const REVIEW_DIR = "/home/tnunamak/.tmp/gw-product/ui-review/gw-r2a";
mkdirSync(REVIEW_DIR, { recursive: true });

const workflow = {
  id: "workflow.image.upscale",
  capability: "image",
  title: "Upscale image",
  description:
    "Make an image larger without changing the prompt. This longer setup note stays in details.",
  inputs: [
    {
      name: "image",
      type: "string",
      required: true,
      accepted: true,
      description: "PNG or JPEG image input.",
    },
    {
      name: "strength",
      type: "number",
      minimum: 0,
      maximum: 1,
      accepted: true,
    },
  ],
  outputs: [{ name: "image", media_type: "image/png" }],
  engine_binding: "image-engine",
  availability: { status: "ready", reason_code: "ready" },
  typical_seconds: null,
  aliases: ["image/upscale"],
  examples: {
    curl: 'curl --json \'{"image":"data:image/png;base64,..."}\'',
    python: "client.workflows.run(id='workflow.image.upscale')",
    javascript: "await client.workflows.run('workflow.image.upscale')",
  },
  native_route: "/v1/workflows/workflow.image.upscale/run",
  discovery_url: "/me/api/workflows/workflow.image.upscale",
};

async function setViewportAndTheme(
  page: Page,
  mode: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: mode, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    mode,
  );
}

async function auditPage(page: Page) {
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}

async function routeWorkflows(
  page: Page,
  status: "seeded" | "empty" | "error",
) {
  await page.route("**/me/api/workflows", (route) => {
    if (status === "error") {
      return route.fulfill({
        status: 500,
        json: { error: { message: "Catalog unavailable" } },
      });
    }
    return route.fulfill({
      json: {
        schema_version: 1,
        workflows: status === "empty" ? [] : [workflow],
        counts: { workflows: status === "empty" ? 0 : 1, capabilities: 1 },
      },
    });
  });
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({ json: { assets: [], next_cursor: null } }),
  );
}

for (const mode of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`workflows seeded ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeWorkflows(page, "seeded");

      await page.goto("/workflows/");
      await expect(
        page.getByText("Choose by purpose and inputs."),
      ).toBeVisible();
      await page.getByRole("button", { name: "Images", exact: true }).click();
      await expect(page.getByText("Upscale image")).toBeVisible();
      await expect(page.getByText("Needs an image")).toBeVisible();
      await expect(page.getByText("Required", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Accepts", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Not measured yet")).toHaveCount(0);
      await page.getByRole("button", { name: "Details" }).click();
      const dialog = page.getByRole("dialog", { name: "Upscale image" });
      await expect(
        dialog.getByText("Inputs, example requests and recent outputs."),
      ).toBeVisible();
      await expect(
        dialog.getByText("workflow.image.upscale", { exact: true }),
      ).toBeAttached();
      await expect(
        dialog.getByText("This workflow does not list its inputs"),
      ).toHaveCount(0);
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/workflows-seeded-${mode}-${width}.png`,
      });
    });

    test(`workflows empty ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeWorkflows(page, "empty");

      await page.goto("/workflows/");
      await expect(page.getByText("No workflows available")).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/workflows-empty-${mode}-${width}.png`,
      });
    });

    test(`workflows error ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await routeWorkflows(page, "error");

      await page.goto("/workflows/");
      await expect(
        page.getByText("Workflows catalog could not be loaded. Try again."),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: /try again/i }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/workflows-error-${mode}-${width}.png`,
      });
    });

    test(`workflows loading ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      await page.route("**/me/api/workflows", () => undefined);

      await page.goto("/workflows/");
      await expect(
        page.getByRole("status", { name: "Loading workflows" }),
      ).toBeVisible();
      await auditPage(page);
      await page.screenshot({
        path: `${REVIEW_DIR}/workflows-loading-${mode}-${width}.png`,
      });
    });
  }
}
