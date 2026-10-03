import {
  test,
  expect,
  type APIRequestContext,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

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

const plans = {
  plans: [
    {
      id: "local",
      name: "Local only",
      description: "No paid providers",
      paid_allowed: false,
    },
    {
      id: "paid",
      name: "Paid cloud",
      description: "Can use selected paid providers",
      paid_allowed: true,
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
      allowed_plans: [],
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

async function routeProviders(page: Page, state: "seeded" | "empty" | "error") {
  let saved = false;
  let savedModels = false;
  let savedCaps = false;
  let savedPlans = false;
  let reconciled = false;
  let reconciled42 = false;
  await page.route("**/__provider-test-flags", (route: Route) =>
    route.fulfill({ json: { savedModels, savedCaps, savedPlans, reconciled } }),
  );
  await page.route("**/admin/api/profiles", (route: Route) =>
    route.fulfill({ json: plans }),
  );
  await page.route("**/admin/api/providers**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/admin/api/providers/-/catalog") {
      return route.fulfill({ json: catalog });
    }
    if (path === "/admin/api/providers/-/test") {
      expect(route.request().postDataJSON()).toMatchObject({
        preset_id: "openrouter",
        provider_id: "openrouter",
        key_mode: "secret",
        api_key: "sk-or-browser-test",
        base_url: "https://openrouter.ai/api/v1",
        models: [],
      });
      return route.fulfill({
        json: {
          ok: true,
          message: "Listed 2 models from OpenRouter.",
          models: [
            {
              id: "openrouter/anthropic/claude-sonnet-4.5",
              name: "Claude Sonnet 4.5",
              upstream_model: "anthropic/claude-sonnet-4.5",
              context: "200k context",
              price: {
                input_usd_per_million_tokens: 3,
                output_usd_per_million_tokens: 15,
              },
            },
            {
              id: "openrouter/unknown-new-model",
              name: "Unknown new model",
              context: "Context not reported",
              price_input: "",
              price_output: "",
            },
          ],
        },
      });
    }
    if (path === "/admin/api/providers/openrouter/reconciliation") {
      expect(route.request().method()).toBe("GET");
      return route.fulfill({
        json: {
          reconciliations: reconciled
            ? []
            : [
                ...(reconciled42
                  ? []
                  : [
                      {
                        id: 42,
                        provider: "openrouter",
                        profile_id: "paid",
                        status: "needs_reconciliation",
                        estimated_usd: "0.015",
                        actual_usd: null,
                        cost_source: "estimated",
                        created_at: 1710000000,
                        settled_at: null,
                        request_id: "req_pending_42_abcdefghijklmnopqrstuvwxyz",
                        generation_id:
                          "gen_pending_42_abcdefghijklmnopqrstuvwxyz",
                      },
                    ]),
                {
                  id: 43,
                  provider: "openrouter",
                  profile_id: "paid",
                  status: "needs_reconciliation",
                  estimated_usd: "0.000001234",
                  actual_usd: null,
                  cost_source: "estimated",
                  created_at: 1710000001,
                  settled_at: null,
                  request_id: "req_pending_43",
                  generation_id: "gen_pending_43",
                },
              ],
        },
      });
    }
    if (path === "/admin/api/providers/openrouter/reconciliation/42") {
      reconciled42 = true;
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toEqual({
        actual_usd: "0.000001234",
      });
      return route.fulfill({
        json: {
          reconciliation: {
            id: 42,
            status: "settled",
            actual_usd: "0.000001234",
          },
        },
      });
    }
    if (path === "/admin/api/providers/openrouter/reconciliation/43") {
      reconciled = true;
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toEqual({ actual_usd: "0" });
      return route.fulfill({
        json: {
          reconciliation: { id: 43, status: "settled", actual_usd: "0.00" },
        },
      });
    }
    if (path === "/admin/api/providers/openrouter/models") {
      savedModels = true;
      const body = route.request().postDataJSON();
      expect(route.request().method()).toBe("PUT");
      expect(body.models[0]).toMatchObject({
        id: "openrouter/anthropic/claude-sonnet-4.5",
        name: "Claude Sonnet 4.5",
        enabled: false,
        price: { output_usd_per_million_tokens: 12 },
      });
      return route.fulfill({
        json: { provider: seededProviders.providers[0] },
      });
    }
    if (path === "/admin/api/providers/openrouter/caps") {
      savedCaps = true;
      expect(route.request().method()).toBe("PUT");
      expect(route.request().postDataJSON()).toEqual({
        daily_usd: 7,
        monthly_usd: 70,
      });
      return route.fulfill({
        json: { provider: seededProviders.providers[0] },
      });
    }
    if (path === "/admin/api/providers/openrouter/allowed-plans") {
      savedPlans = true;
      expect(route.request().method()).toBe("PUT");
      expect(route.request().postDataJSON()).toEqual({
        allowed_plans: ["paid"],
      });
      return route.fulfill({
        json: { provider: seededProviders.providers[0] },
      });
    }
    if (path !== "/admin/api/providers") return route.continue();
    if (route.request().method() === "POST") {
      saved = true;
      const body = route.request().postDataJSON();
      expect(body).toMatchObject({
        preset_id: "openrouter",
        provider_id: "openrouter",
        key_mode: "secret",
        api_key: "sk-or-browser-test",
        allowed_plans: [],
      });
      expect(body.models[0]).toEqual({
        id: "openrouter/anthropic/claude-sonnet-4.5",
        name: "Claude Sonnet 4.5",
        upstream_model: "anthropic/claude-sonnet-4.5",
        enabled: true,
        price: {
          input_usd_per_million_tokens: 3,
          output_usd_per_million_tokens: 15,
        },
      });
      return route.fulfill({
        json: { provider: seededProviders.providers[0] },
      });
    }
    if (state === "error") {
      return route.fulfill({
        status: 503,
        json: { error: { message: "Provider API unavailable" } },
      });
    }
    return route.fulfill({
      json: saved || state === "seeded" ? seededProviders : { providers: [] },
    });
  });
  await page.route("**/me/api/models/catalog", (route: Route) =>
    route.fulfill({
      json: {
        models: saved ? [{ id: "openrouter/anthropic/claude-sonnet-4.5" }] : [],
      },
    }),
  );
  await page.route("**/openai/v1/chat/completions", (route: Route) =>
    route.fulfill({
      status: 403,
      json: {
        error: {
          code: "paid_provider_not_allowed",
          message:
            "Paid provider OpenRouter is not allowed by this access plan.",
        },
      },
    }),
  );
}

async function inspect(page: Page, name: string) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    `${name} must not overflow horizontally`,
  ).toBe(true);
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

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`providers ${state}: ${theme} ${width}`, async ({ page }) => {
        await setViewport(page, theme, width);
        await routeProviders(page, state);
        await page.goto("/ui/providers/");
        await expect(
          page.getByRole("heading", { name: "Providers" }),
        ).toBeVisible();
        if (state === "seeded") {
          await expect(
            page.getByRole("heading", { name: "OpenRouter" }),
          ).toBeVisible();
          await expect(
            page.getByRole("cell", { name: "Claude Sonnet 4.5" }),
          ).toBeVisible();
          await expect(page.getByText(/Set · ends in 4f2a/)).toBeVisible();
        } else if (state === "empty") {
          await expect(
            page.getByText(
              "Connect a provider to use cloud models alongside your local ones.",
            ),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Add provider" }).first(),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("button", { name: "Retry providers" }),
          ).toBeVisible();
        }
        await inspect(page, `providers-${state}-${theme}-${width}`);
      });
    }
  }
}

