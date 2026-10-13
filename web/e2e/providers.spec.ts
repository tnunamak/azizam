import { type APIRequestContext, type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

const catalog = {
  providers: [
    {
      id: "openrouter",
      display_name: "OpenRouter",
      logo_key: "or",
      protocol: "openai-chat",
      base_url: "https://openrouter.ai/api/v1",
      key_url: "https://openrouter.ai/keys",
      recommendations: [
        {
          message:
            "Set an OpenRouter credit limit as a second backstop before enabling paid plans.",
          url: "https://openrouter.ai/settings/credits",
        },
      ],
      billing_dimensions: ["input tokens", "output tokens"],
    },
    {
      id: "generic-openai",
      display_name: "Generic OpenAI-compatible",
      logo_key: "go",
      protocol: "openai-chat",
      base_url: "https://provider.example/v1",
      billing_dimensions: ["input tokens", "output tokens"],
    },
  ],
};

// GET /admin/api/access-plans: a plan's Paid providers are in its document.
const planDocument = (name: string, paid: string[]) => ({
  name,
  can_use: { chat: { items: "all" } },
  direct_engine_access: { engines: [], operations: [] },
  paid_providers: paid,
  provider_fallback: false,
  caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
  can_load_models: true,
  receipt_detail: "basic",
  media_metadata: "none",
});
const plans = {
  access_plans: [
    {
      id: "local",
      name: "Local only",
      created_at: 1710000000,
      updated_at: 1710000000,
      plan: { ...planDocument("Local only", []), id: "local" },
      summary: "No paid providers",
      used_by: { people: [], apps: [], invites: 0 },
    },
    {
      id: "paid",
      name: "Paid cloud",
      created_at: 1710000000,
      updated_at: 1710000000,
      plan: { ...planDocument("Paid cloud", ["openrouter"]), id: "paid" },
      summary: "Can use selected paid providers",
      used_by: { people: [], apps: [], invites: 0 },
    },
  ],
};

const seededProviders = {
  providers: [
    {
      id: "openrouter",
      name: "OpenRouter",
      enabled: true,
      reachable: true,
      checked_at: Math.floor(Date.now() / 1000) - 90,
      key_status: {
        state: "set",
        source: "secret",
        suffix: "4f2a",
        updated_at: Math.floor(Date.now() / 1000) - 172800,
      },
      spend: {
        daily_usd: 1.2,
        monthly_usd: 14.5,
        status: "needs_reconciliation",
        pending_reconciliation_count: 1,
      },
      caps: { daily_usd: 5, monthly_usd: 50 },
      access_plans: ["paid"],
      recommendations: [
        {
          message:
            "Set an OpenRouter credit limit as a second backstop before enabling paid plans.",
          url: "https://openrouter.ai/settings/credits",
        },
      ],
      catalog: {
        preset_id: "openrouter",
        protocol: "openai-chat",
        base_url: "https://openrouter.ai/api/v1",
      },
      models: [
        {
          model: "openrouter/anthropic/claude-sonnet-4.5",
          upstream_model: "anthropic/claude-sonnet-4.5",
          name: "Claude Sonnet 4.5",
          enabled: true,
          price: {
            input_usd_per_million_tokens: 3,
            output_usd_per_million_tokens: 15,
          },
        },
      ],
    },
  ],
};

async function setViewport(
  page: Page,
  theme: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
}
// Display states, request bodies and the add/credential wizard are asserted in
// vitest (src/app/providers/providers-states.spec.tsx). The browser keeps
// real layout at 390 px, axe per state and the real-gateway journey.
async function routeProviders(page: Page, state: "seeded" | "empty" | "error") {
  await page.route("**/admin/api/access-plans", (route: Route) =>
    fulfilJson(route, plans),
  );
  await page.route("**/admin/api/providers**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/admin/api/providers/-/catalog") {
      return fulfilJson(route, catalog);
    }
    if (path === "/admin/api/providers/openrouter/reconciliation") {
      return fulfilJson(route, {
          reconciliations: [
            {
              id: 42,
              provider: "openrouter",
              app_id: "app-paid",
              person_id: "person-owner",
              status: "needs_reconciliation",
              estimated_usd: "0.015",
              actual_usd: null,
              cost_source: "estimated",
              created_at: 1710000000,
              settled_at: null,
              request_id: "req_pending_42_abcdefghijklmnopqrstuvwxyz",
              generation_id: "gen_pending_42_abcdefghijklmnopqrstuvwxyz",
            },
          ],
        });
    }
    if (path !== "/admin/api/providers") return route.continue();
    if (state === "error") {
      return fulfilJson(route, { error: { message: "Provider API unavailable" } }, { status: 503 });
    }
    return fulfilJson(route, state === "seeded" ? seededProviders : { providers: [] });
  });
}

