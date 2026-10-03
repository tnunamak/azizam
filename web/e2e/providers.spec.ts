import { test, expect, type Page, type Route } from "@playwright/test";
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
      spend: { daily_usd: 1.2, monthly_usd: 14.5, status: "measured" },
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
    if (path === "/__provider-test-flags") {
      return route.fulfill({ json: { savedModels, savedCaps, savedPlans } });
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
  });
});

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
  let app: { id: string } | undefined;
  try {
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

    const port = Number(new URL(baseURL!).port);
    await page.goto("/ui/providers/");
    await page.getByRole("button", { name: "Add provider" }).first().click();
    await page.getByLabel("Search providers").fill("openrouter");
    await page.getByRole("button", { name: /OpenRouter/ }).click();
    await page
      .getByLabel("Provider base URL")
      .fill(`http://127.0.0.1:${port + 1}/provider/v1`);
    await page
      .getByLabel("Provider API key")
      .fill("sk-or-isolated-browser-test");
    await page.getByRole("button", { name: "Gateway check" }).click();
    await expect(page.getByText(/model/i).first()).toBeVisible();
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
    await expect(
      page.getByRole("cell", { name: "Claude Sonnet 4.5" }),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "Input $/M" }),
    ).toBeVisible();
    await expect(page.getByRole("cell", { name: "3.000000" })).toBeVisible();

    const denied = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello" }],
        max_tokens: 100,
      },
    });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toMatchObject({
      error: { code: "paid_provider_not_allowed" },
    });

    const allowResponse = await request.put(
      "/admin/api/providers/openrouter/allowed-plans",
      {
        headers,
        data: { allowed_plans: [profile!.id] },
      },
    );
    expect(allowResponse.status()).toBe(200);

    await page.goto("/ui/models/");
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await expect(page.getByText("Claude Sonnet 4.5")).toBeVisible();
    await expect(page.getByText(/Paid/)).toBeVisible();

    const allowed = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${credential.api_key}` },
      data: {
        model: "openrouter/anthropic/claude-sonnet-4.5",
        messages: [{ role: "user", content: "hello" }],
        max_tokens: 100,
      },
    });
    expect(allowed.status(), await allowed.text()).toBe(200);
  } finally {
    await request
      .put("/admin/api/providers/openrouter/allowed-plans", {
        headers,
        data: { allowed_plans: [] },
      })
      .catch(() => undefined);
    await request
      .delete("/admin/api/providers/openrouter", {
        headers,
        data: { expected_generation: null },
      })
      .catch(() => undefined);
    if (app?.id) {
      await request
        .delete(`/admin/api/apps/${app.id}`, { headers })
        .catch(() => undefined);
    }
    if (profile?.id) {
      await request
        .delete(`/admin/api/profiles/${profile.id}`, { headers })
        .catch(() => undefined);
    }
  }
});