test("providers detail edits models caps and plans through real admin paths", async ({
  page,
}) => {
  await routeProviders(page, "seeded");
  await page.goto("/ui/providers/");
  await page.getByRole("button", { name: "Edit models" }).click();
  await page.getByLabel("Enable Claude Sonnet 4.5").click();
  await page.getByLabel("Claude Sonnet 4.5 managed output price").fill("12");
  await page.getByRole("button", { name: "Save models" }).click();
  await page.getByRole("button", { name: "Edit caps" }).click();
  await page.getByLabel("OpenRouter daily cap").fill("7");
  await page.getByLabel("OpenRouter monthly cap").fill("70");
  await page.getByRole("button", { name: "Save caps" }).click();
  await page.getByRole("button", { name: "Edit plans" }).click();
  await page.getByLabel("Allow Paid cloud for OpenRouter").click();
  await page.getByRole("button", { name: "Save plans" }).click();
  const flags = await page.evaluate(() =>
    fetch("/__provider-test-flags").then((response) => response.json()),
  );
  expect(flags).toEqual({
    savedModels: true,
    savedCaps: true,
    savedPlans: true,
    reconciled: false,
  });
});

test("providers reconcile pending charges before removal", async ({ page }) => {
  await routeProviders(page, "seeded");
  await page.goto("/ui/providers/");
  await expect(page.getByText("1 charge needs reconciliation")).toBeVisible();
  await page.getByRole("button", { name: "Reconcile charges" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "req_pending_42_abcdefghijklmnopqrstuvwxyz",
  );
  await expect(page.getByRole("dialog")).toContainText(
    "gen_pending_42_abcdefghijklmnopqrstuvwxyz",
  );
  await expect(page.getByText("$0.015")).toBeVisible();
  await expect(page.getByText("$0.000001234")).toBeVisible();
  const firstActual = page.getByLabel(
    "Verified actual charge for reconciliation 42",
  );
  await expect(firstActual).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Record actual" }).first(),
  ).toBeDisabled();
  await firstActual.fill("0.000001234");
  await page.getByRole("button", { name: "Record actual" }).first().click();
  await expect(page.getByText("Charge #42")).toBeHidden();
  const zeroActual = page.getByLabel(
    "Verified actual charge for reconciliation 43",
  );
  await expect(zeroActual).toHaveValue("");
  await zeroActual.fill("0");
  const zeroButton = page
    .getByRole("button", { name: "Record actual" })
    .first();
  await expect(zeroButton).toBeEnabled();
  await zeroButton.click();
  await expect(
    page.getByText("No pending provider charges need reconciliation."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Done" }).click();
  const flags = await page.evaluate(() =>
    fetch("/__provider-test-flags").then((response) => response.json()),
  );
  expect(flags.reconciled).toBe(true);
});

for (const theme of ["light", "dark"] as const) {
  test(`providers reconciliation dialog: ${theme} 390`, async ({ page }) => {
    await setViewport(page, theme, 390);
    await routeProviders(page, "seeded");
    await page.goto("/ui/providers/");
    await expect(page.getByText("1 charge needs reconciliation")).toBeVisible();
    await inspect(page, `providers-reconciliation-card-${theme}-390`);
    await page.getByRole("button", { name: "Reconcile charges" }).click();
    await expect(page.getByRole("dialog")).toContainText(
      "req_pending_42_abcdefghijklmnopqrstuvwxyz",
    );
    await inspect(page, `providers-reconciliation-dialog-${theme}-390`);
  });
}

type ProviderTestRequest = Record<string, unknown>;

function providerTestResponse(providerId: string) {
  const label = providerId === "openrouter" ? "OpenRouter" : "Generic OpenAI";
  return {
    ok: true,
    message: `Listed 1 model from ${label}.`,
    models: [
      {
        id: `${providerId}/anthropic/claude-sonnet-4.5`,
        name: "Claude Sonnet 4.5",
        upstream_model: "anthropic/claude-sonnet-4.5",
        price: {
          input_usd_per_million_tokens: 3,
          output_usd_per_million_tokens: 15,
        },
      },
    ],
  };
}

async function routeProviderCredentialSecurity(
  page: Page,
  options: { delayOpenRouterCheck?: () => Promise<void> } = {},
) {
  const checks: ProviderTestRequest[] = [];
  await page.route("**/admin/api/profiles", (route: Route) =>
    route.fulfill({ json: plans }),
  );
  await page.route("**/admin/api/providers**", async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/admin/api/providers/-/catalog") {
      return route.fulfill({ json: catalog });
    }
    if (path === "/admin/api/providers/-/test") {
      const body = route.request().postDataJSON() as ProviderTestRequest;
      checks.push(body);
      if (body.provider_id === "openrouter" && options.delayOpenRouterCheck) {
        await options.delayOpenRouterCheck();
      }
      return route.fulfill({
        json: providerTestResponse(String(body.provider_id)),
      });
    }
    if (path === "/admin/api/providers") {
      return route.fulfill({ json: { providers: [] } });
    }
    return route.continue();
  });
  return checks;
}

