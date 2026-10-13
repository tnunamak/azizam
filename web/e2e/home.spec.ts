import { type APIRequestContext, type Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { chatAlias } from "../src/app/_home/chat-alias";
import { fulfilJson } from "./fixtures/intercept";

// The key selector right after minting: the loading label, or the key's own.
const newKeyLabel = (credential: { display_name?: string | null }) =>
  new RegExp(`New key · loading access|· ${(credential.display_name || "Key").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

const homePath = "/";
const homeApi = "**/me/api/home";
const mediaApi = "**/me/api/media?limit=12";
const setupApi = "**/admin/api/setup-progress";


type PlanDocument = Record<string, unknown>;

/**
 * Give the signed-in person a plan for one test, through the same admin API an
 * owner uses. Returns what to run afterwards to hand back the previous plan.
 */
async function holdPlan(
  request: APIRequestContext,
  headers: Record<string, string>,
  email: string,
  document: PlanDocument,
) {
  const created = await request.post("/admin/api/access-plans", {
    headers,
    data: {
      direct_engine_access: { engines: [], operations: [] },
      paid_providers: [],
      provider_fallback: false,
      caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
      can_load_models: true,
      receipt_detail: "basic",
      media_metadata: "none",
      ...document,
    },
  });
  expect(created.status()).toBe(201);
  const { access_plan: plan } = await created.json();
  const { people } = await (await request.get("/admin/api/people")).json();
  const me = people.find((person: { email: string }) => person.email === email);
  const original: string | null = me.plan_id;
  const assign = (planId: string | null) =>
    request.patch(`/admin/api/people/${me.id}`, {
      headers,
      data: { access_plan: planId },
    });
  expect((await assign(plan.id)).ok()).toBe(true);
  return async () => {
    expect((await assign(original)).ok()).toBe(true);
    expect(
      (await request.delete(`/admin/api/access-plans/${plan.id}`, { headers })).ok(),
    ).toBe(true);
  };
}

test("Home names a freshly checked on-demand task as ready to start", async ({
  page,
}) => {
  await page.route(homeApi, async (route) => {
    const response = await route.fetch();
    const home = await response.json();
    await fulfilJson(route, {
        ...home,
        capabilities: home.capabilities.map(
          (capability: { capability: string; availability?: object }) =>
            capability.capability === "chat"
              ? {
                  ...capability,
                  status: "idle",
                  available: true,
                  reason_code: "idle",
                  availability: {
                    ...capability.availability,
                    status: "idle",
                    available: true,
                    evidence: {
                      source: "lifecycle",
                      observed_at_s: Math.floor(Date.now() / 1000),
                    },
                  },
                }
              : capability,
        ),
      }, { response });
  });
  await page.goto(homePath);
  await expect(page.locator("main")).toContainText(
    "Ready to start when you make a request.",
  );
});

test("Home refreshes capability evidence while it stays open", async ({
  page,
}) => {
  await page.clock.install({ time: new Date() });
  let ready = false;
  await page.route(homeApi, async (route) => {
    const response = await route.fetch();
    const home = await response.json();
    await fulfilJson(route, {
        ...home,
        capabilities: home.capabilities.map(
          (capability: { capability: string; availability?: object }) =>
            capability.capability === "chat"
              ? {
                  ...capability,
                  status: ready ? "ready" : "unknown",
                  available: ready,
                  reason_code: ready ? "ready" : "unknown",
                  availability: {
                    ...capability.availability,
                    status: ready ? "ready" : "unknown",
                    evidence: ready
                      ? {
                          source: "status",
                          observed_at_s: Math.floor(Date.now() / 1000),
                        }
                      : {},
                  },
                }
              : capability,
        ),
      }, { response });
  });
  await page.goto(homePath);
  // The state badge sits in the row beside its link, so read the whole row.
  const chat = page.locator("li", {
    has: page.locator('a[href*="capability=chat"]'),
  });
  await expect(chat).toContainText("Unknown");
  ready = true;
  await page.clock.fastForward(61_000);
  await expect(chat).not.toContainText("Unknown");
});

async function chooseKey(page: Page, optionName: string | RegExp) {
  await page.getByRole("combobox", { name: "API key" }).click();
  await page.getByRole("option", { name: optionName }).click();
}

async function openClientInstructionsOnPhone(page: Page, _width: number) {
  const picker = page.getByRole("button", {
    name: "Open WebUI",
    exact: true,
  });
  // D1.1: once an app has made a request, the app chooser sits behind
  // "Add an app"; a newcomer sees it open.
  const addApp = page.getByRole("button", { name: "Add an app", exact: true });
  await expect(picker.or(addApp)).toBeVisible();
  if (await addApp.count()) await addApp.click();
  await expect(picker).toBeVisible();
  if ((await picker.getAttribute("aria-pressed")) !== "true") {
    await picker.click();
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
    fullPage: page.viewportSize()?.width !== 390,
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
            await fulfilJson(route, {
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
                  reason_code: "no_capabilities",
                  affected_capability_ids: [],
                  counts: { total: 0, available: 0, affected: 0 },
                },
              }, { response });
          });
          await page.route(setupApi, (route) =>
            fulfilJson(route, { complete: true, steps: [] }),
          );
          await page.route(mediaApi, (route) =>
            fulfilJson(route, { assets: [], next_cursor: null }),
          );
        }
        if (state === "seeded") {
          await page.route(mediaApi, (route) =>
            fulfilJson(route, {
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
              }),
          );
          await page.route(setupApi, (route) =>
            fulfilJson(route, { complete: true, steps: [] }),
          );
        }
        if (state === "error") {
          await page.route(homeApi, (route) =>
            fulfilJson(route, { error: { message: "Unavailable for this test" } }, { status: 503 }),
          );
        }

        const response = await page.goto(homePath);
        expect(response?.status()).toBe(200);
        if (state !== "error") {
          await expect(
            page.getByRole("heading", { name: "Connect an app", exact: true }),
          ).toBeVisible();
          await expect(page.locator("main")).not.toContainText("Hello,");
          await expect(page.locator("main")).not.toContainText("Welcome to");
        }
        if (state === "seeded") {
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
          const thumbnail = page.locator('a[href^="/library/?asset="] img');
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
          await chooseKey(page, /Browser test app/);
          const selected = seededApp.credentials[0];
          const aliases = snapshot.credential_aliases?.[selected.id] ?? [];
          const model = page.getByRole("textbox", {
            name: "Model",
            exact: true,
          });
          const recommended = snapshot.capabilities.find(
            (row: { icon_key?: string; capability: string }) =>
              (row.icon_key ?? row.capability) === "chat",
          );
          const expectedModel = chatAlias(aliases, recommended?.use_name ?? recommended?.model);
          if (expectedModel) {
            await expect(model).toHaveValue(expectedModel);
          } else {
            await expect(model).toHaveCount(0);
          }
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
            page.getByRole("button", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Check connection", exact: true }),
          ).toBeVisible();
          await expect(page.getByLabel("Loading Home")).toHaveCount(0);
          if (state === "empty") {
            await expect(page.getByText("Nothing is available yet.", { exact: true })).toBeVisible();
            await expect(page.getByText("Your creations will appear here.")).toBeVisible();
            await expect(
              page
                .locator("main")
                .getByRole("status")
                .filter({ hasText: "Nothing is turned on yet" }),
            ).toHaveCount(1);
            await expect(
              page.getByRole("heading", {
                name: "Make something",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", {
                name: "Finish setting up",
                exact: true,
              }),
            ).toHaveCount(0);
          } else if (state === "seeded") {
            await expect(
              page.getByRole("heading", {
                name: "Make something",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", { name: "Recent", exact: true }),
            ).toBeVisible();
          } else {
            await expect(
              page.getByRole("heading", { name: "Recent", exact: true }),
            ).toBeVisible();
          }
          if (state === "seeded") {
            // D1.1: Recent tiles carry the prompt in the tile's name and
            // the image's alt text instead of a caption.
            await expect(
              page.getByRole("link", {
                name: "Open Image in Library: A recorded image from your gateway",
                exact: true,
              }),
            ).toBeVisible();
            await expect(
              page.getByRole("img", {
                name: "A recorded image from your gateway",
                exact: true,
              }),
            ).toBeVisible();
            // D1.1: tasks open Try in the right mode, not Models.
            await expect(
              page.locator('a[href*="/try/?capability=chat"]'),
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
            page.getByRole("button", { name: "Open WebUI", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("alert").filter({ hasText: /could not|try again/i }),
          ).toHaveCount(0);
        }
      });
    }
  }
}

test("Home setup notes fit a phone and remain accessible", async ({
  page,
  context,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route(setupApi, async (route) => {
    const response = await route.fetch();
    await fulfilJson(route, { ...(await response.json()), complete: true }, { response });
  });
  await page.goto(homePath);
  const snapshot = await (await request.get("/me/api/home")).json();
  const seededApp = snapshot.apps.find(
    (app: { name: string }) => app.name === "Browser test app",
  );
  expect(seededApp?.credentials.length).toBeGreaterThan(0);
  await openClientInstructionsOnPhone(page, 390);
  await chooseKey(page, /Browser test app/);
  await page.getByRole("button", { name: "Setup notes", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("Open WebUI");
  await expect(page.getByRole("dialog")).toContainText(
    "gateway check verifies the route and key",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const serious = (await new AxeBuilder({ page }).analyze()).violations.filter(
    (v) => ["serious", "critical"].includes(v.impact ?? ""),
  );
  expect(serious, "Setup notes accessibility").toEqual([]);
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
  await openClientInstructionsOnPhone(page, 1280);
  await expect(
    page.getByRole("button", { name: "Open WebUI", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Loading Home")).toHaveCount(0);
});

test("signed-in root shows Home", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
  await openClientInstructionsOnPhone(page, 1280);
  await expect(
    page.getByRole("button", { name: "Open WebUI", exact: true }),
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
    fulfilJson(route, {
        authenticated: true,
        csrf_token: "member-fixture",
        user: member,
        apps: [],
      }),
  );
  await page.route(homeApi, (route) =>
    fulfilJson(route, { ...actual, user: member, apps: [], receipts: [] }),
  );
  await page.goto(homePath);
  await expect(
    page.getByRole("button", { name: "Open WebUI", exact: true }),
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
      await fulfilJson(route, { ...actual, user: { ...actual.user, role: "superadmin" } }, { response });
    });
  }
  // A superadmin is allowed to use the gateway, but the first-run checklist
  // belongs to its owner. Any accidental request gets the actual contract's 403.
  await page.route(setupApi, (route) =>
    fulfilJson(route, {
        error: { code: "owner_required", message: "Owner access required." },
      }, { status: 403 }),
  );
  await page.goto(homePath);
  await expect(
    page.getByRole("heading", { name: "Make something", exact: true }),
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
    test(`Home keeps setup progress out of the arrival screen: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      const setupCalls: string[] = [];
      page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/admin/api/setup-progress") {
          setupCalls.push(request.url());
        }
      });
      await page.goto(homePath);
      await expect(
        page.getByRole("heading", { name: "Connect an app", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Make something", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Recent", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Finish setting up", exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByText("Recent requests", { exact: true }),
      ).toHaveCount(0);
      expect(setupCalls).toEqual([]);
      await inspectHome(page, `arrival-${theme}-${width}`);
    });
  }
}

