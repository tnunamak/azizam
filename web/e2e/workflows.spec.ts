import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { workflowsPolishCatalog } from "./fixtures/workflows-polish";

const seededCatalog = {
  schema_version: 1,
  workflows: [
    {
      id: "image.product-render",
      capability: "image",
      title: "Product render",
      description: "Create a clean product render.",
      technical_notes:
        "Long manifest guidance stays searchable and available inside Technical details without turning browse cards into documentation blocks.",
      inputs: [
        {
          name: "prompt",
          type: "string",
          required: true,
          accepted: true,
          description: "What to render.",
        },
        {
          name: "width",
          type: "integer",
          default: 1024,
          minimum: 256,
          maximum: 2048,
          accepted: true,
        },
        {
          name: "style_preset",
          type: "string",
          default: "studio",
          enum: ["studio", "flatlay"],
          accepted: false,
        },
      ],
      outputs: [
        {
          name: "render",
          media_type: "image/png",
          description: "Final product render asset.",
        },
      ],
      engine_binding: "image-local",
      availability: {
        status: "configured",
        reason_code: "configured",
        available: true,
        measured: false,
      },
      typical_seconds: 12,
      aliases: ["image", "product-shot"],
      examples: {
        curl: 'curl $AZIZAM_BASE/v1/images/generations -H "Authorization: Bearer $AZIZAM_API_KEY" -H "Content-Type: application/json" -d \'{"workflow":"image.product-render","prompt":"A teal desk lamp"}\'',
        python:
          'from openai import OpenAI\nclient = OpenAI()\nclient.images.generate(model="image", prompt="A teal desk lamp", extra_body={"workflow":"image.product-render"})',
        javascript:
          'import OpenAI from "openai";\nconst client = new OpenAI();\nawait client.images.generate({ model: "image", prompt: "A teal desk lamp", workflow: "image.product-render" });',
      },
      native_route: "/v1/workflows/image.product-render/run",
      discovery_url: "/gateway/v1/workflows/image.product-render",
      try_url: "/ui/try/?workflow=image.product-render",
    },
    {
      id: "audio.voiceover",
      capability: "audio",
      title: "Voiceover",
      description: "Generate spoken audio from a short script.",
      inputs: [
        {
          name: "text",
          type: "string",
          required: true,
          accepted: true,
        },
      ],
      outputs: [],
      engine_binding: null,
      availability: {
        status: "unavailable",
        reason_code: "unavailable_unconfigured",
        available: false,
        measured: false,
      },
      typical_seconds: null,
      aliases: ["speech"],
      examples: {
        curl: 'curl $AZIZAM_BASE/v1/audio/speech -H "Authorization: Bearer $AZIZAM_API_KEY" -d \'{"workflow":"audio.voiceover","input":"Hello"}\'',
        python:
          'from openai import OpenAI\nclient = OpenAI()\nclient.audio.speech.create(model="voice", input="Hello", voice="alloy")',
        javascript:
          'import OpenAI from "openai";\nconst client = new OpenAI();\nawait client.audio.speech.create({ model: "voice", input: "Hello", voice: "alloy" });',
      },
      native_route: null,
      discovery_url: "/gateway/v1/workflows/audio.voiceover",
      try_url: "/ui/try/?workflow=audio.voiceover",
    },
  ],
};

const emptyCatalog = {
  schema_version: 1,
  workflows: [],
};

const seededRecentJobs = {
  assets: [
    {
      asset_id: "asset_image_1",
      job_id: "job_product_1",
      kind: "image",
      prompt: "A teal desk lamp on walnut",
      model: "image",
      workflow: "image.product-render",
      created_at: 1_760_000_000,
      generation_ms: 12_400,
      download_url: "/me/api/media/asset_image_1/download",
      receipt_id: "req_product_1",
    },
    {
      asset_id: "asset_image_2",
      job_id: "job_product_2",
      kind: "image",
      prompt: "A quiet studio product render",
      model: "image",
      workflow: "image.product-render",
      created_at: 1_760_010_000,
      generation_ms: 10_900,
      download_url: "/me/api/media/asset_image_2/download",
      receipt_id: "req_product_2",
    },
  ],
  models: ["image"],
  next_cursor: null,
};

