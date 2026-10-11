import {
  test,
  expect,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/** Open the engines table: folded under a card below 1280 px, open above. */
async function openLayerTable(main: Locator, summary: string) {
  const button = main.getByRole("button", { name: summary, exact: true });
  const table = main.getByRole("table", { name: "Engine status" });
  // Wait for the page to settle on one of the two before deciding: a bare
  // isVisible() raced the first render.
  await expect(button.or(table)).toBeVisible();
  if (await button.isVisible()) await button.click();
  await expect(table).toBeVisible();
}

const registry = {
  schema_version: 1,
  services: {
    chat: {
      id: "chat",
      name: "Local chat engine",
      kind: "llama",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["chat"],
      role: "primary chat",
      config: {
        base_url: "http://127.0.0.1:18081/v1",
        protocol: "openai-compatible",
        models: [{ model_group: "demo-chat", upstream_model: "demo-chat" }],
      },
    },
    image: {
      id: "image",
      name: "Local image engine",
      kind: "comfy",
      source: "ui",
      enabled: false,
      editable: true,
      capabilities: ["image", "video"],
      role: "image",
      config: { base_url: "http://127.0.0.1:18082" },
    },
    imageOnly: {
      id: "imageOnly",
      name: "Image-only engine",
      kind: "comfy",
      source: "ui",
      enabled: true,
      editable: true,
      capabilities: ["image"],
      role: "image",
      config: { base_url: "http://127.0.0.1:18083" },
    },
  },
};

const currentStatus = {
  current_health: {
    state: "warn",
    tone: "warn",
    label: "One engine needs attention",
    detail:
      "The chat engine is reachable; one disabled engine is hidden by default.",
    observation_available: true,
    signals: [
      {
        source: "disk",
        tone: "warn",
        reasons: ["Media disk (/media) is above the warning threshold."],
      },
    ],
    services: [
      {
        service_id: "chat",
        name: "Local chat engine",
        engine_label: "Local chat engine",
        capability_title: "Chat",
        capability_labels: ["Chat"],
        icon_key: "chat",
        configured: true,
        enabled: true,
        role: "primary chat",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "image",
        name: "Local image engine",
        engine_label: "Local image engine",
        capability_title: "Images",
        capability_labels: ["Images", "Video"],
        icon_key: "image",
        configured: true,
        enabled: false,
        role: "image",
        routable: null,
        observation: "disabled",
        tone: "neutral",
        last_error: null,
      },
      {
        service_id: "imageOnly",
        name: "Image-only engine",
        engine_label: "Image-only engine",
        capability_title: "Images",
        capability_labels: ["Images"],
        icon_key: "image",
        configured: true,
        enabled: true,
        role: "image",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
    ],
  },
  generated_at: Math.floor(Date.now() / 1000),
  backends: [
    { service_id: "chat", url: "http://127.0.0.1:18081/v1", latency_ms: 42 },
  ],
  gpus: [{ index: 0, memory_used_mb: 12288, memory_total_mb: 24576 }],
  budgets: { gpu0: { total_gb: 24, headroom_gb: 4 } },
  ram_attribution: {
    total_kb: 64 * 1024 * 1024,
    available_kb: 24 * 1024 * 1024,
  },
  disk: {
    partitions: [
      {
        source: "/dev/nvme0n1p2",
        target: "/media",
        display_name: "Disk (/media)",
        size_bytes: 1000,
        used_bytes: 930,
        avail_bytes: 70,
        pct_used: 93,
      },
    ],
    hotspots: [],
  },
  thermal: {
    amdgpu_edge_c: 95,
    amdgpu_thermal_state: "warn",
  },
};

async function mockStatusApis(
  page: Page,
  { empty = false, statusError = false, providers = 404 } = {},
) {
  await page.route("**/me/api/session", (route: Route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "owner",
          name: "Owner",
          email: "owner@example.test",
          role: "owner",
        },
        apps: [],
      },
    }),
  );
  await page.route("**/admin/api/status", (route: Route) => {
    if (statusError) {
      return route.fulfill({
        status: 503,
        json: {
          error: { code: "status_unavailable", message: "Status probe failed" },
        },
      });
    }
    return route.fulfill({
      json: {
        status: empty
          ? {
              current_health: {
                state: "empty",
                tone: "neutral",
                label: "No engines configured",
                detail: "Add an engine to start routing.",
                observation_available: false,
                services: [],
              },
              backends: [],
            }
          : currentStatus,
      },
    });
  });
  await page.route("**/admin/api/operator/service-registry", (route: Route) =>
    route.fulfill({
      json: {
        service_registry: empty
          ? { schema_version: 1, services: {} }
          : registry,
      },
      headers: { etag: "test-etag" },
    }),
  );
  await page.route("**/admin/api/providers", (route: Route) => {
    if (providers === 404)
      return route.fulfill({
        status: 404,
        json: { error: { message: "not found" } },
      });
    return route.fulfill({
      status: providers,
      json: {
        providers: empty ? [] : [
          {
            id: "openrouter",
            name: "OpenRouter",
            enabled: true,
            reachable: true,
            key_set: true,
            spend_usd: 1.25,
            cap_usd: 10,
          },
        ],
      },
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`status ${state} ${theme} ${width}`, async ({ page }) => {
        await mockStatusApis(page, {
          empty: state === "empty",
          statusError: state === "error",
          providers: state !== "error" ? 200 : 404,
        });
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        await page.goto("/ui/status/");
        await expect(
          page.getByRole("heading", { name: "Status", level: 1 }),
        ).toBeVisible();
        const main = page.locator("main");
        if (state === "seeded") {
          await expect(
            main.getByText(/Media disk .*above the warning threshold/),
          ).toBeVisible();
          // D1: while a card is open, the layer table folds to one line of
          // counts below 1280 px (D1.5: open beside the card above it); the
          // disabled engine is counted and listed as Off.
          await openLayerTable(
            main,
            "Engines: 2 ready, 1 turned off. Remote providers: 1 set up. Show all",
          );
          await expect(
            main.getByRole("row", { name: /Image-only engine/ }),
          ).toBeVisible();
          const disabled = main.getByRole("row", {
            name: /Local image engine/,
          });
          await expect(disabled).toContainText("Off");
          await expect(disabled).not.toContainText("Ready");
          await expect(
            // D1.5: the row header reads "OpenRouter Remote provider".
            main.getByRole("rowheader", { name: /^OpenRouter\b/ }),
          ).toBeVisible();
          const providerDetails = page.getByRole("button", { name: "OpenRouter details" });
          await providerDetails.click();
          const providerSheet = page.getByRole("dialog", { name: "OpenRouter" });
          // The sheet repeats the row's spend evidence, never "not recorded" beside it.
          await expect(providerSheet.getByText("USD 1.25 of USD 10.00")).toBeVisible();
          await page.keyboard.press("Escape");
          await expect(providerSheet).toHaveCount(0);
          await expect(providerDetails).toBeFocused();
          await page.getByRole("button", { name: "Status actions" }).click();
          const details = page.getByRole("menuitem", {
            name: /Technical details/,
          });
          await expect(details).toHaveAttribute("href", "/status/raw");
          await page.keyboard.press("Escape");
          await expect(details).toHaveCount(0);
        } else if (state === "empty") {
          // D1 copy: the verdict and the layer's empty state, with its action.
          // The page verdict is the first status; the owner's own requests follow.
          await expect(main.getByRole("status").first()).toContainText(
            "No engines yet",
          );
          await expect(
            main.getByRole("heading", { name: "No engines yet", level: 2 }),
          ).toBeVisible();
          await expect(
            main.getByRole("link", { name: "Add an engine", exact: true }),
          ).toHaveAttribute("href", "/ui/engines/");
        } else {
          // D1 copy: the verdict and a card for the failed status check.
          // The page verdict is the first status; the owner's own requests follow.
          await expect(main.getByRole("status").first()).toContainText(
            "Couldn’t check status",
          );
          await expect(
            main.getByRole("heading", {
              name: "Engine health couldn’t load",
            }),
          ).toBeVisible();
          await expect(main.getByText("The gateway returned an error. Try again.")).toBeVisible();
          await expect(main.getByText(/\bready\b/i)).toHaveCount(0);
        }
        await page.screenshot({
          path: `test-results/screens/status-${state}-${theme}-${width}.png`,
          fullPage: width !== 390,
        });
        const violations = (
          await new AxeBuilder({ page }).analyze()
        ).violations.filter((v) =>
          ["serious", "critical"].includes(v.impact ?? ""),
        );
        expect(violations).toEqual([]);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        if (state === "error") {
          await page.unroute("**/admin/api/status");
          await page.route("**/admin/api/status", (route) =>
            route.fulfill({ json: { status: currentStatus } }),
          );
          // D1: Refresh is a header action, no longer in the menu.
          await page
            .getByRole("button", { name: "Refresh", exact: true })
            .click();
          await expect(
            main.getByRole("heading", {
              name: "Engine health couldn’t load",
            }),
          ).toHaveCount(0);
          await expect(
            main
              .getByRole("button", {
                name: "Engines: 2 ready, 1 turned off. Show all",
                exact: true,
              })
              .or(main.getByRole("table", { name: "Engine status" })),
          ).toBeVisible();
        }
      });
    }
  }
}

