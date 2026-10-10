import { expect, test, type Locator, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const aliases = [
  ["fast", "chat.completions", "Chat", "chat", "Fast"],
  ["image", "image.generate", "Images", "image", "Images"],
  ["speech", "speech.synthesis", "Speech", "speech", "Speech"],
].map(([id, intent, label, capability, title]) => ({
  id,
  object: "model_catalog.alias",
  display_name: title,
  title,
  capability_title: label,
  icon_key: capability,
  use_names: [id],
  secondary_profile: false,
  alias: true,
  operations: [],
  resolves_to: {},
  description: `${label} through a configured model.`,
  capabilities: [{ intent, label, capability, icon_key: capability }],
  provider: "Local engine",
  engine_label: "Local engine",
  locality: "local",
  cost: { kind: "free", price: null },
  timing: { typical_seconds: 0.8, source: "receipts" },
  availability: {
    available: true,
    measured: true,
    status: "ready",
    reason: "Ready for your next request.",
  },
  io: { input: ["text"], output: ["text"] },
  examples: [],
  recent_receipts: [],
  use: { model: id },
}));

const plans = ["Family", "Creative", "Builders"].map((name, index) => ({
  id: `plan-${index}`,
  name,
  description: null,
  created_at: 1720000000,
  protocols: [],
  workflows: [],
  status_visibility: "full",
  profile: {
    allow: { intents: ["chat", "images"], exposure: ["local"] },
    models: ["demo-chat"],
    fallback: { cloud_allowed: false },
  },
}));

test("polished model grouping fills width before and after filtering", async ({
  page,
}) => {
  await prepare(page, 1440, "light");
  const chatAliases = ["fast", "default", "balanced"].map((id) => ({
    ...aliases[0],
    id,
    display_name: id[0].toUpperCase() + id.slice(1),
    title: id[0].toUpperCase() + id.slice(1),
    use_names: [id],
    use: { model: id },
  }));
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: chatAliases,
        models: [],
      },
    }),
  );
  await page.goto("/ui/models/");
  await expect(
    page.getByRole("heading", { name: "Fast", exact: true }),
  ).toBeVisible();
  const cards = page.locator('table[aria-label="Available model names"] tbody tr');
  await verifyModelLayout(page, cards, 1);
  await expect(page.getByText("2 more", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Audio", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("textbox", { name: "Search models" }).fill("fast");
  await verifyModelLayout(page, cards, 1);
  await expect(page.getByText(/\d+ more/)).toHaveCount(0);
});

