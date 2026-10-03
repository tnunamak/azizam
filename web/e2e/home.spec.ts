import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const homePath = "/ui/home/";
const homeApi = "**/me/api/home";
const setupApi = "**/admin/api/setup-progress";

async function inspectHome(page: Page, name: string) {
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
      test(`Home ${state}: ${theme} at ${width}px`, async ({ page }) => {
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
        }
        if (state === "seeded") {
          await page.route(setupApi, async (route) => {
            const response = await route.fetch();
            const actual = await response.json();
            await route.fulfill({
              response,
              json: { ...actual, complete: true },
            });
          });
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
        await expect(
          page.getByRole("heading", { name: "Home", exact: true }),
        ).toBeVisible();
        if (state === "error") {
          await expect(page.getByRole("alert")).toContainText(
            /could not|try again/i,
          );
          await expect(
            page.getByRole("button", { name: "Try again", exact: true }),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("tab", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Test connection", exact: true }),
          ).toBeVisible();
          await expect(page.getByLabel("Loading Home")).toHaveCount(0);
          if (state === "empty") {
            await expect(
              page.getByText(/No models are available to your account yet/),
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
          // There is no media-list API in this lane: Home must not invent a gallery.
          await expect(
            page.getByRole("heading", {
              name: "Recent creations",
              exact: true,
            }),
          ).toHaveCount(0);
        }
        await inspectHome(page, `${state}-${theme}-${width}`);

        if (state === "error") {
          await page.unroute(homeApi);
          await page
            .getByRole("button", { name: "Try again", exact: true })
            .click();
          await expect(
            page.getByRole("tab", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(page.getByRole("alert")).toHaveCount(0);
        }
      });
    }
  }
}

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
  // Grant the newly created app an explicit, isolated chat plan. The browser
  // then tests its own minted key through the real public dispatch boundary.
  const profileResponse = await request.post("/admin/api/profiles", {
    headers,
    data: {
      name: "Home connection acceptance",
      profile: {
        id: "home-connection-acceptance",
        protocols: ["openai"],
        backends: ["capture-engine"],
        models: ["demo-chat", "fast"],
      },
    },
  });
  expect(profileResponse.status()).toBe(201);
  const { profile } = await profileResponse.json();
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
      /\/me\/api\/apps\/[^/]+\/keys$/.test(new URL(response.url()).pathname) &&
      response.request().method() === "POST",
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
  expect(
    (
      await request.patch(`/admin/api/apps/${credential.app_id}`, {
        headers,
        data: { default_profile_id: profile.id },
      })
    ).ok(),
  ).toBe(true);
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

  const before = await (await request.get("/me/api/activity?limit=100")).json();
  const ownedBefore = before.receipts.filter(
    (receipt: { request_id: string }) => receipt.request_id,
  ).length;
  await page.getByLabel("Key to test", { exact: true }).fill(secret);
  const testedResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/me/api/connection-test",
  );
  await page
    .getByRole("button", { name: "Test connection", exact: true })
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
  const after = await (await request.get("/me/api/activity?limit=100")).json();
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
  expect(receipt.requested_model).toBe("fast");
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
  for (const id of ["claim_owner", "test_request", "connect_app"]) {
    expect(
      steps.find((step) => step.id === id)?.done,
      `${id} reflects the real completed action`,
    ).toBe(true);
  }
  expect(steps.find((step) => step.id === "invite_someone")?.optional).toBe(
    true,
  );
  expect(progress.complete).toBe(
    steps.filter((step) => !step.optional).every((step) => step.done),
  );
});
