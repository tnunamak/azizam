import { expect, test, type Page, type Route } from "@playwright/test";
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
      role: "primary",
      config: {
        host: "127.0.0.1",
        port: 18081,
        base_path: "/v1",
        protocol: "openai-compatible",
        health_endpoint: "/health",
        custom_flag: "keep-me",
        models: { "legacy-chat": { upstream_model: "legacy-upstream" } },
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
      role: "optional",
      config: { base_url: "http://127.0.0.1:18082" },
    },
    managed: {
      id: "managed",
      name: "Gateway substrate",
      kind: "litellm",
      source: "builtin",
      enabled: true,
      editable: false,
      capabilities: ["chat"],
      role: "required",
      config: {
        base_url: "http://litellm:4000",
        protocol: "openai-compatible",
        note: "Managed by the gateway.",
      },
    },
  },
};

const status = {
  current_health: {
    state: "ok",
    tone: "ok",
    label: "Ready",
    detail: "Configured engines are reachable.",
    observation_available: true,
    services: [
      {
        service_id: "chat",
        name: "Local chat engine",
        configured: true,
        enabled: true,
        role: "primary",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "managed",
        name: "Gateway substrate",
        configured: true,
        enabled: true,
        role: "required",
        routable: true,
        observation: "up",
        tone: "ok",
        last_error: null,
      },
      {
        service_id: "image",
        name: "Local image engine",
        configured: true,
        enabled: false,
        role: "optional",
        routable: null,
        observation: "disabled",
        tone: "neutral",
        last_error: null,
      },
    ],
  },
  backends: [{ service_id: "chat", live_status: "up", latency_ms: 42 }],
};

function requestBody(route: Route): unknown {
  const body = route.request().postData();
  return body ? route.request().postDataJSON() : null;
}

