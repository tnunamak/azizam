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
        'table[aria-label="Available model aliases"] tbody tr',
      );
      await expect(cards).toHaveCount(2);
      const aliasGroups = page.getByLabel("Available model aliases");
      await expect(
        aliasGroups.getByText("Local · no provider charge", { exact: true }),
      ).toHaveCount(0);
      await expect(
        cards.first().getByText("Local", { exact: true }),
      ).toHaveCount(0);
      await expect(cards.first().getByText("Household chat")).toHaveCount(0);
      await expect(cards.first().getByText("~151 ms · Measured")).toHaveCount(
        0,
      );
      await expect(
        cards.first().getByText("Unknown", { exact: true }),
      ).toHaveCount(0);
      await expect(
        cards.first().getByText("Live availability is not measured yet"),
      ).toHaveCount(0);
      await expect(
        cards.nth(1).getByText("Remote", { exact: true }),
      ).toHaveCount(0);
      await expect(
        cards.nth(1).getByText("Example image provider"),
      ).toHaveCount(0);
      await expect(cards.nth(1).getByText("~8.0 s · Typical")).toHaveCount(0);
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
        page.getByRole("dialog").getByText("Household chat"),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("Measured time"),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("~151 ms · Measured"),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await cards.nth(1).getByRole("button", { name: "Details" }).click();
      await expect(
        page.getByRole("dialog").getByText("Example image provider"),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("~8.0 s · Typical"),
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
  await expect(page.getByText("Configured engine")).toHaveCount(0);
  await expect(
    page
      .getByLabel("Available model aliases")
      .getByText("Not measured yet", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Details" }).click();
  await expect(
    page.getByRole("dialog").getByText("Not measured yet", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("dialog").getByText("Typical time", { exact: true }),
  ).toHaveCount(0);
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
    page.getByRole("button", { name: "Copy model name chat" }),
  ).toBeVisible();
});

test("aliases keep alternatives within their server-classified capability", async ({
  page,
}) => {
  const peer = (
    id: string,
    intent: string,
    capability: string,
    label: string,
  ) => ({
    ...measured,
    id,
    display_name: id,
    title: id,
    capability,
    capability_title: label,
    icon_key: capability,
    use_names: [id],
    operations: [capability],
    capabilities: [{ intent, label, capability, icon_key: capability }],
  });
  await catalog(page, [
    peer("default", "chat.completions", "chat", "Chat"),
    peer("image", "image.generate", "image", "Images"),
    peer("decide", "decision.systemone", "decide", "Decisions"),
    peer("tts", "speech.synthesis", "speech", "Speech"),
    peer("chat-peer", "chat.completions", "chat", "Chat"),
    peer("image-peer", "image.generate", "image", "Images"),
  ]);

  // A capability is one rowgroup, plus its alternatives' rowgroup once opened.
  const groupOf = (label: string, key: string) =>
    page
      .getByRole("rowgroup", { name: `${label} models` })
      .or(page.locator(`tbody[id="model-alternatives-${key}"]`));
  for (const [label, key, primary, alternative] of [
    ["Chat", "chat", "default", "chat-peer"],
    ["Images", "image", "image", "image-peer"],
    // Decisions read as the task they do, the name Try uses.
    ["Choose an option", "decide", "decide", null],
    ["Speech", "speech", "tts", null],
  ] as const) {
    const group = groupOf(label, key);
    await expect(
      group.getByRole("heading", { name: primary, exact: true }),
    ).toBeVisible();
    if (alternative) {
      await expect(group.getByText("1 more", { exact: true })).toBeVisible();
      const copyAlternative = group.getByRole("button", {
        name: `Copy model name ${alternative}`,
      });
      await expect(copyAlternative).toBeHidden();
      await group.getByText("1 more", { exact: true }).click();
      await expect(copyAlternative).toBeVisible();
    } else {
      await expect(group.getByText(/\d+ more/)).toHaveCount(0);
    }
  }
  const chatGroup = groupOf("Chat", "chat");
  await expect(
    chatGroup.getByRole("button", { name: "Copy model name image-peer" }),
  ).toHaveCount(0);
  const imageGroup = groupOf("Images", "image");
  await expect(
    imageGroup.getByRole("button", { name: "Copy model name chat-peer" }),
  ).toHaveCount(0);
});