async function inspect(page: Page, name: string) {
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      { message: `${name} must not overflow horizontally` },
    )
    .toBe(true);
  const axe = await new AxeBuilder({ page }).analyze();
  expect(
    axe.violations
      .filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      )
      .map((violation) => violation.id),
  ).toEqual([]);
  await page.screenshot({
    path: `test-results/screens/${name}.png`,
    fullPage: true,
  });
}

// One axe/overflow representative per state; together they cover both themes
// and both widths.
for (const [state, theme, width] of [
  ["seeded", "light", 1440],
  ["seeded", "dark", 390],
  ["empty", "light", 390],
  ["error", "dark", 1440],
] as const) {
  test(`providers ${state}: ${theme} ${width}`, async ({ page }) => {
    await setViewport(page, theme, width);
    await routeProviders(page, state);
    await page.goto("/providers/");
    await expect(page.getByRole("heading", { name: "Providers" })).toBeVisible();
    if (state === "seeded") {
      await expect(page.getByRole("heading", { name: "OpenRouter" })).toBeVisible();
      await page.getByText("Manage", { exact: true }).click();
      await expect(
        page.getByRole("cell", { name: "openrouter/anthropic/claude-sonnet-4.5" }),
      ).toBeVisible();
    } else if (state === "empty") {
      await expect(
        page.getByText(/Add one provider, then choose its models/),
      ).toBeVisible();
    } else {
      await expect(
        page.getByRole("button", { name: "Retry providers" }),
      ).toBeVisible();
    }
    await inspect(page, `providers-${state}-${theme}-${width}`);
  });
}