async function openAddProvider(page: Page) {
  await page.goto("/ui/providers/");
  await page.getByRole("button", { name: "Add provider" }).first().click();
}

async function chooseCatalogProvider(page: Page, search: string, name: RegExp) {
  await page.getByLabel("Search providers").fill(search);
  await page.getByRole("button", { name }).click();
}

test("providers add OpenRouter and keep paid access gated", async ({
  page,
}) => {
  await routeProviders(page, "empty");
  await page.goto("/ui/providers/");
  await page.getByRole("button", { name: "Add provider" }).first().click();
  await page.getByLabel("Search providers").fill("openrouter");
  await page.getByRole("button", { name: /OpenRouter/ }).click();
  await expect(page.getByLabel("Provider base URL")).toHaveValue(
    "https://openrouter.ai/api/v1",
  );
  await page.getByLabel("Provider API key").fill("sk-or-browser-test");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await expect(
    page.getByText("Listed 2 models from OpenRouter."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("Claude Sonnet 4.5")).toBeVisible();
  await expect(page.getByText("Unknown new model")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    page.getByText(
      "No plans selected. Paid requests will be denied until an owner allows a plan.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("heading", { name: "OpenRouter" })).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "Claude Sonnet 4.5" }),
  ).toBeVisible();

  const models = await page.evaluate(() =>
    fetch("/me/api/models/catalog").then((response) => response.json()),
  );
  expect(models).toEqual({
    models: [{ id: "openrouter/anthropic/claude-sonnet-4.5" }],
  });
  const denied = await page.evaluate(() =>
    fetch("/openai/v1/chat/completions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello" }],
      }),
    }).then(async (response) => ({
      status: response.status,
      body: await response.json(),
    })),
  );
  expect(denied).toMatchObject({
    status: 403,
    body: { error: { code: "paid_provider_not_allowed" } },
  });
});