test("inline reveal-once key tests a real chat request and produces one owned receipt", { tag: GLOBAL_STATE_TAG }, async ({
  page,
  request,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-azizam-admin-action": "1",
    "x-azizam-csrf": session.actor.csrf_token,
  };
  // Set the person's plan before they create a key, through the same admin
  // interface an operator uses to grant access.
  const release = await holdPlan(request, headers, session.actor.email, {
    name: "Home connection acceptance",
    can_use: { chat: { items: [{ model: "demo-chat" }] } },
  });
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
    await openClientInstructionsOnPhone(page, 1280);
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
    // The new key stays selected: "New key · loading access" until Home's
    // refetch lists it, then its own name. Either order is correct.
    await expect(page.getByRole("combobox", { name: "API key" })).toContainText(
      newKeyLabel(credential),
    );
    // The Model field follows the key's access, which arrives with Home's
    // refetch; the chat check after the reload exercises that access.
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
    await openClientInstructionsOnPhone(page, 1280);
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
    // The new-key option reads "New key for Open WebUI" since D1.1; pick
    // the key just minted for the Open WebUI app.
    await chooseKey(page, /^Open WebUI · /);

    const before = await (
      await request.get("/me/api/activity?limit=100")
    ).json();
    const ownedBefore = before.receipts.filter(
      (receipt: { request_id: string }) => receipt.request_id,
    ).length;
    await page.getByLabel("Paste your API Key", { exact: true }).fill(secret);
    const testedResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/me/api/connection-test",
    );
    await page
      .getByRole("button", { name: "Check connection", exact: true })
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
      page.getByRole("button", { name: "Setup notes", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Make something", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Finish setting up", exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("link", { name: "View request receipt", exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`/usage/\\?receipt=${connection.request_id}$`));
    // /usage/?receipt= opens the Usage receipt sheet (the /r/<id> page is gone).
    const sheet = page.getByRole("dialog", {
      name: "Request receipt",
      exact: true,
    });
    await expect(sheet).toBeVisible();
    await sheet.getByText("Technical details", { exact: true }).click();
    await expect(sheet).toContainText(`Request ID: ${connection.request_id}`);
  } finally {
    await release();
  }
});

