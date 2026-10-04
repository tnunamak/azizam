import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import identity from "../../gateway/product_identity.json";

const homePath = "/ui/home/";
const homeApi = "**/me/api/home";
const mediaApi = "**/me/api/media?limit=5";
const setupApi = "**/admin/api/setup-progress";

async function chooseRecipeKey(page: Page, optionName: string | RegExp) {
  await page.getByRole("combobox", { name: "App key" }).click();
  await page.getByRole("option", { name: optionName }).click();
}

async function openClientInstructionsOnPhone(page: Page, width: number) {
  if (width !== 390) return;
  if ((await page.getByRole("tabpanel").count()) === 0) {
    await page.getByText("Client instructions", { exact: true }).click();
  }
}

async function inspectHome(page: Page, name: string) {
  // Captures represent settled data, including the independent Library query.
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  await page.locator("main img").evaluateAll(async (images) => {
    await Promise.all(
      images.map((image) =>
        (image as HTMLImageElement).decode().catch(() => {}),
      ),
    );
  });
  await page.evaluate(() => document.fonts.ready);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Home must fit the viewport",
  ).toBe(true);
  await page.screenshot({
    path: `test-results/screens/home/${name}.png`,
    fullPage: true,
  });
  const result = await new AxeBuilder({ page }).analyze();
  const serious = result.violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => ({
      rule: violation.id,
      nodes: violation.nodes.map((node) => ({
        target: node.target,
        reason: node.failureSummary,
      })),
    }));
  expect(serious, "Serious accessibility violations").toEqual([]);
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`Home ${state}: ${theme} at ${width}px`, async ({
        page,
        request,
      }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript((value) => {
          localStorage.setItem("theme", value);
        }, theme);

        if (state === "empty") {
          // State fixtures modify real API responses; the seeded gateway and
          // fake engine remain the boundary for the successful user journey.
          await page.route(homeApi, async (route) => {
            const response = await route.fetch();
            const actual = await response.json();
            await route.fulfill({
              response,
              json: {
                ...actual,
                apps: [],
                capabilities: [],
                aliases: [],
                credential_aliases: {},
                new_app_aliases: [],
                receipts: [],
                creations: null,
                status: {
                  state: "unavailable",
                  reason: "No models are available to your account yet.",
                },
              },
            });
          });
          await page.route(setupApi, (route) =>
            route.fulfill({ json: { complete: true, steps: [] } }),
          );
          await page.route(mediaApi, (route) =>
            route.fulfill({ json: { assets: [], next_cursor: null } }),
          );
        }
        if (state === "seeded") {
          await page.route(mediaApi, (route) =>
            route.fulfill({
              json: {
                assets: [
                  {
                    asset_id: "home-recorded-image",
                    kind: "image",
                    prompt: "A recorded image from your gateway",
                    model: "image",
                    created_at: Date.now() / 1000 - 60,
                    download_url:
                      "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 420'%3E%3Crect width='640' height='420' fill='%23e0f2fe'/%3E%3Ccircle cx='300' cy='210' r='110' fill='%230f766e'/%3E%3Cpath d='M365 210l120-80v160z' fill='%2314b8a6'/%3E%3Ccircle cx='265' cy='178' r='16' fill='white'/%3E%3C/svg%3E",
                  },
                ],
                next_cursor: null,
              },
            }),
          );
          await page.route(setupApi, (route) =>
            route.fulfill({ json: { complete: true, steps: [] } }),
          );
        }
        if (state === "error") {
          await page.route(homeApi, (route) =>
            route.fulfill({
              status: 503,
              json: { error: { message: "Unavailable for this test" } },
            }),
          );
        }

        const response = await page.goto(homePath);
        expect(response?.status()).toBe(200);
        if (state !== "error") {
          await expect(
            page.getByRole("heading", { level: 1, name: /^Hello,/ }),
          ).toBeVisible();
        }
        if (state === "seeded") {
          const instance = await (
            await request.get("/.well-known/ai-gateway")
          ).json();
          const subtitle =
            instance.service.name && instance.service.name !== identity.name
              ? ` ${instance.service.name}`
              : "";
          const welcome = page.getByText(
            `Welcome to ${identity.name}.${subtitle}`,
            {
              exact: true,
            },
          );
          if (width === 390) {
            await expect(welcome).toBeHidden();
            const greeting = page.getByRole("heading", { name: /^Hello,/ });
            const layout = await greeting.evaluate((element) => ({
              width: element.getBoundingClientRect().width,
              available: element.parentElement!.getBoundingClientRect().width,
              lines:
                element.getBoundingClientRect().height /
                parseFloat(getComputedStyle(element).lineHeight),
            }));
            expect(
              layout.width,
              "The greeting gets the full text column width",
            ).toBeGreaterThanOrEqual(layout.available - 1);
            expect(
              layout.lines,
              "A seeded greeting fits within two lines",
            ).toBeLessThanOrEqual(2.1);
          } else {
            await expect(welcome).toBeVisible();
          }
          const snapshot = await (await request.get("/me/api/home")).json();
          const media = snapshot.capabilities.filter(
            (cap: { capability: string }) =>
              ["image", "video", "audio"].includes(cap.capability),
          );
          expect(
            media.length,
            "The default plan can discover configured media targets",
          ).toBeGreaterThan(0);
          expect(
            media.every((cap: { available: boolean }) => !cap.available),
            "Disabled media services cannot become Ready when the shared probe endpoint answers",
          ).toBe(true);
          const seededApp = snapshot.apps.find(
            (app: { name: string }) => app.name === "Browser test app",
          );
          expect(seededApp?.credentials.length).toBeGreaterThan(0);
          const thumbnail = page.locator('a[href="/ui/library/"] img');
          await expect(thumbnail).toHaveCount(1);
          await thumbnail.scrollIntoViewIfNeeded();
          await expect
            .poll(() =>
              thumbnail.evaluate(
                (image) => (image as HTMLImageElement).naturalWidth,
              ),
            )
            .toBeGreaterThan(0);
          await openClientInstructionsOnPhone(page, width);
          await chooseRecipeKey(page, /Browser test app/);
          await expect(page.getByRole("tabpanel").locator("pre")).toContainText(
            "fast",
          );
        }
        if (state === "error") {
          await expect(
            page.getByRole("alert").filter({ hasText: /could not|try again/i }),
          ).toContainText(/could not|try again/i);
          await expect(
            page.getByRole("button", { name: "Try again", exact: true }),
          ).toBeVisible();
        } else {
          await openClientInstructionsOnPhone(page, width);
          await expect(
            page.getByRole("tab", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Gateway check", exact: true }),
          ).toBeVisible();
          await expect(page.getByLabel("Loading Home")).toHaveCount(0);
          if (state === "empty") {
            await expect(
              page.getByText(
                /No capabilities are available to your account yet/,
              ),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", {
                name: "Finish setting up",
                exact: true,
              }),
            ).toHaveCount(0);
          } else {
            await expect(
              page.getByRole("heading", {
                name: "What you can use",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", { name: "Recent", exact: true }),
            ).toBeVisible();
          }
          await expect(
            page.getByRole("heading", {
              name: "Recent creations",
              exact: true,
            }),
          ).toBeVisible();
          if (state === "seeded") {
            await expect(
              page.getByText("A recorded image from your gateway", {
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.locator('a[href*="/try/?capability=chat&model=fast"]'),
            ).toHaveCount(1);
          }
        }
        await inspectHome(page, `${state}-${theme}-${width}`);

        if (state === "error") {
          await page.unroute(homeApi);
          await page
            .getByRole("button", { name: "Try again", exact: true })
            .click();
          await openClientInstructionsOnPhone(page, width);
          await expect(
            page.getByRole("tab", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("alert").filter({ hasText: /could not|try again/i }),
          ).toHaveCount(0);
        }
      });
    }
  }
}