test("providers clear pasted key when switching provider before Gateway check", async ({
  page,
}) => {
  const checks = await routeProviderCredentialSecurity(page);
  await openAddProvider(page);
  await chooseCatalogProvider(page, "openrouter", /OpenRouter/);
  await page.getByLabel("Provider API key").fill("sk-provider-a-secret");
  await page.getByRole("button", { name: "Back" }).click();
  await chooseCatalogProvider(page, "generic", /Generic OpenAI-compatible/);
  await expect(page.getByLabel("Provider API key")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Gateway check" }),
  ).toBeDisabled();

  await page.getByLabel("Provider API key").fill("sk-provider-b-secret");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await expect(
    page.getByText("Listed 1 model from Generic OpenAI."),
  ).toBeVisible();

  expect(checks).toHaveLength(1);
  expect(checks[0]).toMatchObject({
    provider_id: "generic-openai",
    key_mode: "secret",
    api_key: "sk-provider-b-secret",
  });
  expect(JSON.stringify(checks[0])).not.toContain("sk-provider-a-secret");
});

test("providers clear environment reference when switching provider before Gateway check", async ({
  page,
}) => {
  const checks = await routeProviderCredentialSecurity(page);
  await openAddProvider(page);
  await chooseCatalogProvider(page, "openrouter", /OpenRouter/);
  await page.getByRole("button", { name: "Use environment variable" }).click();
  await page.getByLabel("Environment variable").fill("OPENROUTER_A_KEY");
  await page.getByRole("button", { name: "Back" }).click();
  await chooseCatalogProvider(page, "generic", /Generic OpenAI-compatible/);
  await expect(page.getByLabel("Environment variable")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Gateway check" }),
  ).toBeDisabled();

  await page.getByLabel("Environment variable").fill("GENERIC_B_KEY");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await expect(
    page.getByText("Listed 1 model from Generic OpenAI."),
  ).toBeVisible();

  expect(checks).toHaveLength(1);
  expect(checks[0]).toMatchObject({
    provider_id: "generic-openai",
    key_mode: "env",
    api_key_env: "GENERIC_B_KEY",
  });
  expect(JSON.stringify(checks[0])).not.toContain("OPENROUTER_A_KEY");
});

