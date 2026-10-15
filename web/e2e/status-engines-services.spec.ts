import { type Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

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
        models: { "existing-chat": { upstream_model: "existing-upstream" } },
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
      capabilities: [],
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

async function mockEngineApis(
  page: Page,
  options: {
    empty?: boolean;
    registryError?: boolean;
    statusError?: boolean;
  } = {},
) {
  await page.route("**/admin/api/status", (route) => {
    if (options.statusError) {
      return fulfilJson(route, {
          error: { code: "status_down", message: "Status refresh failed" },
        }, { status: 503 });
    }
    return fulfilJson(route, {
        status:
          options.empty ? { backends: [] } : status,
      });
  });
  await page.route("**/admin/api/operator/service-registry", (route) => {
    if (options.registryError) {
      return fulfilJson(route, { error: { code: "offline", message: "Registry offline" } }, { status: 503 });
    }
    return fulfilJson(route, {
        service_registry: options.empty
          ? { schema_version: 1, services: {} }
          : registry,
      }, { headers: { etag: "test-etag" } });
  });
}

async function checkPageBasics(page: Page, screenshotName: string) {
  const violations = (
    await new AxeBuilder({ page }).analyze()
  ).violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(violations).toEqual([]);
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  await page.screenshot({
    path: `test-results/screens/${screenshotName}.png`,
    fullPage: true,
  });
}

function engineCard(page: Page, name: string) {
  return page.locator("tr").filter({ hasText: name });
}

async function openEngine(
  page: Page,
  card: ReturnType<typeof engineCard>,
  serviceName: string,
) {
  await card.getByRole("button", { name: new RegExp(serviceName) }).first().click();
}

// Copy, truth tables and the add / edit / toggle / remove request bodies are
// asserted in src/app/engines/engine-states.spec.tsx. The browser keeps real
// layout width, theme tokens and axe, one representative per concept.
const cases = [
  { state: "seeded", theme: "light", width: 1440 },
  { state: "seeded", theme: "dark", width: 390 },
  { state: "empty", theme: "light", width: 390 },
  { state: "error", theme: "dark", width: 1440 },
] as const;
for (const { state, theme, width } of cases) {
  test(`engines ${state} ${theme} ${width}`, async ({ page }) => {
    await mockEngineApis(page, {
      empty: state === "empty",
      registryError: state === "error",
    });
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    await page.goto("/engines/");
    if (state === "seeded") {
      await expect(
        page.getByRole("heading", { name: "Engines", level: 1 }),
      ).toBeVisible();
      const chatCard = engineCard(page, "Local chat engine");
      await expect(chatCard).toBeVisible();
      // A row opens the engine page; the list comes back on Back.
      await openEngine(page, chatCard, "Local chat engine");
      await expect(page).toHaveURL(/engine=chat/);
      await page.goBack();
      await expect(chatCard).toBeVisible();
    } else if (state === "empty") {
      await expect(page.getByText("No engines configured")).toBeVisible();
    } else {
      await expect(
        page.getByText("Engine list could not be loaded"),
      ).toBeVisible();
    }
    await checkPageBasics(page, `status-engines-${state}-${theme}-${width}`);
  });
}

test("Engines is owner gated for a member", async ({ page }) => {
  const adminCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
      adminCalls.push(request.url());
    }
  });
  await page.route("**/me/api/session", (route) =>
    fulfilJson(route, {
        authenticated: true,
        user: {
          id: "member",
          name: "Member",
          email: "member@example.test",
          role: "member",
        },
        apps: [],
      }),
  );
  await page.goto("/engines/");
  await expect(
    page.getByText("Owner access required", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("This page is available to gateway owners and admins.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign in with an owner account" }),
  ).toHaveAttribute("href", /return_to=%2Fengines%2F/);
  expect(adminCalls).toEqual([]);
});
