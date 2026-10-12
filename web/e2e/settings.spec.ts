import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function seed(page: Page, state: "seeded" | "empty" | "error") {
  await page.route("**/admin/api/settings/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.includes("receipts")
      ? { revision: "7", retention_days: null }
      : path.includes("instance")
      ? {
          revision: "7",
          instance: {
            name: "Our gateway",
            base_url: "https://gateway.example.test",
            theme_default: "system",
          },
        }
      : path.includes("prompt-memory")
        ? {
            revision: "7",
            prompt_memory: {
              default_remember: true,
              never_keep: false,
              retention_cap: "never",
            },
          }
        : path.includes("defaults")
          ? {
              defaults: { profile_id: state === "empty" ? null : "local" },
              aliases: state === "empty" ? {} : { chat: "local-chat" },
            }
          : {
              config: {
                proxy: { port: 18000 },
                secret: "<redacted>",
                example_endpoint: `https://gateway.example.test/${"recorded-path/".repeat(25)}`,
              },
              export_url: "/admin/api/settings/effective/export",
            };
    return route.fulfill({ status: state === "error" ? 503 : 200, json: body });
  });
  await page.route("**/admin/api/oidc-config", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        revision: "7",
        oidc_config:
          state === "empty"
            ? null
            : {
                enabled: true,
                issuer: "https://login.example.test",
                client_id: "gateway",
                client_secret_set: true,
                redirect_uri: "https://gateway.example.test/auth/callback",
                scopes: ["openid", "email"],
                groups_claim: "groups",
                allowed_email_domains: [],
                admin_emails: [],
                admin_groups: [],
                admin_roles: [],
                authorization_endpoint: "",
                token_endpoint: "",
                jwks_uri: "",
                clock_skew_seconds: 60,
              },
      },
    }),
  );
  await page.route("**/admin/api/operator/sign-in", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        revision: "7",
        mode: state === "empty" ? "built-in" : "both",
        built_in: true,
        oidc: state !== "empty",
        oidc_label: "Example identity",
      },
    }),
  );
  await page.route("**/admin/api/operator/overrides", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        overrides: {
          revisions: { media_job_admission_overrides: "7" },
          media_job_admission:
            state === "empty"
              ? {}
              : { max_queue_depth: 5, retry_after_seconds: 30 },
          user_access_defaults: {
            self_service_apps: true,
            auto_create_personal_app: true,
          },
        },
      },
    }),
  );
  await page.route("**/admin/api/extensions", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        extension_packages: {
          packages:
            state === "empty"
              ? {}
              : {
                  recordings: {
                    id: "recordings",
                    version: "1.0",
                    enabled: true,
                    enabled_at: Date.now() / 1000 - 3600,
                  },
                },
        },
      },
    }),
  );
  await page.route("**/admin/api/profiles", (route) =>
    route.fulfill({
      json: {
        profiles: [
          {
            id: "local",
            name: "Local only",
            description: "Fixture local access",
            profile: {},
          },
        ],
      },
    }),
  );
}

async function editSection(page: Page, name: string) {
  const section = page.getByRole("region", { name, exact: true });
  await section
    .getByRole("button", { name: `Edit ${name}`, exact: true })
    .click();
  return section;
}