test("providers clear credentials when base URL changes before Gateway check", async ({
  page,
}) => {
  const checks = await routeProviderCredentialSecurity(page);
  await openAddProvider(page);
  await chooseCatalogProvider(page, "openrouter", /OpenRouter/);
  await page.getByLabel("Provider API key").fill("sk-before-base-change");
  await page
    .getByLabel("Provider base URL")
    .fill("https://alt-openrouter.example/v1");
  await expect(page.getByLabel("Provider API key")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Gateway check" }),
  ).toBeDisabled();

  await page.getByLabel("Provider API key").fill("sk-after-base-change");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await expect(page.getByText("Listed 1 model from OpenRouter.")).toBeVisible();

  expect(checks).toHaveLength(1);
  expect(checks[0]).toMatchObject({
    provider_id: "openrouter",
    api_key: "sk-after-base-change",
    base_url: "https://alt-openrouter.example/v1",
  });
  expect(JSON.stringify(checks[0])).not.toContain("sk-before-base-change");
});

test("providers ignore stale Gateway check results after provider switch", async ({
  page,
}) => {
  let releaseOpenRouter!: () => void;
  const openRouterCanFinish = new Promise<void>((resolve) => {
    releaseOpenRouter = resolve;
  });
  const checks = await routeProviderCredentialSecurity(page, {
    delayOpenRouterCheck: () => openRouterCanFinish,
  });
  await openAddProvider(page);
  await chooseCatalogProvider(page, "openrouter", /OpenRouter/);
  await page.getByLabel("Provider API key").fill("sk-provider-a-secret");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await page.getByRole("button", { name: "Back" }).click();
  await chooseCatalogProvider(page, "generic", /Generic OpenAI-compatible/);
  releaseOpenRouter();
  await page.waitForTimeout(100);

  await expect(
    page.getByRole("button", { name: "Gateway check" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  await expect(page.getByText("Listed 1 model from OpenRouter.")).toHaveCount(
    0,
  );

  await page.getByLabel("Provider API key").fill("sk-provider-b-secret");
  await page.getByRole("button", { name: "Gateway check" }).click();
  await expect(
    page.getByText("Listed 1 model from Generic OpenAI."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("Claude Sonnet 4.5")).toBeVisible();

  expect(checks).toHaveLength(2);
  expect(checks[0]).toMatchObject({
    provider_id: "openrouter",
    api_key: "sk-provider-a-secret",
  });
  expect(checks[1]).toMatchObject({
    provider_id: "generic-openai",
    api_key: "sk-provider-b-secret",
  });
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
    .put("/admin/api/providers/openrouter/allowed-plans", {
      headers,
      data: { allowed_plans: [] },
    })
    .catch(() => undefined);
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

test("providers real backend OpenRouter journey keeps paid access denied until allowed", async ({
  page,
  request,
  baseURL,
}) => {
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-ai-gateway-admin-action": "1",
    "x-ai-gateway-csrf": session.actor.csrf_token,
  };
  const unique = Date.now().toString(36);
  let profile: { id: string } | undefined;
  let observerProfile: { id: string } | undefined;
  let app: { id: string } | undefined;
  let observerApp: { id: string } | undefined;
  try {
    await clearOpenRouterProvider(request, headers);
    await removeSeededOpenRouterProvider(request, headers);
    const port = Number(new URL(baseURL!).port);

    const profileResponse = await request.post("/admin/api/profiles", {
      headers,
      data: {
        name: `Provider denied plan ${unique}`,
        profile: {
          id: `provider-denied-${unique}`,
          protocols: ["openai"],
          remote: { providers: [], budget_usd_per_day: "2.00" },
          status_visibility: { level: "full" },
        },
      },
    });
    expect(profileResponse.status()).toBe(201);
    ({ profile } = await profileResponse.json());
    const users = await (await request.get("/admin/api/users")).json();
    const observerProfileResponse = await request.post("/admin/api/profiles", {
      headers,
      data: {
        name: `Provider observer plan ${unique}`,
        profile: {
          id: `provider-observer-${unique}`,
          protocols: ["openai"],
          remote: { providers: ["openrouter"], budget_usd_per_day: "2.00" },
          status_visibility: { level: "full" },
        },
      },
    });
    expect(observerProfileResponse.status()).toBe(201);
    ({ profile: observerProfile } = await observerProfileResponse.json());
    const observerAppResponse = await request.post("/admin/api/apps", {
      headers,
      data: {
        name: `Provider observer app ${unique}`,
        kind: "personal",
        owner_user_id: users.users[0].id,
        profile_id: observerProfile!.id,
      },
    });
    expect(observerAppResponse.status()).toBe(201);
    ({ app: observerApp } = await observerAppResponse.json());
    const appResponse = await request.post("/admin/api/apps", {
      headers,
      data: {
        name: `Provider test app ${unique}`,
        kind: "personal",
        owner_user_id: users.users[0].id,
        profile_id: profile!.id,
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

    await page.goto("/ui/providers/");
    await page.getByRole("button", { name: "Add provider" }).first().click();
    await page.getByLabel("Search providers").fill("openrouter");
    await page.getByRole("button", { name: /OpenRouter/ }).click();
    await page
      .getByLabel("Provider base URL")
      .fill(`http://127.0.0.1:${port + 1}/provider/v1`);
    await page.getByLabel("Provider API key").fill("sk-test-openrouter");
    await page.getByRole("button", { name: "Gateway check" }).click();
    await expect(page.getByText(/Listed 1 models?/)).toBeVisible();
    await page.getByRole("button", { name: "Next" }).click();
    await expect(page.getByText("Claude Sonnet 4.5")).toBeVisible();
    await expect(page.getByLabel("Claude Sonnet 4.5 input price")).toHaveValue(
      "3.000000",
    );
    await expect(page.getByLabel("Claude Sonnet 4.5 output price")).toHaveValue(
      "15.000000",
    );
    await page.getByRole("button", { name: "Next" }).click();
    await expect(
      page.getByText(
        "No plans selected. Paid requests will be denied until an owner allows a plan.",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Save provider" }).click();
    await expect(
      page.getByRole("heading", { name: "OpenRouter" }),
    ).toBeVisible();
    await expect(page.getByText("sk-test-openrouter")).toHaveCount(0);
    await expect(
      page.getByRole("cell", { name: "Claude Sonnet 4.5" }),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "Input $/M" }),
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

    const allowResponse = await request.put(
      "/admin/api/providers/openrouter/allowed-plans",
      {
        headers,
        data: { allowed_plans: [observerProfile!.id, profile!.id] },
      },
    );
    expect(allowResponse.status()).toBe(200);

    await page.goto("/ui/models/");
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await page.getByRole("button", { name: /All models/ }).click();
    await expect(page.getByText("Claude Sonnet 4.5")).toBeVisible();
    await expect(
      page.getByRole("cell", {
        name: /Paid · \$3 input \/ \$15 output per 1M tokens/,
      }),
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

    await page.goto("/ui/providers/");
    await expect(
      page.getByRole("button", { name: "Reconcile charges" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Reconcile charges" }).click();
    await expect(page.getByRole("dialog")).toContainText(String(pendingId));
    const actualCharge = page.getByLabel(
      new RegExp(`Verified actual charge for reconciliation ${pendingId}`),
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
    expect(await reconcileResponse.json()).toMatchObject({
      reconciliation: { status: "settled", actual_usd: "0.000001234" },
    });
    await expect(
      page.getByText("No pending provider charges need reconciliation."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();

    const afterReconcile = await request.get(
      "/admin/api/providers/openrouter/reconciliation",
    );
    expect(afterReconcile.status()).toBe(200);
    expect(await afterReconcile.json()).toMatchObject({ reconciliations: [] });

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
    if (app?.id) {
      await request
        .delete(`/admin/api/apps/${app.id}`, { headers })
        .catch(() => undefined);
    }
    if (observerApp?.id) {
      await request
        .delete(`/admin/api/apps/${observerApp.id}`, { headers })
        .catch(() => undefined);
    }
    if (profile?.id) {
      await request
        .delete(`/admin/api/profiles/${profile.id}`, { headers })
        .catch(() => undefined);
    }
    if (observerProfile?.id) {
      await request
        .delete(`/admin/api/profiles/${observerProfile.id}`, { headers })
        .catch(() => undefined);
    }
  }
});
