import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type {
  CatalogRow,
  ModelsCatalog,
  AvailabilityStatus,
} from "../../lib/product-api";

function row(
  id: string,
  status: AvailabilityStatus,
  overrides: Partial<CatalogRow> = {},
): CatalogRow {
  return {
    id,
    object: "model_catalog.alias",
    display_name: id,
    name: id,
    alias: true,
    operations: ["chat"],
    resolves_to: { chat: `${id}-model` },
    description: `Chat through ${id}.`,
    capabilities: [
      {
        intent: "chat.completions",
        label: "Chat",
        input_modalities: ["text"],
        output_modalities: ["text"],
        streaming: true,
        available: true,
      },
    ],
    provider: "fixture",
    engine_label: "Household chat",
    locality: "local",
    cost: { kind: "unknown", price: null },
    timing: { typical_seconds: null, source: null },
    availability: {
      available: status === "ready" || status === "idle",
      status,
      reason: `${id} observation.`,
      measured: status !== "unknown",
      enabled: true,
      configured: true,
      up: status === "unknown" ? null : status !== "unavailable",
      idle_startable: status === "idle",
      evidence:
        status === "unknown"
          ? {}
          : { source: "health", observed_at_s: 1791072000 },
    },
    io: { input: ["text"], output: ["text"] },
    examples: [
      {
        kind: "chat",
        body: { model: id, messages: [{ role: "user", content: "Hello" }] },
      },
    ],
    recent_receipts: [],
    use: {
      model: id,
      path: "/v1/chat/completions",
      curl: null,
      python: { path: "/v1/chat/completions", model: id },
      javascript: null,
    },
    ...overrides,
  };
}
const states: AvailabilityStatus[] = [
  "ready",
  "idle",
  "starting",
  "busy",
  "unknown",
  "unavailable",
];
const aliases = states.map((status) => row(status, status));
aliases.push(
  row("catalog-only", "unavailable", {
    availability: {
      available: false,
      status: "unavailable",
      reason: "No serving capability is configured",
      measured: false,
      configured: false,
      enabled: false,
    },
  }),
);
const named = row("named-down", "unavailable", {
  alias: false,
  object: "model_catalog.model",
});
const seeded: ModelsCatalog = {
  schema_version: 1,
  object: "models.catalog",
  aliases,
  models: [named],
  counts: { aliases: 7, models: 1, capabilities: 8 },
};
const empty: ModelsCatalog = {
  ...seeded,
  aliases: [],
  models: [],
  counts: { aliases: 0, models: 0, capabilities: 0 },
};

async function prepare(
  page: Page,
  theme: string,
  width: number,
  payload: ModelsCatalog,
  status = 200,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({
    colorScheme: theme as "light" | "dark",
    reducedMotion: "reduce",
  });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({ status, json: payload }),
  );
  await page.goto("/ui/models/");
}
async function verify(page: Page, name: string) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const axe = await new AxeBuilder({ page }).analyze();
  expect(
    axe.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
  await page.screenshot({
    path: `test-results/screens/models-audit-${name}.png`,
    fullPage: true,
  });
}
for (const theme of ["light", "dark"])
  for (const width of [1440, 390]) {
    test(`contract states ${theme} ${width}`, async ({ page }) => {
      await prepare(page, theme, width, seeded);
      await expect(page.getByText("Ready", { exact: true })).toBeVisible();
      await expect(
        page.getByText("Ready to start", { exact: true }),
      ).toBeVisible();
      await expect(page.getByText("Starting", { exact: true })).toBeVisible();
      await expect(page.getByText("Busy", { exact: true })).toBeVisible();
      await expect(page.getByText("Unknown", { exact: true })).toBeVisible();
      await expect(page.getByText("Unavailable", { exact: true })).toHaveCount(
        2,
      );
      await expect(
        page
          .locator("article")
          .getByText("Not measured yet", { exact: true })
          .first(),
      ).toBeVisible();
      await expect(
        page.getByText("Price not provided", { exact: true }).first(),
      ).toBeVisible();
      await verify(page, `states-${theme}-${width}`);
      await page.getByRole("button", { name: /All models/ }).click();
      const table = page.getByRole("table");
      await expect(
        table.getByText("Configured", { exact: true }),
      ).toBeVisible();
      await expect(
        table.getByRole("link", { name: "Check status" }),
      ).toHaveAttribute("href", "/ui/status/");
      await verify(page, `table-${theme}-${width}`);
    });
    test(`empty and error recovery ${theme} ${width}`, async ({ page }) => {
      await prepare(page, theme, width, empty);
      await expect(page.getByText("No models are available yet")).toBeVisible();
      await verify(page, `empty-${theme}-${width}`);
      await page.route("**/me/api/models/catalog", (route) =>
        route.fulfill({
          status: 503,
          json: { error: { message: "Temporary failure" } },
        }),
      );
      await page.reload();
      await expect(
        page.getByText("Models catalog could not be loaded. Try again."),
      ).toBeVisible();
      await verify(page, `error-${theme}-${width}`);
      await page.route("**/me/api/models/catalog", (route) =>
        route.fulfill({ json: seeded }),
      );
      await page.getByRole("button", { name: /try again|retry/i }).click();
      await expect(page.getByText("Ready", { exact: true })).toBeVisible();
    });
  }

for (const theme of ["light", "dark"])
  for (const width of [1440, 390]) {
    test(`live LAN discovery replay ${theme} ${width}`, async ({ page }) => {
      test.skip(
        !process.env.MODELS_LIVE_REPLAY,
        "Set MODELS_LIVE_REPLAY to a public discovery catalog capture",
      );
      const { readFile } = await import("node:fs/promises");
      const payload = JSON.parse(
        await readFile(process.env.MODELS_LIVE_REPLAY!, "utf8"),
      ) as ModelsCatalog;
      expect(
        payload.models.some((r) =>
          r.capabilities.some((c) => c.intent === "chat.completions"),
        ),
      ).toBe(true);
      await prepare(page, theme, width, payload);
      await page
        .getByRole("button", { name: /Show all models|All models/ })
        .first()
        .click();
      await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(
        payload.models.length,
      );
      await verify(page, `live-${theme}-${width}`);
    });
  }

test("member recovery keeps owner controls out of the catalog", async ({
  page,
}) => {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "member",
          name: "Member",
          email: "member@example.test",
          role: "member",
        },
        apps: [],
        csrf_token: "fixture",
      },
    }),
  );
  await prepare(page, "light", 390, seeded);
  const card = page.locator("article").filter({
    has: page.getByRole("button", {
      name: "Copy alias catalog-only",
      exact: true,
    }),
  });
  await expect(
    card.getByRole("link", { name: "Check status" }),
  ).toHaveAttribute("href", "/ui/status/");
  await expect(
    page.getByRole("link", { name: "Connect an engine" }),
  ).toHaveCount(0);
  await verify(page, "member-390");
});

test("loading finishes with observed models", async ({ page }) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/me/api/models/catalog", async (route) => {
    await pending;
    await route.fulfill({ json: seeded });
  });
  await page.goto("/ui/models/");
  await expect(
    page.getByRole("status", { name: "Loading models" }),
  ).toHaveAttribute("aria-busy", "true");
  release();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
});
