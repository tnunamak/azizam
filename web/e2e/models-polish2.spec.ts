import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const measured = {
  id: "chat",
  object: "model_catalog.alias",
  display_name: "chat",
  alias: true,
  operations: ["chat"],
  resolves_to: { chat: "internal-chat-model" },
  description: "Everyday chat on your hardware.",
  capabilities: [{ intent: "chat.completions", label: "Chat" }],
  provider: "capture-engine",
  engine_label: "Household chat",
  locality: "local",
  cost: { kind: "free", price: null },
  timing: { typical_seconds: 0.151, source: "receipts", sample_count: 3 },
  availability: {
    available: true,
    measured: false,
    reason: "Live availability is not measured yet",
  },
  io: { input: ["text"], output: ["text"] },
  examples: [],
  recent_receipts: [],
  use: { model: "chat" },
};

const typical = {
  ...measured,
  id: "image",
  display_name: "image",
  operations: ["image"],
  resolves_to: { image: "internal-image-workflow" },
  description: "Images through your chosen provider.",
  capabilities: [{ intent: "image.generate", label: "Images" }],
  provider: "provider-opaque-id",
  engine_label: "Example image provider",
  locality: "remote",
  cost: { kind: "paid", price: "$0.04 per image" },
  timing: { typical_seconds: 8, source: "configured", sample_count: 0 },
  availability: {
    available: true,
    measured: true,
    status: "ready",
    reason: "Ready for your next request.",
  },
  io: { input: ["text"], output: ["image"] },
  use: { model: "image" },
};

async function catalog(page: Page, aliases: unknown[]) {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases,
        models: [],
      },
    }),
  );
  await page.goto("/ui/models/");
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [390, 1440]) {
    test(`models polish 2 truthful metadata ${theme} ${width}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await catalog(page, [measured, typical]);
      const cards = page.locator(
        '[aria-label="Available model aliases"] article',
      );
      await expect(cards).toHaveCount(2);
      await expect(
        cards.first().getByText("Local", { exact: true }),
      ).toBeVisible();
      await expect(cards.first().getByTitle("Household chat")).toBeVisible();
      await expect(cards.first().getByText("~151 ms · Measured")).toBeVisible();
      await expect(
        cards.first().getByText("Unknown", { exact: true }),
      ).toBeVisible();
      await expect(
        cards.first().getByText("Live availability is not measured yet"),
      ).toBeVisible();
      await expect(
        cards.nth(1).getByText("Remote", { exact: true }),
      ).toBeVisible();
      await expect(
        cards.nth(1).getByTitle("Example image provider"),
      ).toBeVisible();
      await expect(cards.nth(1).getByText("~8.0 s · Typical")).toBeVisible();
      await expect(
        cards.getByText("Not measured yet", { exact: true }),
      ).toHaveCount(0);
      await expect(
        cards.getByText(/capture-engine|provider-opaque-id/i),
      ).toHaveCount(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const result = await new AxeBuilder({ page }).analyze();
      expect(
        result.violations.filter((entry) =>
          ["serious", "critical"].includes(entry.impact ?? ""),
        ),
      ).toEqual([]);
      await page.screenshot({
        path: `test-results/screens/polish2-models-${theme}-${width}.png`,
        fullPage: true,
      });
      await cards.first().getByRole("button", { name: "Details" }).click();
      await expect(
        page.getByRole("dialog").getByText("Measured time"),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("~151 ms · Measured"),
      ).toBeVisible();
    });
  }
}

test("models polish 2 absent telemetry stays honest without exposing service ids", async ({
  page,
}) => {
  await catalog(page, [
    {
      ...measured,
      engine_label: null,
      timing: { typical_seconds: null, source: null },
    },
  ]);
  await expect(page.getByTitle("Configured engine")).toBeVisible();
  await expect(
    page
      .getByLabel("Available model aliases")
      .getByText("Not measured yet", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/capture-engine|Location not measured yet/),
  ).toHaveCount(0);
});

test("models polish 2 empty states provide the next action", async ({
  page,
}) => {
  await catalog(page, []);
  await expect(
    page.getByRole("link", { name: "Review setup" }),
  ).toHaveAttribute("href", "/ui/home/");
  await catalog(page, [measured]);
  await page.getByRole("textbox", { name: "Search models" }).fill("unmatched");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(
    page.getByRole("button", { name: "Copy alias chat" }),
  ).toBeVisible();
});