async function setSurface(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 768 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function routeWorkflows(page: Page, payload: unknown, status = 200) {
  await page.route("**/me/api/workflows", (route) =>
    route.fulfill({ status, json: payload }),
  );
}

async function routeRecentJobs(
  page: Page,
  payload: unknown,
  status = 200,
  requests: string[] = [],
) {
  await page.route("**/me/api/media?**", (route) => {
    requests.push(route.request().url());
    return route.fulfill({ status, json: payload });
  });
}

async function preparePage(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 768 | 390,
  payload: unknown,
  status = 200,
) {
  await setSurface(page, theme, width);
  await routeWorkflows(page, payload, status);
  await routeRecentJobs(page, { assets: [], models: [], next_cursor: null });
  const response = await page.goto("/ui/workflows/");
  expect(response?.status()).toBeLessThan(500);
  await expect(
    page.getByRole("heading", { name: "Workflows", exact: true }),
  ).toBeVisible();
}

/** The owner folds workflows that are not set up; open that section. */
async function openNotSetUp(page: Page, count = 1) {
  await page.getByText(`Not set up (${count})`, { exact: true }).click();
}

const faceLikenessRow = {
  id: "image.analyze.face-likeness",
  capability: "image",
  title: "Face Likeness Check",
  purpose: "Score how closely a face matches a reference",
  description: "Score how closely a face matches a reference",
  inputs: [
    { name: "reference", type: "string", asset: true, media_type: "image/*", label: "a face photo", required: true, accepted: true },
    { name: "candidate", type: "string", asset: true, media_type: "image/*", label: "a photo to check", required: true, accepted: true },
    { name: "people", type: "string", label: "a list of people", required: false, accepted: true },
    { name: "thresholds", type: "string", label: "custom thresholds", required: false, accepted: true },
  ],
  outputs: [{ name: "report", media_type: "application/json", label: "a likeness score" }],
  engine_binding: "gateway",
  availability: { status: "unknown", reason_code: "unknown", available: false, measured: false, evidence: { kind: "unknown" } },
  typical_seconds: 2,
  aliases: [],
  examples: { curl: "curl", python: "python", javascript: "js" },
  native_route: null,
  discovery_url: "/gateway/v1/workflows/image.analyze.face-likeness",
};

async function assertNoSeriousAxe(page: Page) {
  const a11y = await new AxeBuilder({ page }).analyze();
  const violations = a11y.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
}

async function assertNoOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Workflows page must fit the viewport",
  ).toBe(true);
}

test("workflow cards use the available width at phone, tablet and desktop sizes", async ({
  page,
}) => {
  for (const [width, columns] of [
    [390, 1],
    [768, 2],
    [1440, 3],
  ] as const) {
    await preparePage(page, "light", width, workflowsPolishCatalog);
    await expect(
      page.getByText("Create a clean product render", { exact: true }),
    ).toBeVisible();
    const cards = page
      .getByRole("region", { name: "Images workflows" })
      .locator("article");
    await expect(cards).toHaveCount(3);
    const geometry = await cards.evaluateAll((elements) =>
      elements.map((element) => {
        const rect = element.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width };
      }),
    );
    const firstRow = geometry.slice(0, columns);
    expect(new Set(firstRow.map((card) => Math.round(card.x))).size).toBe(
      columns,
    );
    expect(firstRow.every((card) => Math.abs(card.y - firstRow[0].y) < 2)).toBe(
      true,
    );
    if (columns < 3) {
      expect(geometry[columns].y).toBeGreaterThan(geometry[0].y);
    }
    const grid = await cards
      .first()
      .locator("xpath=..")
      .evaluate((element) => ({
        width: element.getBoundingClientRect().width,
        gap: Number.parseFloat(getComputedStyle(element).columnGap),
      }));
    const expectedWidth = (grid.width - grid.gap * (columns - 1)) / columns;
    expect(Math.abs(geometry[0].width - expectedWidth)).toBeLessThan(2);

    await page.getByPlaceholder("Search workflows").fill("product-render");
    await expect(cards).toHaveCount(1);
    const singletonWidth = await cards
      .first()
      .evaluate((card) => card.getBoundingClientRect().width);
    const contentWidth = await page
      .getByLabel("Available workflows")
      .evaluate((grid) => grid.getBoundingClientRect().width);
    expect(singletonWidth).toBeGreaterThan(contentWidth - 2);
    await assertNoOverflow(page);
  }
});

