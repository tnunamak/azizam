import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const reviewRoot =
  process.env.HOME_AUDIT_SCREEN_DIR ?? "test-results/screens/home-audit";

const app = {
  id: "app-browser",
  name: "Browser test app",
  kind: "personal",
  disabled_at: null,
  created_at: 1700000000,
  profile: null,
  can_mint_keys: true,
  credentials: [
    {
      id: "cred-browser",
      subject_kind: "app",
      subject_id: "app-browser",
      app_id: "app-browser",
      profile_id: null,
      prefix: "opk_test",
      display_name: "Browser key",
      scopes: [],
      created_at: 1700000000,
      expires_at: null,
      revoked_at: null,
      last_used_at: null,
    },
  ],
};

const chatAlias = {
  id: "fast",
  operation: "chat.completions",
  available: true,
  status: "ready",
  reason: "Chat is ready.",
};

function home(role: "owner" | "member" = "owner", empty = false) {
  return {
    authenticated: true,
    csrf_token: "csrf",
    user: {
      id: role,
      name: role === "owner" ? "Alex" : "Mira",
      email: null,
      role,
    },
    apps: empty ? [] : [app],
    public_base_url: "https://gateway.example",
    api_base_url: "https://gateway.example/openai/v1",
    capabilities: empty
      ? []
      : [
          {
            capability: "image",
            intent: "image.generate",
            model: "image",
            available: false,
            status: "unknown",
            reason: "Not measured yet.",
          },
        ],
    aliases: empty ? [] : [chatAlias],
    credential_aliases: empty ? {} : { "cred-browser": [chatAlias] },
    new_app_aliases: empty ? [] : [chatAlias],
    status: empty
      ? { state: "unavailable", reason: "No models are available yet." }
      : { state: "ready", reason: "Ready." },
    receipts: empty
      ? []
      : [
          {
            request_id: "req-1",
            status: "succeeded",
            requested_model: "fast",
            intent: "chat.completions",
            _stored_at: 1700000000,
          },
        ],
    creations: null,
  };
}

function progress(complete = true) {
  return {
    complete,
    steps: [
      {
        id: "connect_service",
        done: true,
        optional: false,
        reason: "A service is connected.",
        href: "/ui/engines/",
      },
      {
        id: "test_request",
        done: true,
        optional: false,
        reason: "A gateway request succeeded.",
        href: "#connect",
      },
      {
        id: "connect_app",
        done: true,
        optional: false,
        reason: "Legacy key use must not complete client setup.",
        href: "#connect",
      },
    ],
    evidence: {
      gateway_request: { recorded: true, source: "receipt" },
      settings_copied: { recorded: false, source: "none" },
      external_client_confirmed: { recorded: false, source: "none" },
      receipt_viewed: { recorded: false, source: "none" },
    },
  };
}

async function setupRoutes(
  page: Page,
  state: "seeded" | "empty" | "error",
  role: "owner" | "member" = "owner",
) {
  await page.route("**/.well-known/ai-gateway", (route) =>
    route.fulfill({ json: { service: { name: "Family gateway" } } }),
  );
  await page.route("**/me/api/media?limit=4", (route) =>
    route.fulfill({ json: { assets: [], next_cursor: null } }),
  );
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "csrf",
        user: { id: role, name: "Alex", role },
      },
    }),
  );
  await page.route("**/admin/api/setup-progress", (route) =>
    route.fulfill({ json: progress(state !== "seeded") }),
  );
  await page.route("**/me/api/home", (route) => {
    if (state === "error") {
      return route.fulfill({
        status: 503,
        json: { error: { message: "Unavailable for this test" } },
      });
    }
    return route.fulfill({ json: home(role, state === "empty") });
  });
}

async function inspect(page: Page, name: string) {
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const violations = (
    await new AxeBuilder({ page }).analyze()
  ).violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact ?? ""),
  );
  expect(violations).toEqual([]);
  await page.screenshot({ path: `${reviewRoot}/${name}.png`, fullPage: true });
}