test("providers reconciliation dialog: dark 390", async ({ page }) => {
  await setViewport(page, "dark", 390);
  await routeProviders(page, "seeded");
  await page.goto("/providers/");
  await page.getByText("Manage", { exact: true }).click();
  await expect(page.getByText("1 charge needs checking")).toBeVisible();
  await inspect(page, "providers-reconciliation-card-dark-390");
  await page.getByRole("button", { name: "Check charges" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "req_pending_42_abcdefghijklmnopqrstuvwxyz",
  );
  await inspect(page, "providers-reconciliation-dialog-dark-390");
});

async function clearOpenRouterProvider(
  request: APIRequestContext,
  headers: Record<string, string>,
) {
  const pending = await request
    .get("/admin/api/providers/openrouter/reconciliation")
    .catch(() => null);
  if (pending && pending.status() === 200) {
    const body = await pending.json();
    for (const row of body.reconciliations ?? []) {
      await request
        .post(`/admin/api/providers/openrouter/reconciliation/${row.id}`, {
          headers,
          data: { actual_usd: "0" },
        })
        .catch(() => undefined);
    }
  }
  await request
    .delete("/admin/api/providers/openrouter/key", {
      headers,
      data: {},
    })
    .catch(() => undefined);
}

async function removeSeededOpenRouterProvider(
  request: APIRequestContext,
  headers: Record<string, string>,
) {
  const response = await request.delete("/admin/api/providers/openrouter", {
    headers,
    data: { expected_generation: null },
  });
  expect(response.status(), await response.text()).toBe(200);
  const providers = await request.get("/admin/api/providers");
  expect(providers.status()).toBe(200);
  const body = await providers.json();
  expect(
    (body.providers ?? []).some(
      (provider: { id?: string }) => provider.id === "openrouter",
    ),
  ).toBe(false);
}

test("providers real backend OpenRouter journey keeps paid access denied until allowed", { tag: "@global-state" }, async ({
  page,
  request,
  baseURL,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-azizam-admin-action": "1",
    "x-azizam-csrf": session.actor.csrf_token,
  };
  const unique = Date.now().toString(36);
  let plan: { id: string } | undefined;
  let observerPlan: { id: string } | undefined;
  let app: { id: string } | undefined;
  let observerApp: { id: string } | undefined;
  // A paid route needs both the app's plan and its accountable person's plan
  // (design §2.6). The owner's plan is restored afterwards.
  const people = await (await request.get("/admin/api/people")).json();
  const owner = people.people.find((p: { role: string }) => p.role === "owner");
  const ownerPlan = (await (await request.get("/admin/api/access-plans")).json())
    .access_plans.find((p: { id: string }) => p.id === owner.plan_id);
  expect(ownerPlan, "the owner holds a plan").toBeTruthy();
  const document = (name: string, paid: string[]) => ({
    name,
    can_use: { chat: { items: "all" } },
    direct_engine_access: { engines: [], operations: [] },
    paid_providers: paid,
    provider_fallback: false,
    caps: { usd_per_request: null, usd_per_day: "2.00", usd_per_month: null },
    can_load_models: true,
    receipt_detail: "full",
    media_metadata: "none",
  });
  const putPlan = async (id: string, doc: Record<string, unknown>) => {
    const response = await request.put(`/admin/api/access-plans/${id}`, { headers, data: doc });
    expect(response.status(), await response.text()).toBe(200);
  };
  try {
    await clearOpenRouterProvider(request, headers);
    await removeSeededOpenRouterProvider(request, headers);
    const port = Number(new URL(baseURL!).port);

    const planResponse = await request.post("/admin/api/access-plans", {
      headers,
      data: document(`Provider denied plan ${unique}`, []),
    });
    expect(planResponse.status(), await planResponse.text()).toBe(201);
    ({ access_plan: plan } = await planResponse.json());
    const observerPlanResponse = await request.post("/admin/api/access-plans", {
      headers,
      data: document(`Provider observer plan ${unique}`, ["openrouter"]),
    });
    expect(observerPlanResponse.status(), await observerPlanResponse.text()).toBe(201);
    ({ access_plan: observerPlan } = await observerPlanResponse.json());
    const observerAppResponse = await request.post("/admin/api/apps", {
      headers,
      data: {
        name: `Provider observer app ${unique}`,
        kind: "personal",
        owner_user_id: owner.id,
        access_plan: observerPlan!.id,
      },
    });
    expect(observerAppResponse.status()).toBe(201);
    ({ app: observerApp } = await observerAppResponse.json());
    const appResponse = await request.post("/admin/api/apps", {
      headers,
      data: {
        name: `Provider test app ${unique}`,
        kind: "personal",
        owner_user_id: owner.id,
        access_plan: plan!.id,
      },
    });
    expect(appResponse.status()).toBe(201);
    ({ app } = await appResponse.json());
    const keyResponse = await request.post(`/admin/api/apps/${app!.id}/keys`, {
      headers,
      data: { label: `Provider journey key ${unique}` },
    });
    expect(keyResponse.status()).toBe(201);
    const { credential } = await keyResponse.json();

    await page.goto("/providers/");
    await page.getByRole("button", { name: "Add provider" }).first().click();
    await page.getByLabel("Search providers").fill("openrouter");
    await page.getByRole("button", { name: /OpenRouter/ }).click();
    await page
      .getByLabel("Provider base URL")
      .fill(`http://127.0.0.1:${port + 1}/provider/v1`);
    await page.getByLabel("Provider API key").fill("sk-test-openrouter");
    await page
      .getByRole("button", { name: "Check provider connection" })
      .click();
    await expect(page.getByText(/Listed 1 models?/)).toBeVisible();
    await page.getByRole("button", { name: "Next" }).click();
    await expect(
      page.getByText("openrouter/anthropic/claude-sonnet-4.5"),
    ).toBeVisible();
    await expect(
      page.getByLabel("openrouter/anthropic/claude-sonnet-4.5 input price"),
    ).toHaveValue("3.000000");
    await expect(
      page.getByLabel("openrouter/anthropic/claude-sonnet-4.5 output price"),
    ).toHaveValue("15.000000");
    await page.getByRole("button", { name: "Next" }).click();
    await expect(
      page.getByText(/No access plan includes a new provider/),
    ).toBeVisible();
    await page.getByRole("button", { name: "Save provider" }).click();
    await expect(
      page.getByRole("heading", { name: "OpenRouter" }),
    ).toBeVisible();
    await expect(page.getByText("sk-test-openrouter")).toHaveCount(0);
    await page.getByText("Manage", { exact: true }).click();
    await expect(
      page.getByRole("cell", {
        name: "openrouter/anthropic/claude-sonnet-4.5",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", {
        name: "Input: USD per 1 million tokens",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("cell", { name: /^3(?:\.000000)?$/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("cell", { name: /^15(?:\.000000)?$/ }),
    ).toBeVisible();
    const providerList = await request.get("/admin/api/providers");
    expect(providerList.status()).toBe(200);
    const providerPayload = await providerList.json();
    expect(JSON.stringify(providerPayload)).not.toContain("sk-test-openrouter");
    expect(providerPayload).toMatchObject({
      providers: [
        {
          id: "openrouter",
          key_status: { state: "set", source: "secret_store" },
          models: [
            {
              name: "Claude Sonnet 4.5",
              price: {
                input_usd_per_million_tokens: 3,
                output_usd_per_million_tokens: 15,
              },
            },
          ],
        },
      ],
    });
    const registryAfterSave = await request.get(
      "/admin/api/operator/service-registry",
    );
    expect(registryAfterSave.status()).toBe(200);
    const savedService = (await registryAfterSave.json()).service_registry
      .services.openrouter;
    expect(savedService.config).toMatchObject({
      host: "127.0.0.1",
      port: port + 1,
      base_path: "/provider/v1",
    });

    const denied = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello" }],
        max_tokens: 100,
      },
    });
    const deniedText = await denied.text();
    const deniedBody = (() => {
      try {
        return JSON.parse(deniedText);
      } catch {
        return { error: { code: "non_json_error", message: deniedText } };
      }
    })();
    expect(denied.status(), JSON.stringify(deniedBody)).toBe(403);
    expect(deniedBody).toMatchObject({
      error: { code: "remote_provider_not_allowed" },
    });

    // Allow it: add OpenRouter to the app's plan and to the owner's plan.
    await putPlan(plan!.id, document(`Provider denied plan ${unique}`, ["openrouter"]));
    await putPlan(ownerPlan.id, {
      ...ownerPlan.plan,
      id: undefined,
      paid_providers: [...ownerPlan.plan.paid_providers, "openrouter"],
    });

    await page.goto("/models/");
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    // Every model name is listed; none hides behind a control.
    const sonnet = page
      .locator("li[data-model]")
      .filter({ hasText: "Claude Sonnet 4.5" })
      .first();
    await expect(sonnet).toBeVisible();
    await expect(
      sonnet.getByTitle(/\$3 input \/ \$15 output per 1M tokens/),
    ).toBeVisible();

    const allowed = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello" }],
        max_tokens: 100,
      },
    });
    expect(allowed.status(), await allowed.text()).toBe(200);

    const unknownUsage = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "force_unknown_usage" }],
        max_tokens: 100,
      },
    });
    expect(unknownUsage.status(), await unknownUsage.text()).toBe(200);

    const pendingResponse = await request.get(
      "/admin/api/providers/openrouter/reconciliation",
    );
    expect(pendingResponse.status()).toBe(200);
    const pending = await pendingResponse.json();
    expect(pending.reconciliations.length).toBeGreaterThan(0);
    const pendingId = pending.reconciliations[0].id;

    const blocked = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "blocked until reconcile" }],
        max_tokens: 100,
      },
    });
    const blockedText = await blocked.text();
    expect(blocked.status(), blockedText).toBe(402);
    expect(JSON.parse(blockedText)).toMatchObject({
      error: { code: "provider_needs_reconciliation" },
    });

    await page.goto("/providers/");
    await page.getByText("Manage", { exact: true }).click();
    await expect(
      page.getByRole("button", { name: "Check charges" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Check charges" }).click();
    await expect(page.getByRole("dialog")).toContainText(String(pendingId));
    const actualCharge = page.getByLabel(
      new RegExp(`Verified actual charge for charge ${pendingId}`),
    );
    await expect(actualCharge).toHaveValue("");
    await actualCharge.fill("0.000001234");
    const reconcilePost = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes(
            `/admin/api/providers/openrouter/reconciliation/${pendingId}`,
          ) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Record actual" }).click();
    const reconcileResponse = await reconcilePost;
    expect(reconcileResponse.status()).toBe(200);
    const settled = await reconcileResponse.json();
    expect(settled).toMatchObject({
      reconciliation: { status: "settled", actual_usd: "0.000001234" },
    });
    await expect(
      page.getByText("No charges need checking."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();

    const afterReconcile = await request.get(
      "/admin/api/providers/openrouter/reconciliation",
    );
    expect(afterReconcile.status()).toBe(200);
    const remaining = await afterReconcile.json();
    expect(remaining).toMatchObject({ reconciliations: [] });

    const unblocked = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello again" }],
        max_tokens: 100,
      },
    });
    expect(unblocked.status(), await unblocked.text()).toBe(200);
  } finally {
    await clearOpenRouterProvider(request, headers);
    await removeSeededOpenRouterProvider(request, headers).catch(
      () => undefined,
    );
    // Apps are turned off, never deleted, so the two test plans stay listed.
    for (const id of [app?.id, observerApp?.id].filter(Boolean)) {
      await request
        .delete(`/admin/api/apps/${id}`, { headers })
        .catch(() => undefined);
    }
    await putPlan(ownerPlan.id, { ...ownerPlan.plan, id: undefined }).catch(() => undefined);
  }
});

test("providers add dialog fits a 390 px phone with long preset metadata", async ({
  page,
}) => {
  await setViewport(page, "light", 390);
  await routeProviders(page, "empty");
  await page.route("**/admin/api/providers/-/catalog", (route: Route) =>
    fulfilJson(route, {
        providers: [
          // The longest real preset name, URL and billing labels.
          {
            id: "long-preset",
            display_name: "Google Gemini OpenAI-compatible",
            logo_key: "go",
            protocol: "anthropic-messages",
            base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
            billing_dimensions: [
              "input_tokens",
              "output_tokens",
              "cache_read_input_tokens",
              "cache_creation_input_tokens",
            ],
          },
        ],
      }),
  );
  await page.goto("/providers/");
  await page.getByRole("button", { name: "Add provider" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Search providers")).toBeVisible();
  const overflowing = () =>
    dialog.evaluate((node) =>
      [node, ...node.querySelectorAll<HTMLElement>("*")]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
        .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 50)}`),
    );
  await expect.poll(overflowing).toEqual([]);
  await dialog.getByRole("button", { name: /Google Gemini/ }).click();
  await expect(dialog.getByLabel("Provider base URL")).toBeVisible();
  await expect.poll(overflowing).toEqual([]);
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeInViewport();
});