test("typed input table fits desktop and mobile summaries expose Range on a narrow sheet", async ({
  page,
}) => {
  for (const width of [1440, 390] as const) {
    await setSurface(page, "light", width);
    await routeWorkflows(page, workflowsPolishCatalog);
    await routeRecentJobs(page, { assets: [], models: [], next_cursor: null });
    await page.goto("/ui/workflows/");
    await page.getByRole("button", { name: "Details" }).first().click();

    const sheet = page.getByRole("dialog", { name: "Product render" });
    await expect(sheet).toBeVisible();
    // The typed input table sits inside the closed Technical details.
    await sheet.getByText("Technical details", { exact: true }).click();
    const columnNames = ["Name", "Type", "Default", "Range", "Applies"];

    if (width === 1440) {
      const scrollport = sheet.getByLabel("Scrollable inputs");
      const bounds = await scrollport.evaluate((element) => {
        const viewport = element.getBoundingClientRect();
        const columns = Array.from(
          element.querySelectorAll('th[data-slot="table-head"]'),
        ).map((column) => {
          const rect = column.getBoundingClientRect();
          return { left: rect.left, right: rect.right };
        });
        return {
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
          left: viewport.left,
          right: viewport.right,
          columns,
        };
      });
      expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.clientWidth);
      expect(bounds.columns).toHaveLength(columnNames.length);
      expect(
        bounds.columns.every(
          (column) =>
            column.left >= bounds.left && column.right <= bounds.right,
        ),
      ).toBe(true);

      for (const name of columnNames) {
        await expect(
          scrollport.getByRole("columnheader", { name, exact: true }),
        ).toBeVisible();
      }
      await expect(
        scrollport.getByRole("cell", { name: "0", exact: true }),
      ).toHaveCount(2);
      expect(
        await scrollport
          .getByRole("cell", { name: "guidance_scale", exact: true })
          .evaluate((cell) => getComputedStyle(cell).whiteSpace),
      ).toBe("normal");
      await expect(
        scrollport.getByRole("cell", { name: "false" }),
      ).toBeVisible();
      await expect(
        scrollport.getByRole("cell", { name: "0 to 65535" }),
      ).toBeVisible();
      await expect(
        scrollport.getByRole("cell", { name: "0 to 20" }),
      ).toBeVisible();
      await expect(scrollport.getByText("studio, flatlay")).toBeVisible();
      await expect(scrollport.getByText("Not specified")).toHaveCount(0);
    } else {
      const summaries = sheet.getByRole("list", {
        name: "Input summaries",
      });
      await expect(summaries.getByText("seed")).toBeVisible();
      await expect(summaries.getByText("integer · Optional")).toBeVisible();
      await expect(summaries.getByText("0 to 65535")).toBeVisible();
      await expect(summaries.getByText("guidance_scale")).toBeVisible();
      await expect(summaries.getByText("0 to 20")).toBeVisible();
      await expect(summaries.getByText("studio, flatlay")).toBeVisible();
      await expect(summaries.getByText("Ignored")).toBeVisible();
      await expect(summaries.getByText("Not specified")).toHaveCount(0);
      await assertNoOverflow(page);
    }
  }
});

test("missing input and time details stay quiet", async ({
  page,
}) => {
  await setSurface(page, "light", 390);
  await routeWorkflows(page, seededCatalog);
  await routeRecentJobs(page, { assets: [], models: [], next_cursor: null });
  await page.goto("/ui/workflows/");
  await openNotSetUp(page);
  await page.getByRole("button", { name: "Details" }).nth(1).click();
  const sheet = page.getByRole("dialog", { name: "Voiceover" });
  await sheet.getByText("Technical details", { exact: true }).click();
  await expect(sheet.getByText("Not measured yet")).toHaveCount(0);
  await expect(sheet.getByText("Not specified")).toHaveCount(0);
});

async function checkState(page: Page, screenshotName: string) {
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
  await page.screenshot({
    path: `test-results/screens/${screenshotName}`,
    fullPage:
      !screenshotName.includes("sheet") && page.viewportSize()?.width !== 390,
  });
}