test("all ten client recipes fit a phone and remain accessible", async ({
  page,
  context,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route(setupApi, async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: { ...(await response.json()), complete: true },
    });
  });
  await page.goto(homePath);
  const snapshot = await (await request.get("/me/api/home")).json();
  const seededApp = snapshot.apps.find(
    (app: { name: string }) => app.name === "Browser test app",
  );
  expect(seededApp?.credentials.length).toBeGreaterThan(0);
  await openClientInstructionsOnPhone(page, 390);
  await chooseRecipeKey(page, /Browser test app/);
  await expect(page.getByRole("tabpanel").locator("pre")).toContainText("fast");
  for (const name of [
    "Open WebUI",
    "LibreChat",
    "SillyTavern",
    "Cursor",
    "Codex",
    "Claude Code",
    "Python",
    "JavaScript",
    "curl",
    "Phone",
  ]) {
    await page.getByRole("tab", { name, exact: true }).click();
    await expect(
      page
        .getByRole("tabpanel")
        .getByText("Client setup: not verified", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: `Connect ${name}`, exact: true }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("tabpanel")
        .getByText("Client setup: not verified", { exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const serious = (
      await new AxeBuilder({ page }).analyze()
    ).violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    );
    expect(serious, `${name} accessibility`).toEqual([]);
    const code = page.locator("pre code:visible");
    if (await code.count()) {
      const expected = await code.textContent();
      await page.getByRole("button", { name: /^Copy .* code$/ }).click();
      await expect
        .poll(() => page.evaluate(() => navigator.clipboard.readText()))
        .toBe(expected);
    }
  }
});