for (const width of [390, 1440]) {
  test(`paid model metadata preserves price, unknown timing and readable providers ${width}`, async ({
    page,
  }) => {
    await prepare(page, width, "light");
    const provider =
      "Example remote provider with a long configured display name";
    const price =
      "$0.80 per million input tokens / $2.40 per million output tokens";
    const paid = ["image", "portrait", "illustration"].map((id) => ({
      ...aliases[1],
      id,
      display_name: id[0].toUpperCase() + id.slice(1),
      title: id[0].toUpperCase() + id.slice(1),
      use_names: [id],
      provider,
      engine_label: provider,
      locality: "remote",
      cost: { kind: "paid", price },
      timing: { typical_seconds: null, source: null },
      use: { model: id },
    }));
    await page.route("**/me/api/models/catalog", (route) =>
      route.fulfill({
        json: {
          schema_version: 1,
          object: "models.catalog",
          aliases: paid,
          models: [],
        },
      }),
    );
    await page.goto("/ui/models/");
    const cards = page.locator(
      'table[aria-label="Available model names"] tbody tr',
    );
    await verifyModelLayout(page, cards, 1);
    await expect(page.getByText("2 more", { exact: true })).toBeVisible();
    await expect(
      cards.first().getByText(`Paid · ${price}`, { exact: true }),
    ).toBeVisible();
    await expect(
      cards.first().getByText("Not measured yet", { exact: true }),
    ).toHaveCount(0);
    await expect(
      cards.first().getByText(provider, { exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/screens/polish-paid-${width}.png`,
      fullPage: true,
    });
    await cards.first().getByRole("button", { name: "Details" }).click();
    await expect(
      page.getByRole("dialog").getByText(`Paid · ${price}`, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("dialog").getByText(provider, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("dialog").getByText("Not measured yet", { exact: true }),
    ).toHaveCount(0);
  });
}

async function prepare(page: Page, width: number, theme: "light" | "dark") {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}

async function verifyModelLayout(page: Page, cards: Locator, count: number) {
  await expect(cards).toHaveCount(count);
  const boxes = await cards.evaluateAll((elements) =>
    elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return { width: rect.width };
    }),
  );
  expect(Math.min(...boxes.map((box) => box.width))).toBeGreaterThan(280);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const a11y = await new AxeBuilder({ page }).analyze();
  expect(
    a11y.violations
      .filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      )
      .map((violation) => violation.id),
  ).toEqual([]);
}

async function verifyGrid(
  page: Page,
  cards: Locator,
  width: number,
  count: number,
) {
  await expect(cards).toHaveCount(count);
  const boxes = await cards.evaluateAll((elements) =>
    elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width };
    }),
  );
  const columns = width < 640 ? 1 : width < 1024 ? 2 : Math.min(count, 3);
  const row = boxes.slice(0, columns);
  expect(new Set(row.map((box) => Math.round(box.x))).size).toBe(columns);
  if (columns > 1) expect(Math.abs(row[0].y - row[1].y)).toBeLessThan(2);
  if (count > columns) expect(boxes[columns].y).toBeGreaterThan(boxes[0].y);
  const contentWidth = await page.locator("main").evaluate((element) => {
    const style = getComputedStyle(element);
    return (
      element.getBoundingClientRect().width -
      parseFloat(style.paddingLeft) -
      parseFloat(style.paddingRight)
    );
  });
  // Cards share the content width, rather than leaving singleton capability
  // sections as isolated narrow columns.
  expect(boxes[0].width).toBeGreaterThan(contentWidth / columns - 32);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const a11y = await new AxeBuilder({ page }).analyze();
  expect(
    a11y.violations
      .filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      )
      .map((violation) => violation.id),
  ).toEqual([]);
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [390, 768, 1440]) {
    for (const count of [2, 3]) {
      test(`polished models ${count} aliases ${theme} ${width}`, async ({
        page,
      }) => {
        await prepare(page, width, theme);
        await page.route("**/me/api/models/catalog", (route) =>
          route.fulfill({
            json: {
              schema_version: 1,
              object: "models.catalog",
              aliases: aliases.slice(0, count),
              models: [],
            },
          }),
        );
        await page.goto("/ui/models/");
        // Below 640 px a row is two lines: the task name, then the alias. The
        // model's friendly title is a desktop column.
        await expect(
          page.getByRole("heading", {
            name: width < 640 ? "Chat" : "Fast",
            exact: true,
          }),
        ).toBeVisible();
        const cards = page.locator(
          'table[aria-label="Available model names"] tbody tr',
        );
        await verifyModelLayout(page, cards, count);
        await expect(
          page
            .getByRole("group", { name: "Price filters (choose any)" })
            .getByRole("button", { name: "No provider charge" }),
        ).toHaveAttribute("aria-pressed", "true");
        await page.screenshot({
          path: `test-results/screens/polish-models-${count}-${theme}-${width}.png`,
          fullPage: true,
        });
      });
    }
    test(`polished access plans ${theme} ${width}`, async ({ page }) => {
      await prepare(page, width, theme);
      await page.route("**/admin/api/people", (route) =>
        route.fulfill({ json: { people: [], profiles: plans } }),
      );
      await page.route("**/admin/api/invites", (route) =>
        route.fulfill({ json: { invites: [], profiles: plans } }),
      );
      await page.route("**/admin/api/profiles", (route) =>
        route.fulfill({ json: { profiles: plans } }),
      );
      await page.goto("/ui/people/");
      await page.getByRole("tab", { name: "Access plans" }).click();
      await expect(
        page.getByRole("heading", { name: "Family", exact: true }),
      ).toBeVisible();
      // Craft PR 3: plans are one ListCard of full-width rows, 60 tall (64 on
      // a phone), each with its summary and counts on the second line.
      const rows = page.getByRole("tabpanel").getByRole("listitem");
      await expect(rows).toHaveCount(3);
      const boxes = await rows.evaluateAll((elements) =>
        elements.map((element) => element.getBoundingClientRect().toJSON()),
      );
      const contentWidth = await page
        .getByRole("tabpanel")
        .evaluate((element) => element.getBoundingClientRect().width);
      for (const [index, box] of boxes.entries()) {
        expect(Math.round(box.x)).toBe(Math.round(boxes[0].x));
        expect(box.width).toBeGreaterThan(contentWidth - 4);
        expect(Math.round(box.height)).toBe(width < 640 ? 64 : 60);
        if (index) expect(box.y).toBeGreaterThan(boxes[index - 1].y);
      }
      await expect(rows.first()).toContainText(
        "Chat and images. Local models only. No paid providers. Selected models only.",
      );
      await expect(rows.first()).toContainText("1 selected model");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: `test-results/screens/polish-plans-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}
