import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";

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

// The seeded, empty and error pictures (copy, rows, folds, provider sheet,
// refresh recovery) are asserted in src/app/status/status-layers.spec.tsx.
// The browser keeps what jsdom cannot see: real layout width, theme tokens
// and axe, one representative per concept.
const cases = [
  { state: "seeded", theme: "light", width: 1440 },
  { state: "seeded", theme: "dark", width: 390 },
  { state: "empty", theme: "light", width: 390 },
  { state: "error", theme: "dark", width: 1440 },
] as const;
for (const { state, theme, width } of cases) {
  test(`status ${state} ${theme} ${width}`, async ({ page }) => {
    await mockStatusApis(page, {
      empty: state === "empty",
      statusError: state === "error",
      providers: state !== "error" ? 200 : 404,
    });
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    await page.goto("/status/");
    await expect(
      page.getByRole("heading", { name: "Status", level: 1 }),
    ).toBeVisible();
    const main = page.locator("main");
    if (state === "seeded") {
      await expect(
        main.getByText(/Media disk .*above the warning threshold/),
      ).toBeVisible();
    } else if (state === "empty") {
      await expect(
        main.getByRole("heading", { name: "No engines yet", level: 2 }),
      ).toBeVisible();
    } else {
      await expect(
        main.getByRole("heading", { name: "Engine health couldn’t load" }),
      ).toBeVisible();
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
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
  });
}