async function mockEngineApis(
  page: Page,
  options: {
    empty?: boolean;
    registryError?: boolean;
    statusError?: boolean;
    registryOverride?: typeof registry;
    statusOverride?: unknown;
    bodies?: unknown[];
  } = {},
) {
  const activeRegistry = options.registryOverride ?? registry;
  await page.route("**/admin/api/status", (route) => {
    if (options.statusError) {
      return route.fulfill({
        status: 503,
        json: {
          error: { code: "status_down", message: "Status refresh failed" },
        },
      });
    }
    return route.fulfill({
      json: {
        status:
          options.statusOverride ?? (options.empty ? { backends: [] } : status),
      },
    });
  });
  await page.route("**/admin/api/operator/service-presets", (route) =>
    route.fulfill({
      json: {
        presets: [
          {
            id: "openai-compatible",
            label: "OpenAI compatible",
            base_path: "/v1",
            default_port: null,
            protocol: "openai-compatible",
            capabilities: ["chat"],
            native_passthrough: false,
          },
        ],
      },
    }),
  );
  await page.route("**/admin/api/operator/service-registry", (route) => {
    if (options.registryError) {
      return route.fulfill({
        status: 503,
        json: { error: { code: "offline", message: "Registry offline" } },
      });
    }
    return route.fulfill({
      json: {
        service_registry: options.empty
          ? { schema_version: 1, services: {} }
          : activeRegistry,
      },
      headers: { etag: "test-etag" },
    });
  });
  await page.route("**/admin/api/operator/services/test", (route: Route) =>
    route.fulfill({
      json: {
        probe: {
          ok: true,
          status: 200,
          error: "",
          url: "http://127.0.0.1:18081/v1/health",
        },
      },
    }),
  );
  await page.route("**/admin/api/operator/services/detect-models", (route) =>
    route.fulfill({
      json: { detected: { ok: true, models: ["demo-chat"], error: "" } },
    }),
  );
  await page.route("**/admin/api/operator/services", async (route) => {
    options.bodies?.push(requestBody(route));
    route.fulfill({
      json: {
        service_registry: {
          ...activeRegistry,
          services: {
            ...activeRegistry.services,
            added: {
              id: "added",
              name: "Added engine",
              kind: "openai-compatible",
              source: "ui",
              enabled: true,
              editable: true,
              capabilities: ["chat"],
              role: "optional",
              config: { base_url: "http://127.0.0.1:18083/v1" },
            },
          },
        },
      },
      headers: { etag: "test-etag-2" },
    });
  });
  await page.route(
    "**/admin/api/operator/services/chat/enabled",
    async (route) => {
      options.bodies?.push(requestBody(route));
      return route.fulfill({
        json: {
          service_registry: {
            ...registry,
            services: {
              ...activeRegistry.services,
              chat: { ...activeRegistry.services.chat, enabled: false },
            },
          },
        },
        headers: { etag: "test-etag-2" },
      });
    },
  );
  await page.route("**/admin/api/operator/services/image", async (route) => {
    options.bodies?.push(requestBody(route));
    return route.fulfill({
      json: {
        service_registry: {
          ...activeRegistry,
          services: { chat: activeRegistry.services.chat },
        },
      },
      headers: { etag: "test-etag-remove" },
    });
  });
  await page.route("**/admin/api/operator/services/managed", async (route) => {
    options.bodies?.push(requestBody(route));
    return route.fulfill({
      json: {
        service_registry: {
          ...activeRegistry,
          services: {
            ...activeRegistry.services,
            managed: {
              ...registry.services.managed,
              name: "Renamed substrate",
              config: {
                ...activeRegistry.services.managed.config,
                note: "Managed note changed.",
              },
            },
          },
        },
      },
      headers: { etag: "test-etag-managed" },
    });
  });
  await page.route("**/admin/api/operator/services/chat", async (route) => {
    options.bodies?.push(requestBody(route));
    return route.fulfill({
      json: {
        service_registry: {
          ...activeRegistry,
          services: {
            ...activeRegistry.services,
            chat: {
              ...registry.services.chat,
              name: "Renamed chat engine",
              config: {
                ...activeRegistry.services.chat.config,
                host: "127.0.0.1",
                port: 19000,
                base_path: "/v1",
              },
            },
          },
        },
      },
      headers: { etag: "test-etag-3" },
    });
  });
}

