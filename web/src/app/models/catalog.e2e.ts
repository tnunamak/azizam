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
        capability: "chat",
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
const aliases = states.map((status) =>
  row(status, status, status === "unknown" ? { locality: "remote" } : {}),
);
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
  role = "owner",
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
  await routeSession(page, role);
  await page.goto("/ui/models/");
}

async function routeSession(page: Page, role = "owner") {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: role,
          name: role === "member" ? "Member" : "Owner",
          email: `${role}@example.test`,
          role,
        },
        apps: [],
        csrf_token: "fixture",
      },
    }),
  );
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
      await expect(page.getByText("Ready", { exact: true })).toHaveCount(0);
      await page.getByRole("button", { name: "6 more Chat models" }).click();
      await expect(
        page.getByText("Ready to start", { exact: true }),
      ).toBeVisible();
      await expect(page.getByText("Starting", { exact: true })).toBeVisible();
      await expect(page.getByText("Busy", { exact: true })).toBeVisible();
      await expect(
        page.locator("article").getByText("Unknown", { exact: true }),
      ).toHaveCount(0);
      await expect(page.getByText("Unavailable", { exact: true })).toHaveCount(
        2,
      );
      await expect(
        page.locator("article").getByText("Not measured yet", { exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByText("Price not provided", { exact: true }),
      ).toHaveCount(0);
      await expect(page.getByText("Local", { exact: true })).toHaveCount(0);
      const unknownRow = page
        .getByRole("button", { name: "Copy model name unknown" })
        .locator("xpath=..");
      await unknownRow.getByRole("button", { name: "Details" }).click();
      await expect(
        page
          .getByRole("dialog")
          .getByText("Price not provided", { exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("dialog").getByText("Unknown", { exact: true }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      await verify(page, `states-${theme}-${width}`);
      await page.getByRole("button", { name: /All models/ }).click();
      const table = page.getByRole("table");
      await expect(table.getByText("Unavailable").first()).toBeVisible();
      await expect(
        table.getByRole("link", { name: "Check status" }),
      ).toHaveCount(0);
      await table.getByRole("row").filter({ hasText: "named-down" }).click();
      await expect(
        page.getByRole("dialog").getByRole("link", { name: "Check status" }),
      ).toHaveAttribute("href", "/ui/status/");
      await page.keyboard.press("Escape");
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
      await expect(
        page.getByRole("button", { name: "Copy model name ready" }),
      ).toBeVisible();
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
      expect(
        payload.aliases.some(
          (row) =>
            row.id === "default" &&
            row.recommended === true &&
            row.capabilities.some((c) => c.intent === "chat.completions"),
        ),
      ).toBe(true);
      await prepare(page, theme, width, payload);
      const chat = page.locator("section").filter({
        has: page.getByRole("heading", { name: "Chat", exact: true }),
      });
      await expect(
        chat.getByRole("button", { name: "Copy model name default" }),
      ).toBeVisible();
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
  await prepare(page, "light", 390, seeded, 200, "member");
  await page.getByRole("button", { name: "6 more Chat models" }).click();
  await expect(
    page.getByRole("heading", { name: "catalog-only", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Check status" })).toHaveCount(0);
  await page
    .getByRole("article")
    .filter({
      has: page.getByRole("button", {
        name: "Copy model name catalog-only",
        exact: true,
      }),
    })
    .getByRole("button", { name: "Details" })
    .click();
  await expect(
    page.getByRole("dialog").getByRole("link", { name: "Check status" }),
  ).toHaveAttribute("href", "/ui/status/");
  const detailsAxe = await new AxeBuilder({ page }).analyze();
  expect(
    detailsAxe.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
  await page.keyboard.press("Escape");
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
  await routeSession(page);
  await page.goto("/ui/models/");
  await expect(
    page.getByRole("status", { name: "Loading models" }),
  ).toHaveAttribute("aria-busy", "true");
  release();
  await expect(
    page.getByRole("button", { name: "Copy model name ready" }),
  ).toBeVisible();
});

for (const theme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    test(`presentation contract ${theme} ${width}`, async ({ page }) => {
      const profiles = [
        row("coder", "ready", {
          title: "Coder",
          description: "Coding profile for technical work.",
          icon_key: "chat",
          capability_title: "Chat",
        }),
        row("fast", "ready", {
          title: "Fast",
          description: "Direct answers with thinking disabled.",
          icon_key: "chat",
          capability_title: "Chat",
        }),
        row("default", "ready", {
          recommended: true,
          title: "Default",
          description: "Thinks through questions before answering.",
          icon_key: "chat",
          capability_title: "Chat",
          engine_label: "llama.cpp (chat)",
          use_names: ["default", "chat-model"],
        }),
        row("coder-q5", "ready", {
          title: "Coder Q5",
          description: "Coding profile with Q5 weights.",
          secondary_profile: true,
        }),
        row("rp", "ready", {
          alias: false,
          object: "model_catalog.model",
          title: "Roleplay",
          description: "Chat profile for roleplay.",
          secondary_profile: true,
        }),
        row("music", "ready", {
          title: "Music",
          icon_key: "music",
          capability_title: "Music",
          description: "Generate music from a prompt.",
          use_names: ["music", "song", "music-workflow"],
          capabilities: [
            {
              intent: "audio.generate",
              capability: "music",
              icon_key: "music",
              label: "Music",
              input_modalities: ["text"],
              output_modalities: ["audio"],
              available: true,
              streaming: false,
            },
          ],
        }),
      ];
      await prepare(page, theme, width, {
        ...seeded,
        aliases: profiles.filter((profile) => profile.alias),
        models: profiles.filter((profile) => !profile.alias),
        counts: {
          aliases: profiles.filter((profile) => profile.alias).length,
          models: profiles.filter((profile) => !profile.alias).length,
          capabilities: profiles.length,
        },
      });
      const rows = page
        .getByLabel("Available model aliases")
        .locator("article");
      await expect(rows).toHaveCount(2);
      await expect(
        page.getByRole("button", { name: "Audio", exact: true }),
      ).toHaveCount(0);
      for (const title of ["Default", "Music"]) {
        await expect(
          rows.getByRole("heading", { name: title, exact: true, level: 3 }),
        ).toBeVisible();
      }
      await expect(rows.first().getByText("llama.cpp (chat)")).toHaveCount(0);
      await expect(
        rows.first().getByRole("button", { name: "Copy model name default" }),
      ).toBeVisible();
      await rows.first().getByRole("button", { name: "Details" }).click();
      await expect(
        page.getByRole("dialog").getByText("llama.cpp (chat)"),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "4 more Chat models" }).click();
      for (const code of ["fast", "coder"]) {
        await expect(
          page.getByRole("button", {
            name: `Copy model name ${code}`,
            exact: true,
          }),
        ).toBeVisible();
      }
      await expect(
        page.getByRole("heading", { name: "Coder Q5", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Roleplay", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: /More chat profiles/ }),
      ).toHaveCount(0);
      await expect(
        rows.getByRole("img", { name: "Music", exact: true }).locator("svg"),
      ).toHaveClass(/lucide-music-2/);
      await verify(page, `presentation-${theme}-${width}`);
      await expect(page.getByText("Chat profile for roleplay.")).toBeVisible();
      await verify(page, `merged-chat-alternatives-${theme}-${width}`);
      await page.getByRole("textbox", { name: "Search models" }).fill("song");
      await expect(rows).toHaveCount(1);
      await rows.getByRole("button", { name: "Details" }).click();
      await expect(
        page.getByRole("dialog").getByText("Names you can use"),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("song", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog").getByText("music-workflow", { exact: true }),
      ).toBeVisible();
    });
  }
}