test("a new inherited key cannot borrow an existing app's access beyond the person's plan", { tag: GLOBAL_STATE_TAG }, async ({
  page,
  request,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-azizam-admin-action": "1",
    "x-azizam-csrf": session.actor.csrf_token,
  };
  const release = await holdPlan(request, headers, session.actor.email, {
    name: "Home inherited no chat",
    // A plan lists what it grants: use an explicit non-chat model.
    can_use: { image: { items: [{ model: "demo-image" }] } },
  });
  try {
    const snapshot = await (await request.get("/me/api/home")).json();
    const broader = snapshot.apps.find(
      (app: { name: string }) => app.name === "Browser test app",
    ).credentials[0];
    // An app runs only what both its plan and its person's plan allow
    // (ACCESS_PLANS.md §2.6): the person's plan now grants no chat, so the
    // app's own chat grant no longer gives its key the chat alias either.
    expect(
      snapshot.credential_aliases[broader.id].some(
        (alias: { id: string }) => alias.id === "fast",
      ),
    ).toBe(false);
    expect(snapshot.new_app_aliases).toEqual([]);
    await page.goto(homePath);
    await openClientInstructionsOnPhone(page, 1280);
    const selector = page.getByRole("combobox", {
      name: "API key",
    });
    // "New app · default access" reads "New key for Open WebUI" since D1.1.
    await expect(selector).toContainText("New key for Open WebUI");
    await expect(page.getByRole("textbox", { name: "Model" })).toHaveCount(0);
    await chooseKey(page, /Browser test app/);
    const allowedChat = chatAlias(snapshot.credential_aliases[broader.id]);
    if (allowedChat) {
      await expect(page.getByRole("textbox", { name: "Model" })).toHaveValue(allowedChat);
    } else {
      await expect(page.getByRole("textbox", { name: "Model" })).toHaveCount(0);
    }

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
    await expect(selector).toContainText(newKeyLabel(credential));
    await expect(page.getByRole("textbox", { name: "Model" })).toHaveCount(0);
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
      .getByRole("button", { name: "Check connection", exact: true })
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
    await expect(page.getByRole("textbox", { name: "Model" })).toHaveCount(0);
  } finally {
    await release();
  }
});