async function checkPageBasics(page: Page, screenshotName: string) {
  const violations = (
    await new AxeBuilder({ page }).analyze()
  ).violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/${screenshotName}.png`,
    fullPage: true,
  });
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`engines seeded ${theme} ${width}`, async ({ page }) => {
      await mockEngineApis(page);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/ui/engines/");

      await expect(
        page.getByRole("heading", { name: "Engines", level: 1 }),
      ).toBeVisible();
      await expect(
        page
          .locator("article")
          .filter({ hasText: "Local chat engine" })
          .getByRole("heading", { name: "Chat (openai-compatible)" }),
      ).toBeVisible();
      await expect(
        page.getByText("legacy-chat", { exact: true }),
      ).toBeVisible();
      await expect(page.getByText("Image & Video (comfy)")).toHaveCount(0);
      await page.getByText("Show disabled").click();
      await expect(page.getByText("Image & Video (comfy)")).toBeVisible();

      const chatCard = page
        .locator("article")
        .filter({ hasText: "Local chat engine" });
      await chatCard.getByRole("button", { name: "Test" }).click();
      await expect(page.getByText("Connected to")).toBeVisible();
      await chatCard.getByRole("button", { name: "Detect" }).click();
      await expect(page.getByText("Detected models: demo-chat")).toBeVisible();
      await chatCard.getByRole("button", { name: "Edit" }).click();
      await expect(
        page.getByRole("dialog", { name: "Edit engine" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");

      await checkPageBasics(
        page,
        `status-engines-services-seeded-${theme}-${width}`,
      );
    });

    test(`engines empty ${theme} ${width}`, async ({ page }) => {
      await mockEngineApis(page, { empty: true });
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/ui/engines/");
      await expect(page.getByText("No engines configured")).toBeVisible();
      await checkPageBasics(
        page,
        `status-engines-services-empty-${theme}-${width}`,
      );
    });

    test(`engines error ${theme} ${width}`, async ({ page }) => {
      await mockEngineApis(page, { registryError: true });
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/ui/engines/");
      await expect(page.getByText("Engines unavailable")).toBeVisible();
      await expect(page.getByText("Registry offline")).toBeVisible();
      await checkPageBasics(
        page,
        `status-engines-services-error-${theme}-${width}`,
      );
    });
  }
}

test("engines do not infer green state when observations are unavailable", async ({
  page,
}) => {
  await mockEngineApis(page, {
    statusOverride: {
      current_health: {
        state: "unavailable",
        tone: "neutral",
        label: "Live status unavailable",
        detail: "The gateway could not observe engines.",
        observation_available: false,
        services: status.current_health.services,
      },
      backends: [{ service_id: "chat", live_status: "up" }],
    },
  });
  await page.goto("/ui/engines/");
  await expect(
    page.locator("main").getByText("Not measured yet").first(),
  ).toBeVisible();
  await expect(page.getByText("Ready")).toHaveCount(0);
});

test("engines route selects a disabled service from the query string", async ({
  page,
}) => {
  await mockEngineApis(page);
  await page.goto("/ui/engines/?service=image");
  await expect(page.getByText("Image & Video (comfy)")).toBeVisible();
  await expect(
    page
      .locator("article[aria-current='true']")
      .getByText("Local image engine"),
  ).toBeVisible();
});

test("engines clear live projection when status refresh fails", async ({
  page,
}) => {
  await mockEngineApis(page, { statusError: true });
  await page.goto("/ui/engines/");
  await expect(page.getByText("Live status unavailable")).toBeVisible();
  await expect(page.getByText("Status refresh failed")).toBeVisible();
  await expect(
    page.locator("main").getByText("Not measured yet").first(),
  ).toBeVisible();
  await expect(page.getByText("Ready")).toHaveCount(0);
});

test("engines add, edit, toggle, and remove use existing APIs safely", async ({
  page,
}) => {
  const bodies: unknown[] = [];
  await mockEngineApis(page, { bodies });
  await page.goto("/ui/engines/");

  await page.getByRole("button", { name: "Add engine" }).first().click();
  const addDialog = page.getByRole("dialog", { name: "Add engine" });
  await addDialog.getByRole("combobox", { name: "Preset" }).click();
  await addDialog.getByRole("option", { name: "OpenAI compatible" }).click();
  await addDialog.getByLabel("Name").fill("Added engine");
  await addDialog.getByLabel("Base URL").fill("http://127.0.0.1:18083/v1");
  await addDialog.getByRole("button", { name: "Add engine" }).click();
  await expect(page.getByText("Engine added")).toBeVisible();

  const chatCard = page
    .locator("article")
    .filter({ hasText: "Local chat engine" });
  await chatCard.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit engine" });
  await dialog.getByLabel("Name").fill("Renamed chat engine");
  await dialog.getByLabel("Endpoint").fill("https://example.test/v1");
  await dialog.getByRole("button", { name: "Save engine" }).click();
  await expect(
    dialog.getByText(/HTTPS endpoints are not supported/),
  ).toBeVisible();
  await dialog.getByText("Advanced fields", { exact: true }).click();
  const config = await dialog
    .getByLabel("Advanced JSON", { exact: true })
    .inputValue();
  await dialog.getByLabel("Advanced JSON", { exact: true }).fill("null");
  await dialog.getByRole("button", { name: "Save engine" }).click();
  await expect(
    dialog.getByText("Advanced fields must be a JSON object."),
  ).toBeVisible();
  await dialog.getByLabel("Advanced JSON", { exact: true }).fill(config);
  await dialog.getByLabel("Endpoint").fill("http://127.0.0.1:19000/v1");
  await dialog.getByRole("button", { name: "Save engine" }).click();
  await expect(page.getByText("Engine saved")).toBeVisible();

  await page
    .locator("article")
    .filter({ hasText: "Gateway substrate" })
    .getByRole("button", { name: "Edit" })
    .click();
  const managedDialog = page.getByRole("dialog", { name: "Edit engine" });
  await managedDialog.getByLabel("Name").fill("Renamed substrate");
  await managedDialog.getByLabel("Note").fill("Managed note changed.");
  await expect(managedDialog.getByLabel("Endpoint")).toHaveCount(0);
  await managedDialog.getByRole("button", { name: "Save engine" }).click();
  await expect(page.getByText("Engine saved")).toBeVisible();

  await page.getByRole("switch", { name: "Toggle Local chat engine" }).click();
  await page.getByText("Show disabled").click();
  const imageCard = page
    .locator("article")
    .filter({ hasText: "Local image engine" });
  await imageCard
    .getByRole("button", { name: "Actions for Local image engine" })
    .click();
  await page.getByRole("menuitem", { name: "Remove engine" }).click();
  await page
    .getByRole("dialog", { name: "Remove engine?", exact: true })
    .getByRole("textbox")
    .fill("Local image engine");
  await page
    .getByRole("button", { name: "Remove engine", exact: true })
    .click();
  await expect(page.getByText("Engine removed")).toBeVisible();

  expect(bodies).toContainEqual(
    expect.objectContaining({
      name: "Added engine",
      preset: "openai-compatible",
      base_url: "http://127.0.0.1:18083/v1",
    }),
  );
  expect(bodies).toContainEqual({ enabled: false });
  expect(bodies).toContainEqual({
    name: "Renamed substrate",
    note: "Managed note changed.",
  });
  expect(bodies).toContainEqual(
    expect.objectContaining({
      name: "Renamed chat engine",
      host: "127.0.0.1",
      port: 19000,
      base_path: "/v1",
      custom_flag: "keep-me",
      enabled: true,
    }),
  );
  const editBody = bodies.find(
    (body): body is Record<string, unknown> =>
      Boolean(body) &&
      typeof body === "object" &&
      (body as Record<string, unknown>).port === 19000,
  );
  expect(editBody).toBeTruthy();
  expect(editBody).not.toHaveProperty("base_url");
});

test("engines label degraded and standby states honestly", async ({ page }) => {
  await mockEngineApis(page, {
    registryOverride: {
      ...registry,
      services: {
        ...registry.services,
        image: { ...registry.services.image, enabled: true },
      },
    },
    statusOverride: {
      current_health: {
        state: "warn",
        tone: "warn",
        label: "Engines need attention",
        detail: "One engine is degraded and one is stopped on standby.",
        observation_available: true,
        services: [
          {
            service_id: "chat",
            name: "Local chat engine",
            configured: true,
            enabled: true,
            role: "primary",
            routable: true,
            observation: "degraded",
            tone: "warn",
            last_error: null,
          },
          {
            service_id: "image",
            name: "Local image engine",
            configured: true,
            enabled: true,
            role: "optional",
            routable: true,
            observation: "standby_stopped",
            tone: "neutral",
            last_error: null,
          },
        ],
      },
      backends: [],
    },
  });
  await page.goto("/ui/engines/");
  await expect(
    page.getByText("Unavailable: probe reports degraded"),
  ).toBeVisible();
  await expect(
    page.getByText("Unavailable: stopped on standby; starts on demand"),
  ).toBeVisible();
  await expect(page.getByText("Busy")).toHaveCount(0);
  await expect(page.getByText("Starting")).toHaveCount(0);
});

test("services compatibility route is owner gated", async ({ page }) => {
  const adminCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
      adminCalls.push(request.url());
    }
  });
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
      },
    }),
  );
  await page.goto("/ui/services/");
  await expect(
    page.getByText(
      "This page is available to gateway owners and admins. Sign in with an owner account to continue.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(adminCalls).toEqual([]);
});