async function openSeededSheet(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 390,
  requests: string[] = [],
) {
  await setSurface(page, theme, width);
  await routeWorkflows(page, seededCatalog);
  await routeRecentJobs(page, seededRecentJobs, 200, requests);
  await page.goto("/ui/workflows/");
  await page.getByRole("button", { name: "Details" }).first().click();
  const sheet = page.getByRole("dialog", { name: "Product render" });
  await expect(sheet).toBeVisible();
  return sheet;
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`workflows seeded catalog: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await preparePage(page, theme, width, seededCatalog);

      await expect(
        page.getByRole("heading", { name: "Images", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Product render", exact: true }),
      ).toBeVisible();
      // The owner sees the workflow that is not set up, folded, with one reason and a next step.
      await openNotSetUp(page);
      await expect(
        page.getByRole("heading", { name: "Audio", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("Setup is not finished. Finish it in Engines."),
      ).toBeVisible();
      await expect(
        page.getByRole("link", { name: "Open Engines" }),
      ).toBeVisible();
      await expect(
        page.getByText(/Long manifest guidance stays searchable/),
      ).toHaveCount(0);
      await expect(page.getByText("image/png")).toHaveCount(0);
      await expect(page.getByText("product-shot", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Unavailable", { exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Details" })).toHaveCount(
        2,
      );

      await checkState(page, `workflows-seeded-${theme}-${width}.png`);
    });

    test(`workflows empty catalog: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await preparePage(page, theme, width, emptyCatalog);
      await expect(page.getByText("No workflows available")).toBeVisible();
      await checkState(page, `workflows-empty-${theme}-${width}.png`);
    });

    test(`workflows error catalog: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await preparePage(
        page,
        theme,
        width,
        { error: { message: "Unavailable for this test" } },
        503,
      );
      await expect(
        page.getByText("Workflows catalog could not be loaded. Try again."),
      ).toBeVisible();
      await checkState(page, `workflows-error-${theme}-${width}.png`);
    });
  }
}

test("workflows loading state stays accessible while the catalog is pending", async ({
  page,
}) => {
  await setSurface(page, "light", 390);
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/me/api/workflows", async (route) => {
    await pending;
    await route.fulfill({ json: seededCatalog });
  });
  await routeRecentJobs(page, { assets: [], models: [], next_cursor: null });
  await page.goto("/ui/workflows/", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("status", { name: "Loading workflows" }),
  ).toBeVisible();
  await assertNoOverflow(page);
  await assertNoSeriousAxe(page);
  release();
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
});

test("workflows search filters aliases and can be cleared", async ({
  page,
}) => {
  await preparePage(page, "light", 1440, seededCatalog);
  const search = page.getByPlaceholder("Search workflows");

  await search.fill("product-shot");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Voiceover", exact: true }),
  ).toHaveCount(0);

  await search.fill("does-not-exist");
  await expect(page.getByText("No matching workflows")).toBeVisible();
  await search.fill("Long manifest guidance");
  await expect(
    page.getByText("No matching workflows"),
  ).toBeVisible();
  await search.fill("image-local");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await search.fill("What to render");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await search.fill("Final product render asset");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await search.fill("256 to 2048");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await search.fill("/v1/workflows/image.product-render/run");
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await search.clear();
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await openNotSetUp(page);
  await expect(
    page.getByRole("heading", { name: "Voiceover", exact: true }),
  ).toBeVisible();
});

test("a search that only matches a workflow that is not set up opens its folded section", async ({
  page,
}) => {
  await preparePage(page, "light", 1440, seededCatalog);
  await page.getByPlaceholder("Search workflows").fill("speech");
  await expect(
    page.getByRole("heading", { name: "Voiceover", exact: true }),
  ).toBeVisible();
});

test("owner sees Face Likeness Check in plain words with a reason and a link", async ({
  page,
}) => {
  await preparePage(page, "light", 1440, {
    schema_version: 1,
    workflows: [seededCatalog.workflows[0], faceLikenessRow],
  });
  await openNotSetUp(page);
  const card = page.getByRole("article", { name: "Face Likeness Check" });
  await expect(card).toContainText("Needs: a face photo and a photo to check");
  await expect(card).toContainText("Optional: a list of people and custom thresholds");
  await expect(card).toContainText("Makes: a likeness score");
  await expect(card).toContainText("No health check has reported on it yet. See Status.");
  await expect(card).not.toContainText(/Unavailable|Not available right now|text and text/);
  await expect(card.getByRole("link", { name: "Open Status" })).toHaveAttribute("href", /\/status/);
  await assertNoSeriousAxe(page);
});

test("a member does not see workflows that are not set up", async ({ page }) => {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "fixture",
        apps: [],
        user: { id: "member-wf", name: "Member", role: "member", email: "member@example.test" },
      },
    }),
  );
  await preparePage(page, "light", 1440, {
    schema_version: 1,
    workflows: [seededCatalog.workflows[0], faceLikenessRow, seededCatalog.workflows[1]],
  });
  await expect(
    page.getByRole("heading", { name: "Product render", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Face Likeness Check")).toHaveCount(0);
  await expect(page.getByText("Voiceover")).toHaveCount(0);
  await expect(page.getByText(/Not set up/)).toHaveCount(0);
  await expect(page.getByText("1 workflow", { exact: true })).toBeVisible();
});

test("workflow sheet shows controls, code, links and seeded recent jobs", async ({
  context,
  page,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const mediaRequests: string[] = [];
  const sheet = await openSeededSheet(page, "light", 1440, mediaRequests);
  const technicalDetails = sheet
    .getByText("Technical details", { exact: true })
    .locator("xpath=ancestor::details[1]");
  await expect(technicalDetails).not.toHaveAttribute("open", "");
  await expect(
    sheet.getByText(/Long manifest guidance stays searchable/),
  ).not.toBeVisible();
  await sheet.getByText("Technical details", { exact: true }).click();
  await expect(technicalDetails).toHaveAttribute("open", "");
  await expect(
    sheet.getByText(/Long manifest guidance stays searchable/),
  ).toBeVisible();
  await expect(sheet.getByRole("columnheader", { name: "Name" })).toBeVisible();
  await expect(sheet.getByRole("columnheader", { name: "Type" })).toBeVisible();
  await expect(
    sheet.getByRole("columnheader", { name: "Default" }),
  ).toBeVisible();
  await expect(
    sheet.getByRole("columnheader", { name: "Range" }),
  ).toBeVisible();
  await expect(
    sheet.getByRole("columnheader", { name: "Applies" }),
  ).toBeVisible();
  await expect(sheet.getByRole("cell", { name: "prompt" })).toBeVisible();
  await expect(
    sheet.getByRole("cell", { name: /256\s*(?:-|to)\s*2048/ }),
  ).toBeVisible();
  await expect(sheet.getByRole("cell", { name: "Ignored" })).toBeVisible();

  const details = sheet.locator("details").filter({
    hasText: "Technical details",
  });
  await expect(
    details.getByText("image.product-render", { exact: true }),
  ).toBeVisible();
  await expect(details.getByText("image-local", { exact: true })).toBeVisible();
  await expect(
    details.getByText("/v1/workflows/image.product-render/run", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    details.getByText("product-shot", { exact: true }),
  ).toBeVisible();

  await expect(sheet.getByText(/curl \$AZIZAM_BASE/)).toBeVisible();
  await sheet.getByRole("tab", { name: "Python" }).click();
  await expect(sheet.getByText(/from openai import OpenAI/)).toBeVisible();
  await sheet.getByRole("tab", { name: "JavaScript" }).click();
  await expect(sheet.getByText(/import OpenAI from "openai"/)).toBeVisible();

  await sheet.getByRole("button", { name: "Copy JavaScript code" }).click();
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toContain("image.product-render");

  await expect(sheet.getByRole("link", { name: "Open in Try" })).toHaveAttribute(
    "href",
    "/ui/try/?workflow=image.product-render",
  );
  await expect(
    sheet.getByRole("link", { name: "Machine-readable workflow" }),
  ).toHaveAttribute("href", "/gateway/v1/workflows/image.product-render");

  expect(mediaRequests.at(-1)).toContain("workflow=image.product-render");
  expect(mediaRequests.at(-1)).toContain("limit=5");
  await expect(sheet.getByText("Recent jobs")).toBeVisible();
  await expect(sheet.getByText("A teal desk lamp on walnut")).toBeVisible();
  await expect(sheet.getByText("12.4 s")).toBeVisible();
  await expect(sheet.locator("time").first()).toBeVisible();
  await expect(
    sheet
      .getByRole("region", { name: "Recent jobs" })
      .getByText("image", { exact: true })
      .first(),
  ).toBeVisible();
  await expect(
    sheet.getByText("image.product-render", { exact: true }).first(),
  ).toBeVisible();
  await checkState(page, "workflows-sheet-seeded-light-1440.png");
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`workflow seeded sheet screenshot: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await openSeededSheet(page, theme, width);
      await expect(page.getByText("A teal desk lamp on walnut")).toBeVisible();
      await checkState(page, `workflows-sheet-seeded-${theme}-${width}.png`);
    });
  }
}

test("workflow sheet recent jobs handles empty records", async ({ page }) => {
  await setSurface(page, "dark", 390);
  await routeWorkflows(page, seededCatalog);
  await routeRecentJobs(page, { assets: [], models: [], next_cursor: null });
  await page.goto("/ui/workflows/");
  await openNotSetUp(page);
  await page.getByRole("button", { name: "Details" }).nth(1).click();
  await expect(page.getByRole("dialog")).toContainText(
    /No recent (jobs|records)/,
  );
  await checkState(page, "workflows-sheet-empty-dark-390.png");
});

test("workflow sheet recent jobs handles media errors", async ({ page }) => {
  await setSurface(page, "dark", 390);
  await routeWorkflows(page, seededCatalog);
  await routeRecentJobs(page, { error: { message: "Unavailable" } }, 503);
  await page.goto("/ui/workflows/");
  await page.getByRole("button", { name: "Details" }).first().click();
  await expect(page.getByRole("dialog")).toContainText(
    /Recent jobs could not be loaded|Workflow data could not be loaded/,
  );
  await checkState(page, "workflows-sheet-error-dark-390.png");
});