test("Home shows a loading skeleton until the real snapshot arrives", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(homeApi, async (route) => {
    await gate;
    await route.continue();
  });
  await page.goto(homePath);
  await expect(page.getByLabel("Loading Home")).toBeVisible();
  const serious = (await new AxeBuilder({ page }).analyze()).violations.filter(
    (v) => ["serious", "critical"].includes(v.impact ?? ""),
  );
  expect(
    serious,
    "Loading Home must be accessible while its API is pending",
  ).toEqual([]);
  release();
  await expect(
    page.getByRole("tab", { name: "Open WebUI", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Loading Home")).toHaveCount(0);
});

test("signed-in landing redirects to Home", async ({ page }) => {
  await page.goto("/ui/");
  await expect(page).toHaveURL(/\/ui\/home\/$/);
  await expect(
    page.getByRole("tab", { name: "Open WebUI", exact: true }),
  ).toBeVisible();
});

test("member Home never requests owner APIs", async ({ page, request }) => {
  const actual = await (await request.get("/me/api/home")).json();
  const member = {
    id: "member-home-fixture",
    name: "Test member",
    email: "member@example.test",
    role: "member",
  };
  const ownerCalls: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/admin/api/")) ownerCalls.push(pathname);
  });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "member-fixture",
        user: member,
        apps: [],
      },
    }),
  );
  await page.route(homeApi, (route) =>
    route.fulfill({
      json: { ...actual, user: member, apps: [], receipts: [] },
    }),
  );
  await page.goto(homePath);
  await expect(
    page.getByRole("tab", { name: "Open WebUI", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Finish setting up", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "People", exact: true }),
  ).toHaveCount(0);
  expect(ownerCalls).toEqual([]);
});