// One real-browser pass per state (display copy and truth tables live in
// src/app/settings/settings-page.spec.tsx); theme and width vary across them.
for (const [state, theme, width] of [
  ["seeded", "light", 1440],
  ["empty", "dark", 390],
  ["error", "light", 390],
] as const) {
  {
    {
      test(`settings: ${state}, ${theme}, ${width}px`, async ({ page }) => {
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
        await seed(page, state);
        await page.goto("/settings/");
        await expect(
          page.getByRole("heading", { name: "Settings", exact: true }),
        ).toBeVisible();
        for (const name of [
          "Instance",
          "Sign-in",
          "Defaults",
          "App creation",
          "Prompts",
          "Receipts",
          "Media admission",
          "Extensions",
          "Advanced",
        ]) {
          await expect(
            page.getByRole("heading", { name, exact: true }),
          ).toBeVisible();
        }
        await editSection(page, "Advanced");
        await page
          .getByText("Effective configuration and export", { exact: true })
          .click();
        if (state === "error") {
          await expect(
            page.getByRole("region", { name: "Receipts", exact: true })
              .getByText("Couldn’t load receipt settings"),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toHaveCount(10);
          for (const name of ["Defaults", "App creation"]) {
            const retry = page
              .getByRole("region", { name, exact: true })
              .getByRole("button", { name: "Try again" });
            expect((await retry.boundingBox())?.height).toBeGreaterThanOrEqual(
              44,
            );
          }
        } else {
          await expect(
            page.getByText("Our gateway", { exact: true }),
          ).toBeVisible();
          await expect(
            page.getByLabel("Instance name", { exact: true }),
          ).toHaveCount(0);
          const instance = await editSection(page, "Instance");
          await expect(instance.getByLabel("Instance name")).toHaveValue(
            "Our gateway",
          );
          await expect(
            page.getByText("Built-in accounts on", { exact: true }),
          ).toHaveCount(0);
          const signIn = await editSection(page, "Sign-in");
          await expect(
            signIn.getByText("Built-in accounts on", { exact: true }),
          ).toBeVisible();
          await expect(
            signIn.getByLabel("Available sign-in methods"),
          ).toContainText(
            state === "empty"
              ? "Built-in accounts only"
              : "Built-in accounts and OIDC",
          );
          await expect(
            page.getByText(state === "empty" ? "OIDC off" : "OIDC on", {
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            page.getByRole("link", { name: "Export redacted snapshot" }),
          ).toBeVisible();
          if (state === "empty") {
            await expect(
              page.getByRole("region", { name: "Extensions", exact: true }),
            ).toContainText("No extensions installed");
            await editSection(page, "Extensions");
            await expect(
              page.getByText("No extensions installed", { exact: true }),
            ).toBeVisible();
            await expect(
              page.getByText(/No identity provider is connected yet/),
            ).toBeVisible();
          }
        }
        await expect
          .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
          .toBe(true);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({
          path: `test-results/screens/settings-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
      });
    }
  }
}

test("settings saves each section through its own API and keeps OIDC secret write-only", async ({
  page,
}) => {
  // The default access plan is shared server state: the owner inherits it, so
  // a "Local chat" default left behind hides every workflow from later specs
  // (try-media-inputs read an empty catalog). Put the original plan back.
  const defaults = await page.request.get("/admin/api/settings/defaults");
  expect(defaults.ok()).toBe(true);
  const originalPlan = (
    (await defaults.json()) as {
      defaults: { profile_id: string | null };
    }
  ).defaults.profile_id;
  const accessDefaults = await page.request.get(
    "/admin/api/operator/overrides",
  );
  const originalApps = (await accessDefaults.json()).overrides
    .user_access_defaults;
  try {
    await settingsJourney(page);
  } finally {
    const session = await (await page.request.get("/admin/api/session")).json();
    const restored = await page.request.put("/admin/api/settings/defaults", {
      headers: {
        "x-azizam-admin-action": "1",
        "x-azizam-csrf": session.actor.csrf_token,
      },
      data: { profile_id: originalPlan },
    });
    expect(restored.ok(), "restore the default access plan").toBe(true);
    const appsRestored = await page.request.put(
      "/admin/api/operator/user-access-defaults",
      {
        headers: {
          "x-azizam-admin-action": "1",
          "x-azizam-csrf": session.actor.csrf_token,
        },
        data: originalApps,
      },
    );
    expect(appsRestored.ok(), "restore app creation settings").toBe(true);
  }
});

async function settingsJourney(page: Page) {
  await page.goto("/settings/");
  await editSection(page, "Instance");
  await expect(page.getByLabel("Instance name")).toBeVisible();
  await page.getByLabel("Instance name").fill("Browser settings journey");
  const saved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/settings/instance") &&
      r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save instance", exact: true })
    .click();
  expect((await saved).status()).toBe(200);
  await expect(
    page.getByText("Instance settings saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await editSection(page, "Instance");
  await expect(page.getByLabel("Instance name")).toHaveValue(
    "Browser settings journey",
  );
  await expect(
    page
      .locator("aside")
      .getByText("Browser settings journey", { exact: true }),
  ).toBeVisible();
  await editSection(page, "Media admission");
  await page.getByLabel("Maximum queued jobs").fill("7");
  const mediaSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/media-job-admission") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save media admission" }).click();
  expect((await mediaSaved).status()).toBe(200);
  await page.reload();
  await editSection(page, "Media admission");
  await expect(page.getByLabel("Maximum queued jobs")).toHaveValue("7");
  await expect(
    page.getByRole("region", { name: "Defaults", exact: true }),
  ).toBeVisible();
  await page.getByLabel("Default access plan").click();
  await page.getByRole("option", { name: "Local chat", exact: true }).click();
  const planSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/settings/defaults") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save default plan" }).click();
  expect((await planSaved).status()).toBe(200);
  await expect(
    page
      .getByRole("region", { name: "Defaults", exact: true })
      .getByRole("status"),
  ).toHaveText("Default access plan saved");
  const personalApp = page.getByRole("switch", {
    name: "Create a personal app automatically",
  });
  const wasAutomatic = await personalApp.isChecked();
  await personalApp.click();
  const appSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/user-access-defaults") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save app defaults" }).click();
  expect((await appSaved).status()).toBe(200);
  await expect(
    page
      .getByRole("region", { name: "App creation", exact: true })
      .getByRole("status"),
  ).toHaveText("Personal app defaults saved");
  await page.reload();
  await expect(personalApp).toBeChecked({ checked: !wasAutomatic });
  await expect(
    page.getByRole("region", { name: "Defaults", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Default access plan")).toContainText(
    "Local chat",
  );
  await editSection(page, "Sign-in");
  await page.getByLabel("Available sign-in methods").click();
  await page
    .getByRole("option", { name: "Built-in accounts only", exact: true })
    .click();
  const signInChoiceSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/sign-in") &&
      r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save sign-in choice", exact: true })
    .click();
  expect((await signInChoiceSaved).status()).toBe(200);
  await expect(
    page.getByText("Sign-in choice saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await editSection(page, "Sign-in");
  await expect(page.getByLabel("Available sign-in methods")).toContainText(
    "Built-in accounts only",
  );
  await page.getByLabel("Available sign-in methods").click();
  await page.getByRole("option", { name: "OIDC only", exact: true }).click();
  const signInChoiceRejected = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/sign-in") &&
      r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save sign-in choice", exact: true })
    .click();
  expect((await signInChoiceRejected).status()).toBe(409);
  await expect(
    page.getByRole("alert").filter({
      hasText:
        "Configure an identity provider before enabling this sign-in option.",
    }),
  ).toBeVisible();
  await page.getByLabel("Available sign-in methods").click();
  await page
    .getByRole("option", { name: "Built-in accounts only", exact: true })
    .click();
  const signInChoiceRestored = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/sign-in") &&
      r.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save sign-in choice", exact: true })
    .click();
  expect((await signInChoiceRestored).status()).toBe(200);
  await page.getByLabel("Issuer URL").fill("https://login.example.test");
  await page.getByLabel("Client ID", { exact: true }).fill("browser-test");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("browser-fixture-secret");
  await page
    .getByLabel("Redirect URL")
    .fill("https://gateway.example.test/auth/callback");
  const oidcSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/oidc-config") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save sign-in", exact: true }).click();
  expect((await oidcSaved).status()).toBe(200);
  await page.reload();
  await editSection(page, "Sign-in");
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveValue(
    "",
  );
  await expect(
    page.getByText("A secret is saved. Leave this blank to keep it."),
  ).toBeVisible();
  const preserve = page.waitForRequest(
    (r) => r.url().endsWith("/admin/api/oidc-config") && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "Save sign-in", exact: true }).click();
  expect((await preserve).postDataJSON()).not.toHaveProperty("client_secret");
  await editSection(page, "Advanced");
  await page
    .getByText("Effective configuration and export", { exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Export redacted snapshot" }),
  ).toBeVisible();
  await expect(page.locator("main")).not.toContainText("browser-fixture-secret");
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export redacted snapshot" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("gateway-effective-config.json");
}

test("a provider draft saves after a real sign-in choice write", async ({
  page,
}) => {
  await page.goto("/settings/");
  await editSection(page, "Sign-in");
  await page
    .getByLabel("Client ID", { exact: true })
    .fill("independent-provider-draft");
  await page.getByLabel("Available sign-in methods").click();
  await page
    .getByRole("option", { name: "Built-in accounts only", exact: true })
    .click();
  const modeSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/admin/api/operator/sign-in") &&
      response.request().method() === "PUT",
  );
  await page
    .getByRole("button", { name: "Save sign-in choice", exact: true })
    .click();
  expect((await modeSaved).status()).toBe(200);
  await expect(page.getByLabel("Client ID", { exact: true })).toHaveValue(
    "independent-provider-draft",
  );
  const providerSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/admin/api/oidc-config") &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save sign-in", exact: true }).click();
  expect((await providerSaved).status()).toBe(200);
  await page.reload();
  await editSection(page, "Sign-in");
  await expect(page.getByLabel("Client ID", { exact: true })).toHaveValue(
    "independent-provider-draft",
  );
});



test("new visitors use the saved default theme and personal choices persist", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({ colorScheme: "light" });
  const page = await context.newPage();
  await page.route("**/.well-known/azizam", (route) =>
    route.fulfill({
      json: {
        service: { name: "Our gateway" },
        presentation: { theme_default: "dark" },
      },
    }),
  );
  await page.goto(`${baseURL}/sign-in/`);
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await page.reload();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await context.close();
});





// Craft PR 2: a setting row is one line of label and one of detail on a
// phone (64 px), and its detail is one line everywhere. Long text never
// overflows: the name wraps above 640 px; a truncated line carries its full
// text in its title; Edit shows the full values.
for (const width of [390]) {
  test(`long instance names and public URLs stay inside the page at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await seed(page, "seeded");
    const name = "n".repeat(300);
    const url = `https://example.test/${"path".repeat(100)}`;
    await page.route("**/admin/api/settings/instance", (route) =>
      route.fulfill({
        json: { instance: { name, base_url: url, theme_default: "system" } },
      }),
    );
    await page.goto("/settings/");
    const section = page.getByRole("region", { name: "Instance", exact: true });
    for (const text of [name, url]) {
      const field = section.getByText(text, { exact: text === name });
      await expect(field).toBeVisible();
      await expect.poll(() => field.evaluate((el) => el.getBoundingClientRect().right <= innerWidth)).toBe(true);
      // Wrapped in full, or cut with an ellipsis that keeps the full text.
      const fits = await field.evaluate(
        (el) => el.scrollWidth <= el.clientWidth,
      );
      if (!fits) {
        await expect(field).toHaveCSS("text-overflow", "ellipsis");
        await expect.poll(() => field.getAttribute("title")).toContain(text);
      }
      if (text === name && width === 1440) expect(fits).toBe(true);
    }
    await editSection(page, "Instance");
    await expect(section.getByLabel("Instance name")).toHaveValue(name);
    await expect(section.getByLabel("Public base URL")).toHaveValue(url);
    await expect
          .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
          .toBe(true);
  });
}



for (const theme of ["dark"] as const) {
  for (const width of [390]) {
    test(`default access choices, keyboard, targets and 200% text: ${theme}, ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await seed(page, "empty");
      await page.goto("/settings/");
      const defaults = page.getByRole("region", {
        name: "Defaults",
        exact: true,
      });
      const apps = page.getByRole("region", {
        name: "App creation",
        exact: true,
      });
      const plan = page.getByLabel("Default access plan");
      await expect(plan).toContainText("None — built-in local access");
      await expect(
        defaults.getByText(/Choosing none does not block AI access/),
      ).toBeVisible();
      await expect(
        apps.getByRole("switch", {
          name: "Create a personal app automatically",
        }),
      ).toBeChecked();
      await defaults.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `${process.env.UI_DEFAULTS_SHOTS_DIR ?? "test-results/screens"}/after-${width}-${theme}.png`,
        fullPage: true,
      });
      await plan.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("option", {
          name: "None — built-in local access",
          exact: true,
        }),
      ).toBeFocused();
      await page.keyboard.press("End");
      await expect(
        page.getByRole("option", { name: "Local only", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(plan).toContainText("Local only");
      await expect(plan).toBeFocused();
      await expect(
        defaults.getByText(/capabilities allowed by this plan/),
      ).toBeVisible();
      await page.screenshot({
        path: `${process.env.UI_DEFAULTS_SHOTS_DIR ?? "test-results/screens"}/after-plan-${width}-${theme}.png`,
        fullPage: true,
      });
      await expect(
        defaults.getByRole("link", { name: "View and edit access plans" }),
      ).toHaveAttribute("href", "/people/?tab=plans");
      for (const textSize of ["100%", "200%"]) {
        await page.evaluate((size) => {
          document.documentElement.style.fontSize = size;
        }, textSize);
        await expect(plan).toBeVisible();
        if (textSize === "200%") {
          await plan.click();
          await page
            .getByRole("option", {
              name: "None — built-in local access",
              exact: true,
            })
            .click();
          await expect(plan).toContainText("None — built-in local access");
          await expect(
            page.locator('[data-slot="select-content"]'),
          ).toHaveCount(0);
        }
        const overflow = await page.locator("body *").evaluateAll((elements) =>
          elements
            .filter(
              (el) =>
                el.getBoundingClientRect().width > 0 &&
                el.getBoundingClientRect().right > innerWidth + 1,
            )
            .map((el) => ({
              tag: el.tagName,
              slot: el.getAttribute("data-slot"),
              text: el.textContent?.slice(0, 70),
              right: el.getBoundingClientRect().right,
            })),
        );
        await expect
          .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: JSON.stringify(overflow) })
          .toBe(true);
        for (const section of [defaults, apps]) {
          for (const control of await section
            .locator("button, a, summary")
            .all()) {
            const bounds = await control.boundingBox();
            const sectionBounds = await section.boundingBox();
            expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
              sectionBounds!.x + sectionBounds!.width,
            );
            expect(
              bounds?.height,
              (await control.textContent()) ?? "control",
            ).toBeGreaterThanOrEqual(44);
          }
        }
      }
      await page.screenshot({
        path: `${process.env.UI_DEFAULTS_SHOTS_DIR ?? "test-results/screens"}/after-200pct-${width}-${theme}.png`,
        fullPage: true,
      });
      const axe = await new AxeBuilder({ page })
        .include('[aria-labelledby="defaults"]')
        .include('[aria-labelledby="app-creation"]')
        .analyze();
      expect(axe.violations).toEqual([]);
      // The secondary link opens the plan editor, not the People tab.
      await defaults
        .getByRole("link", { name: "View and edit access plans" })
        .click();
      await expect(
        page.getByRole("tab", { name: "Access plans" }),
      ).toHaveAttribute("data-state", "active");
    });
  }
}
