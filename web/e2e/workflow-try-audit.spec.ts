import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const emptyModelsCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases: [],
  models: [],
  counts: { aliases: 0, models: 0, capabilities: 0 },
};

const workflowCatalog = {
  schema_version: 1,
  object: "workflow.catalog",
  discovery_url: "/gateway/v1/workflows",
  workflows: [
    {
      id: "image.generate.poster",
      short_id: "poster",
      title: "Poster image",
      description: "Image workflow served through the OpenAI image route.",
      engine_binding: "comfyui",
      native_route: "/v1/images/generations",
      inputs: [
        { name: "prompt", type: "string", required: true, accepted: true },
        {
          name: "style",
          type: "string",
          enum: ["studio", "flatlay"],
          default: "studio",
          required: true,
          accepted: true,
        },
        {
          name: "steps",
          type: "integer",
          default: 8,
          minimum: 1,
          maximum: 12,
          required: true,
          accepted: true,
        },
      ],
      example_request: {
        method: "POST",
        path: "/v1/images/generations",
        content_type: "application/json",
        body: {
          model: "poster",
          prompt: "A teal poster.",
          style: "studio",
          steps: 8,
        },
        form: {},
        files: {},
      },
    },
  ],
};

async function routeCatalogs(page: Page, workflows = workflowCatalog) {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({ json: emptyModelsCatalog }),
  );
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ json: workflows }),
  );
}

async function prepareWorkflowTry(page: Page, workflows = workflowCatalog) {
  await routeCatalogs(page, workflows);
  await page.goto("/try/?workflow=image.generate.poster");
  if (!workflows.workflows.length)
    await expect(page.getByText("No workflows are listed")).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Workflow", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`workflow Try guided states fit and pass axe at ${width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((value) => {
        localStorage.setItem("theme", value);
      }, theme);

      await prepareWorkflowTry(page);
      await expect(
        page.getByRole("heading", { name: "Try", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("textbox", { name: /^prompt Required$/i }),
      ).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: /^style Required$/i }),
      ).toContainText("studio");
      await checkState(page, `workflow-try-guided-${theme}-${width}.png`);

      await page.route("**/v1/images/generations", (route) =>
        route.fulfill({
          json: {
            data: [
              {
                url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
                media_type: "image/png",
              },
            ],
          },
        }),
      );
      await page
        .getByRole("textbox", { name: /^prompt Required$/i })
        .fill("A teal poster.");
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await expect(page.getByAltText("Generated result")).toBeVisible();
      await expect(
        page.getByText("Try uses a temporary key", { exact: false }),
      ).toBeVisible();
      await checkState(page, `workflow-try-result-${theme}-${width}.png`);
      await page.unroute("**/v1/images/generations");
      await page.route("**/v1/images/generations", (route) =>
        route.fulfill({
          status: 403,
          json: { error: { message: "This key cannot use poster." } },
        }),
      );
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await expect(page.getByText("This key cannot use poster.")).toBeVisible();
      await checkState(page, `workflow-try-error-${theme}-${width}.png`);
      await page.unroute("**/v1/images/generations");

      await prepareWorkflowTry(page, { ...workflowCatalog, workflows: [] });
      await expect(
        page.getByText("Run workflow", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: "Model", exact: true }),
        // Try offers only catalog choices; it no longer invents one.
      ).toContainText("No options available");
      await expect(
        page.getByRole("button", { name: "Run", exact: true }),
      ).toBeDisabled();
      await checkState(page, `workflow-try-empty-${theme}-${width}.png`);
    });
  }
}

async function checkState(page: Page, file: string) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/${file}`,
    fullPage: true,
  });
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => ({
      id: violation.id,
      nodes: violation.nodes.map((node) => ({
        target: node.target,
        summary: node.failureSummary,
      })),
    }));
  if (violations.length) {
    console.log(JSON.stringify(violations, null, 2));
  }
  expect(violations).toEqual([]);
}
