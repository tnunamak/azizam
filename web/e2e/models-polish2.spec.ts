import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const measured = {
  id: "chat",
  object: "model_catalog.alias",
  display_name: "Default",
  title: "Default",
  capability_title: "Chat",
  icon_key: "chat",
  use_names: ["chat", "internal-chat-model"],
  secondary_profile: false,
  alias: true,
  operations: ["chat"],
  resolves_to: { chat: "internal-chat-model" },
  description: "Everyday chat on your hardware.",
  capabilities: [
    {
      intent: "chat.completions",
      capability: "chat",
      icon_key: "chat",
      label: "Chat",
    },
  ],
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
  display_name: "Images",
  title: "Images",
  capability_title: "Images",
  icon_key: "image",
  use_names: ["image", "internal-image-workflow"],
  operations: ["image"],
  resolves_to: { image: "internal-image-workflow" },
  description: "Images through your chosen provider.",
  capabilities: [
    {
      intent: "image.generate",
      capability: "image",
      icon_key: "image",
      label: "Images",
    },
  ],
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
  await page.goto("/models/");
}

// Row metadata honesty is asserted in vitest
// (src/app/models/models-seeded-catalog.spec.tsx). The browser keeps the
// dark phone render: overflow and axe on a catalog with timing and engine data.
test("models polish 2 metadata rows fit a dark 390 px phone and pass axe", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("theme", "dark"));
  await catalog(page, [measured, typical]);
  await expect(
    page.locator('table[aria-label="Available model names"] tbody tr'),
  ).toHaveCount(2);
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  const result = await new AxeBuilder({ page }).analyze();
  expect(
    result.violations.filter((entry) =>
      ["serious", "critical"].includes(entry.impact ?? ""),
    ),
  ).toEqual([]);
  await page.screenshot({
    path: "test-results/screens/polish2-models-dark-390.png",
    fullPage: true,
  });
});

test("opened alternatives and the details sheet stay accessible and inside a dark phone viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("theme", "dark"));
  const peer = {
    ...measured,
    id: "chat-peer",
    display_name: "chat-peer",
    title: "chat-peer",
    use_names: ["chat-peer"],
  };
  await catalog(page, [measured, peer]);
  const check = async () => {
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const result = await new AxeBuilder({ page }).analyze();
    expect(
      result.violations
        .filter((violation) =>
          ["serious", "critical"].includes(violation.impact ?? ""),
        )
        .map((violation) => violation.id),
    ).toEqual([]);
  };
  await page.getByText("1 more", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Copy model name chat-peer" }),
  ).toBeVisible();
  await check();
  await page
    .getByRole("button", { name: "Copy model name chat-peer" })
    .locator("xpath=ancestor::tr")
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await check();
});