for (const state of ["seeded", "empty", "error"] as const) {
  for (const theme of ["light", "dark"] as const) {
    for (const width of [1440, 390] as const) {
      test(`Home audit ${state} ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        await setupRoutes(page, state);
        await page.goto("/ui/home/");
        if (state === "error") {
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("heading", { name: "Connect an app", exact: true }),
          ).toBeVisible();
          await page.getByRole("button", { name: "Open WebUI" }).click();
          await expect(
            page.getByText("Base URL", { exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Check connection" }),
          ).toBeVisible();
        }
        await inspect(page, `${state}-${theme}-${width}`);
      });
    }
  }
}

test("fresh owner sees connection and media without setup progress", async ({
  page,
}) => {
  let setupCalls = 0;
  await page.setViewportSize({ width: 390, height: 844 });
  await setupRoutes(page, "seeded", "owner");
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/admin/api/setup-progress") {
      setupCalls += 1;
    }
  });
  await page.goto("/ui/home/");
  await expect(
    page.getByRole("heading", { name: "Connect an app", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "What you can use", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recent", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Finish setting up", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Open WebUI", exact: true }).click();
  await expect(page.getByText("Base URL", { exact: true })).toBeVisible();
  expect(setupCalls).toBe(0);
});

test("member Home keeps owner setup API out and connection task in the first viewport", async ({
  page,
}) => {
  let setupCalls = 0;
  await page.setViewportSize({ width: 390, height: 844 });
  await setupRoutes(page, "seeded", "member");
  await page.route("**/admin/api/setup-progress", (route) => {
    setupCalls += 1;
    return route.fulfill({
      status: 403,
      json: { error: { message: "Owner only" } },
    });
  });
  await page.goto("/ui/home/");
  await page.getByRole("button", { name: "Open WebUI" }).click();
  const baseUrl = page.getByText("Base URL", { exact: true });
  await expect(baseUrl).toBeVisible();
  const box = await baseUrl.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y).toBeLessThan(844);
  expect(setupCalls).toBe(0);
});

test("setup evidence buttons are not shown on Home", async ({ page }) => {
  const posts: unknown[] = [];
  await setupRoutes(page, "seeded", "owner");
  await page.route("**/admin/api/setup-evidence", async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ json: { recorded: true } });
  });
  await page.goto("/ui/home/");
  await page.getByRole("button", { name: "Open WebUI" }).click();
  await expect(
    page.getByRole("button", { name: "I copied the settings" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "My app sent a request" }),
  ).toHaveCount(0);
  expect(posts).toEqual([]);
});

test("real gateway setup evidence stays separate from gateway check", async ({
  page,
  request,
}) => {
  const snapshot = await (await request.get("/me/api/home")).json();
  const seededApp = snapshot.apps.find(
    (item: { name: string }) => item.name === "Browser test app",
  );
  expect(seededApp, "Seeded Browser test app exists").toBeTruthy();

  const adminSession = await (await request.get("/admin/api/session")).json();
  const registry = await request.get("/admin/api/operator/service-registry");
  const configured = await request.patch(
    "/admin/api/operator/services/capture-engine",
    {
      headers: {
        "x-ai-gateway-admin-action": "1",
        "x-ai-gateway-csrf": adminSession.actor.csrf_token,
        "if-match": registry.headers().etag,
      },
      data: { gateway_lifecycle: true },
    },
  );
  expect(configured.ok()).toBe(true);
  const session = await (await request.get("/me/api/session")).json();
  const minted = await request.post(`/me/api/apps/${seededApp.id}/keys`, {
    headers: {
      "x-ai-gateway-user-action": "1",
      "x-ai-gateway-csrf": session.csrf_token,
    },
    data: { label: "Home audit evidence key" },
  });
  expect(minted.status()).toBe(201);
  const { credential } = await minted.json();
  const secret = credential.api_key as string;
  expect(typeof secret).toBe("string");

  await page.goto("/ui/home/");
  await page.getByRole("button", { name: "Open WebUI" }).click();
  await page.getByRole("combobox", { name: "API key" }).click();
  await page.getByRole("option", { name: /Home audit evidence key/ }).click();
  await page.getByLabel("Paste your API key", { exact: true }).fill(secret);

  const checkResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/me/api/connection-test" &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Check connection" }).click();
  const checked = await checkResponse;
  expect(checked.ok()).toBe(true);
  const connection = await checked.json();
  expect(connection.ok).toBe(true);
  expect(typeof connection.request_id).toBe("string");
  expect(connection.request_id.length).toBeGreaterThan(0);
  await expect(
    page.getByText("A one-token local chat request succeeded."),
  ).toBeVisible();

  const afterCheck = await (
    await request.get("/admin/api/setup-progress")
  ).json();
  expect(afterCheck.evidence.gateway_request.recorded).toBe(true);
  expect(afterCheck.evidence.external_client_confirmed.recorded).toBe(false);
  // This shared fixture already has successful app traffic. It completes setup
  // through observed evidence without fabricating a manual confirmation.
  expect(afterCheck.evidence.observed_external_client.recorded).toBe(true);
  expect(afterCheck.complete).toBe(true);
  await expect(
    page.getByRole("heading", { name: "Finish setting up" }),
  ).toHaveCount(0);

  await expect(
    page.getByRole("button", { name: "I copied the settings" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "My app sent a request" }),
  ).toHaveCount(0);

  const receiptResponse = await request.get(
    `/me/api/receipts/${connection.request_id}`,
  );
  expect(receiptResponse.ok()).toBe(true);
  const receiptDetail = await receiptResponse.json();
  expect((receiptDetail.receipt ?? receiptDetail).request_id).toBe(
    connection.request_id,
  );

  const confirmedReceipt = await request.post("/admin/api/setup-evidence", {
    headers: {
      "x-ai-gateway-user-action": "1",
      "x-ai-gateway-csrf": session.csrf_token,
    },
    data: { event: "receipt_viewed", request_id: connection.request_id },
  });
  expect(confirmedReceipt.ok()).toBe(true);
  await expect
    .poll(
      async () =>
        (await (await request.get("/admin/api/setup-progress")).json()).evidence
          .receipt_viewed.recorded,
    )
    .toBe(true);
});