test("superadmin Home does not request the owner-only setup API", async ({
  page,
}) => {
  const setupCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/admin/api/setup-progress")
      setupCalls.push(request.url());
  });
  for (const path of ["**/me/api/session", homeApi]) {
    await page.route(path, async (route) => {
      const response = await route.fetch();
      const actual = await response.json();
      await route.fulfill({
        response,
        json: { ...actual, user: { ...actual.user, role: "superadmin" } },
      });
    });
  }
  // A superadmin is allowed to use the gateway, but the first-run checklist
  // belongs to its owner. Any accidental request gets the actual contract's 403.
  await page.route(setupApi, (route) =>
    route.fulfill({
      status: 403,
      json: {
        error: { code: "owner_required", message: "Owner access required." },
      },
    }),
  );
  await page.goto(homePath);
  await expect(
    page.getByRole("heading", { name: "What you can use", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recent", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Finish setting up", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("alert").filter({ hasText: /could not|try again/i }),
  ).toHaveCount(0);
  expect(setupCalls).toEqual([]);
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`owner first-run checklist: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.route(setupApi, async (route) => {
        const response = await route.fetch();
        const actual = await response.json();
        await route.fulfill({
          response,
          json: {
            ...actual,
            complete: false,
            steps: actual.steps.map((step: { id: string }) => ({
              ...step,
              done: step.id === "claim_owner",
              reason:
                (
                  {
                    connect_service: "Connect a service and check its health.",
                    test_request: "Run a test request with your key.",
                    connect_app: "Create an app key and use it in your client.",
                  } as Record<string, string>
                )[step.id] ?? (step as { reason?: string }).reason,
            })),
          },
        });
      });
      await page.goto(homePath);
      await expect(
        page.getByRole("heading", { name: "Finish setting up", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "What you can use", exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("heading", { name: "Recent", exact: true }),
      ).toHaveCount(0);
      await page.getByText("Setup details", { exact: true }).click();
      await expect(page.getByText("Optional", { exact: true })).toBeVisible();
      await expect(
        page.getByRole("link", { name: /Invite someone/ }),
      ).toHaveAttribute("href", /people/);
      await inspectHome(page, `first-run-${theme}-${width}`);
    });
  }
}

test("inline reveal-once key tests a real chat request and produces one owned receipt", async ({
  page,
  request,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-ai-gateway-admin-action": "1",
    "x-ai-gateway-csrf": session.actor.csrf_token,
  };
  // Set the person's inherited plan before they create a key, through the
  // same admin mapping interface an operator uses to grant access.
  const profileResponse = await request.post("/admin/api/profiles", {
    headers,
    data: {
      name: "Home connection acceptance",
      profile: {
        id: "home-connection-acceptance",
        protocols: ["openai"],
        backends: ["capture-engine"],
        models: ["demo-chat"],
      },
    },
  });
  expect(profileResponse.status()).toBe(201);
  const { profile } = await profileResponse.json();
  const mappingResponse = await request.post(
    "/admin/api/runtime-profile-mappings",
    {
      headers,
      data: {
        source_kind: "email",
        source_value: session.actor.email,
        profile_id: profile.id,
      },
    },
  );
  expect(mappingResponse.status()).toBe(201);
  const { mapping } = await mappingResponse.json();
  try {
    const registryResponse = await request.get(
      "/admin/api/operator/service-registry",
    );
    expect(
      (
        await request.patch("/admin/api/operator/services/capture-engine", {
          headers: { ...headers, "if-match": registryResponse.headers().etag },
          data: { gateway_lifecycle: true },
        })
      ).ok(),
    ).toBe(true);
    await page.goto(homePath);
    const mintResponse = page.waitForResponse(
      (response) =>
        /\/me\/api\/apps\/[^/]+\/keys$/.test(
          new URL(response.url()).pathname,
        ) && response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Create key for Open WebUI", exact: true })
      .click();
    const mint = await mintResponse;
    expect(mint.status()).toBe(201);
    const { credential } = await mint.json();
    const secret = credential.api_key as string;
    expect(
      typeof secret === "string" && secret.length > 20,
      "A real key was minted",
    ).toBe(true);
    expect(
      (await page.getByLabel("Your key", { exact: true }).inputValue()) ===
        secret,
      "Minted key is shown once",
    ).toBe(true);
    await expect(page.getByRole("combobox", { name: "App key" })).toContainText(
      "New key",
    );
    await expect(page.getByRole("tabpanel").locator("pre")).toContainText(
      "fast",
    );
    await page
      .getByRole("button", { name: "I’ve saved it", exact: true })
      .click();
    expect(
      await page.evaluate(
        (key) => document.body.textContent?.includes(key) ?? false,
        secret,
      ),
      "Secret must disappear after acknowledgement",
    ).toBe(false);
    await page.reload();
    await expect(page.getByLabel("Your key", { exact: true })).toHaveCount(0);
    expect(
      await page.evaluate((key) => {
        const values = [
          ...Object.values(localStorage),
          ...Object.values(sessionStorage),
        ];
        return (
          document.body.textContent?.includes(key) ||
          values.some((value) => String(value).includes(key))
        );
      }, secret),
      "Home must not persist or redisplay the key",
    ).toBeFalsy();
    await chooseRecipeKey(page, /Open WebUI/);

    const before = await (
      await request.get("/me/api/activity?limit=100")
    ).json();
    const ownedBefore = before.receipts.filter(
      (receipt: { request_id: string }) => receipt.request_id,
    ).length;
    await page.getByLabel("Paste your key", { exact: true }).fill(secret);
    const testedResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/me/api/connection-test",
    );
    await page
      .getByRole("button", { name: "Gateway check", exact: true })
      .click();
    const tested = await testedResponse;
    expect(tested.ok()).toBe(true);
    const connection = await tested.json();
    expect(connection.ok).toBe(true);
    expect(connection.path).toBe("/v1/chat/completions");
    expect(connection.model).toBe("fast");
    expect(connection.latency_ms).toBeGreaterThanOrEqual(0);
    expect(
      typeof connection.request_id === "string" &&
        connection.request_id.length > 0,
    ).toBe(true);
    await expect(
      page.getByText("A one-token local chat request succeeded.", {
        exact: true,
      }),
    ).toBeVisible();
    const after = await (
      await request.get("/me/api/activity?limit=100")
    ).json();
    expect(
      after.receipts.length - ownedBefore,
      "One test creates exactly one request receipt",
    ).toBe(1);
    const receiptResponse = await request.get(
      `/me/api/receipts/${connection.request_id}`,
    );
    expect(receiptResponse.ok()).toBe(true);
    const detail = await receiptResponse.json();
    const receipt = detail.receipt ?? detail;
    expect(receipt.request_id).toBe(connection.request_id);
    expect(receipt.status).toBe("succeeded");
    expect(receipt.source_kind).toBe("gateway-ui");
    // Stable alias resolution precedes the existing durable receipt boundary.
    expect(receipt.requested_model).toBe("demo-chat");
    expect(
      JSON.stringify(connection).includes(secret),
      "Connection response must not echo the key",
    ).toBe(false);
    const progress = await (
      await request.get("/admin/api/setup-progress")
    ).json();
    const steps = progress.steps as Array<{
      id: string;
      done: boolean;
      optional: boolean;
    }>;
    for (const id of ["claim_owner", "connect_service", "test_request"]) {
      expect(
        steps.find((step) => step.id === id)?.done,
        `${id} reflects an observed gateway action, not client setup`,
      ).toBe(true);
    }
    // Global setup already made a successful external app request. A UI test
    // remains UI-attributed and does not fabricate a manual confirmation.
    expect(steps.find((step) => step.id === "connect_app")?.done).toBe(true);
    expect(progress.evidence.observed_external_client.recorded).toBe(true);
    expect(progress.evidence.external_client_confirmed.recorded).toBe(false);
    expect(steps.find((step) => step.id === "invite_someone")?.optional).toBe(
      true,
    );
    expect(progress.complete).toBe(true);
    await expect(
      page.getByText("Client setup: not verified", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "What you can use", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Finish setting up", exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("link", { name: "View request receipt", exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`/r/${connection.request_id}$`));
    await expect(
      page.getByRole("dialog", { name: "Request detail", exact: true }),
    ).toBeVisible();
    await expect(page.locator("#receipt-body")).toContainText(
      connection.request_id,
    );
  } finally {
    expect(
      (
        await request.delete(
          `/admin/api/runtime-profile-mappings/${mapping.id}`,
          { headers },
        )
      ).ok(),
    ).toBe(true);
  }
});

test("a new inherited key cannot borrow a broader existing app's recipe", async ({
  page,
  request,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-ai-gateway-admin-action": "1",
    "x-ai-gateway-csrf": session.actor.csrf_token,
  };
  const profileResponse = await request.post("/admin/api/profiles", {
    headers,
    data: {
      name: "Home inherited no chat",
      profile: {
        id: "home-inherited-no-chat",
        protocols: ["openai"],
        backends: ["capture-engine"],
        models: [],
      },
    },
  });
  expect(profileResponse.status()).toBe(201);
  const { profile } = await profileResponse.json();
  const mappingResponse = await request.post(
    "/admin/api/runtime-profile-mappings",
    {
      headers,
      data: {
        source_kind: "email",
        source_value: session.actor.email,
        profile_id: profile.id,
      },
    },
  );
  expect(mappingResponse.status()).toBe(201);
  const { mapping } = await mappingResponse.json();
  try {
    const snapshot = await (await request.get("/me/api/home")).json();
    const broader = snapshot.apps.find(
      (app: { name: string }) => app.name === "Browser test app",
    ).credentials[0];
    expect(
      snapshot.credential_aliases[broader.id].some(
        (alias: { id: string }) => alias.id === "fast",
      ),
    ).toBe(true);
    expect(snapshot.new_app_aliases).toEqual([]);
    await page.goto(homePath);
    const selector = page.getByRole("combobox", {
      name: "App key",
    });
    await expect(selector).toContainText("New app");
    await expect(
      page.getByText("No chat alias is advertised for this key yet", {
        exact: true,
      }),
    ).toBeVisible();
    await chooseRecipeKey(page, /Browser test app/);
    await expect(page.getByRole("tabpanel").locator("pre")).toContainText(
      "fast",
    );

    const mintedResponse = page.waitForResponse(
      (response) =>
        /\/me\/api\/apps\/[^/]+\/keys$/.test(
          new URL(response.url()).pathname,
        ) && response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Create key for Open WebUI", exact: true })
      .click();
    const minted = await mintedResponse;
    expect(minted.status()).toBe(201);
    const { credential } = await minted.json();
    await expect(selector).toContainText("New key");
    await expect(
      page.getByText("No chat alias is advertised for this key yet", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByRole("tabpanel").locator("pre")).toHaveCount(0);
    const catalog = await request.get("/v1/models", {
      headers: { authorization: `Bearer ${credential.api_key}` },
    });
    expect(catalog.ok()).toBe(true);
    expect((await catalog.json()).data).toEqual([]);

    const before = await (
      await request.get("/me/api/activity?limit=100")
    ).json();
    const testedResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/me/api/connection-test",
    );
    await page
      .getByRole("button", { name: "Gateway check", exact: true })
      .click();
    const tested = await testedResponse;
    expect(tested.ok()).toBe(true);
    const result = await tested.json();
    expect(result.ok).toBe(true);
    expect(result.path).toBe("/v1/models");
    expect(result.model).toBeNull();
    expect(result.reason).toContain("Chat was not tested");
    const after = await (
      await request.get("/me/api/activity?limit=100")
    ).json();
    expect(after.receipts.length).toBe(before.receipts.length);
    await page
      .getByRole("button", { name: "I’ve saved it", exact: true })
      .click();
    await expect(
      page.getByText("No chat alias is advertised for this key yet", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByRole("tabpanel").locator("pre")).toHaveCount(0);
  } finally {
    expect(
      (
        await request.delete(
          `/admin/api/runtime-profile-mappings/${mapping.id}`,
          { headers },
        )
      ).ok(),
    ).toBe(true);
  }
});