test("provider failures are visible and retryable", async ({ page }) => {
  await mockStatusApis(page, { providers: 503 });
  await page.goto("/ui/status/");
  const main = page.locator("main");
  // D1: engines and providers share one layer section, folded while a card
  // is open; the provider failure stays visible outside the fold.
  await expect(
    main.getByRole("heading", { name: "Engines and providers", exact: true }),
  ).toBeVisible();
  await expect(
    main.getByText("Provider status did not load.", { exact: true }),
  ).toBeVisible();
  await expect(
    main
      .getByRole("alert")
      .filter({ hasText: "Provider status couldn’t load" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry providers", exact: true }),
  ).toBeVisible();
});

test("failed refresh removes stale green engine observations", async ({
  page,
}) => {
  await mockStatusApis(page);
  await page.goto("/ui/status/");
  const main = page.locator("main");
  // Open the folded layer table so the engines' green states are on screen.
  await openLayerTable(main, "Engines: 2 ready, 1 turned off. Show all");
  const engineRows = main
    .getByRole("row")
    .filter({ hasText: /engine/i })
    .filter({ hasNot: page.getByRole("columnheader") });
  await expect(engineRows.filter({ hasText: "Ready" })).toHaveCount(2);
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: "unavailable", message: "Refresh failed" } },
    }),
  );
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(
    main.getByRole("heading", {
      name: "Engine health couldn’t load",
    }),
  ).toBeVisible();
  await expect(engineRows).toHaveCount(3);
  await expect(engineRows.filter({ hasText: "Ready" })).toHaveCount(0);
  await expect(main.getByText(/engines? ready/i)).toHaveCount(0);
});
