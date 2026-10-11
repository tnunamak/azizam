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
      json: { profiles: [{ id: "local", name: "Local only" }] },
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

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
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
        await page.goto("/ui/settings/");
        await expect(
          page.getByRole("heading", { name: "Settings", exact: true }),
        ).toBeVisible();
        for (const name of [
          "Instance",
          "Sign-in",
          "Defaults",
          "Prompts",
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
            page.getByRole("button", { name: "Try again" }),
          ).toHaveCount(9);
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
              page.getByText("No extensions installed").nth(1),
            ).toBeVisible();
            await expect(
              page.getByText(/No identity provider is connected yet/),
            ).toBeVisible();
          }
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
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
  const originalPlan = ((await defaults.json()) as {
    defaults: { profile_id: string | null };
  }).defaults.profile_id;
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
  }
});

async function settingsJourney(page: Page) {
  await page.goto("/ui/settings/");
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
  await editSection(page, "Defaults");
  await page.getByLabel("Default access plan").click();
  await page.getByRole("option", { name: "Local chat", exact: true }).click();
  const planSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/settings/defaults") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save default plan" }).click();
  expect((await planSaved).status()).toBe(200);
  await page.reload();
  await editSection(page, "Defaults");
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
  expect(await page.locator("main").innerText()).not.toContain(
    "browser-fixture-secret",
  );
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export redacted snapshot" }).click();
  expect((await download).suggestedFilename()).toBe(
    "gateway-effective-config.json",
  );
}

test("a provider draft saves after a real sign-in choice write", async ({
  page,
}) => {
  await page.goto("/ui/settings/");
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

test("settings announces delayed loading states for assistive technology", async ({
  page,
}) => {
  await seed(page, "seeded");
  let releaseSignIn: () => void = () => {};
  const signInResponse = new Promise<void>((resolve) => {
    releaseSignIn = resolve;
  });
  await page.route("**/admin/api/operator/sign-in", async (route) => {
    await signInResponse;
    return route.fulfill({
      json: {
        mode: "both",
        built_in: true,
        oidc: true,
        oidc_label: "Example identity",
      },
    });
  });
  await page.goto("/ui/settings/");
  const signIn = page.getByRole("region", { name: "Sign-in", exact: true });
  const status = signIn.getByRole("status", {
    name: "Loading sign-in choices",
  });
  await expect(status).toHaveAttribute("aria-busy", "true");
  releaseSignIn();
  await expect(status).toHaveCount(0);
});

test("settings reports save failure and retries a failed load", async ({
  page,
}) => {
  await seed(page, "seeded");
  let failing = true;
  await page.route("**/admin/api/settings/instance", (route) =>
    route.fulfill({
      status: failing ? 503 : 200,
      json: failing
        ? {
            error: {
              code: "unavailable",
              message: "Instance could not be saved. Try again.",
            },
          }
        : {
            instance: {
              name: "Recovered",
              base_url: "",
              theme_default: "system",
            },
          },
    }),
  );
  await page.goto("/ui/settings/");
  const section = page.getByRole("region", { name: "Instance", exact: true });
  await expect(
    section.getByRole("button", { name: "Try again" }),
  ).toBeVisible();
  failing = false;
  await section.getByRole("button", { name: "Try again" }).click();
  await section
    .getByRole("button", { name: "Edit Instance", exact: true })
    .click();
  await expect(page.getByLabel("Instance name")).toHaveValue("Recovered");
  failing = true;
  await page.getByLabel("Instance name").fill("Typed but not saved");
  await page
    .getByRole("button", { name: "Save instance", exact: true })
    .click();
  await expect(section.getByRole("alert")).toContainText("Unsaved changes");
  await expect(section.getByRole("alert")).toContainText(
    "Instance could not be saved",
  );
  await expect(page.getByLabel("Instance name")).toHaveValue(
    "Typed but not saved",
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
  await page.goto(`${baseURL}/ui/showcase/`);
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await page.reload();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await context.close();
});

test("built-in default plans remain visible without a database profile row", async ({
  page,
}) => {
  await seed(page, "seeded");
  await page.route("**/admin/api/settings/defaults", (route) =>
    route.fulfill({
      json: { defaults: { profile_id: "default-local" }, aliases: {} },
    }),
  );
  await page.goto("/ui/settings/");
  await editSection(page, "Defaults");
  await expect(page.getByLabel("Default access plan")).toContainText(
    "Default local (built-in)",
  );
});

test("extension enable uses the validated source and waits for explicit code trust", async ({
  page,
}) => {
  await seed(page, "empty");
  await page.route("**/admin/api/extensions/validate", (route) =>
    route.fulfill({
      json: {
        review: {
          ok: true,
          package: { id: "trusted-fixture", version: "1" },
          errors: [],
          warnings: [],
          requires_code_trust: true,
          resolved_source: { resolved_ref: "fixture-commit" },
          object_counts: { services: 1 },
        },
      },
    }),
  );
  await page.route("**/admin/api/extensions/enable", (route) =>
    route.fulfill({ json: {} }),
  );
  await page.goto("/ui/settings/");
  await editSection(page, "Extensions");
  await page.getByRole("button", { name: "Add extension" }).click();
  await page.getByLabel("Directory on the gateway").fill("/extensions/fixture");
  await page.getByRole("button", { name: "Validate package" }).click();
  await expect(
    page.getByRole("button", { name: "Enable extension" }),
  ).toBeDisabled();
  await page
    .getByLabel("I trust this package to run code on the gateway")
    .click();
  const enabled = page.waitForRequest(
    (r) =>
      r.url().endsWith("/admin/api/extensions/enable") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Enable extension" }).click();
  expect((await enabled).postDataJSON()).toEqual({
    source: { type: "local_directory", path: "/extensions/fixture" },
    review: { resolved_source: { resolved_ref: "fixture-commit" } },
  });
  await expect(
    page.getByText("Extension enabled", { exact: true }),
  ).toBeVisible();
});

test("403 settings states do not retry and administrator app defaults stay usable", async ({
  page,
}) => {
  await seed(page, "seeded");
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "admin",
          name: "Admin",
          role: "admin",
          email: "admin@example.test",
        },
      },
    }),
  );
  await page.route("**/admin/api/settings/**", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { code: "owner_required", message: "Owner required" } },
    }),
  );
  await page.route("**/admin/api/operator/sign-in", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { code: "owner_required", message: "Owner required" } },
    }),
  );
  await page.goto("/ui/settings/");
  const instance = page.getByRole("region", { name: "Instance", exact: true });
  await expect(instance).toContainText("Only the owner can change this.");
  await expect(instance.getByRole("button", { name: "Try again" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Save sign-in", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("switch", { name: "Let people create their own apps" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Save app defaults" }),
  ).toBeEnabled();
});

test("independent sign-in and defaults saves retain the other form's drafts", async ({
  page,
}) => {
  await seed(page, "seeded");
  let modeRevision = "7";
  let providerRevision = "7";
  let clientId = "gateway";
  await page.route("**/admin/api/operator/sign-in", (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["if-match"]).toBe(modeRevision);
      modeRevision = "8";
    }
    return route.fulfill({
      json: {
        mode: "both",
        built_in: true,
        oidc: true,
        revision: modeRevision,
      },
    });
  });
  await page.route("**/admin/api/oidc-config", (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["if-match"]).toBe(providerRevision);
      expect(route.request().postDataJSON().client_id).toBe("unsaved-provider");
      clientId = route.request().postDataJSON().client_id;
      providerRevision = "8";
    }
    return route.fulfill({
      json: {
        oidc_config: {
          enabled: true,
          issuer: "https://login.example.test",
          client_id: clientId,
          redirect_uri: "https://gateway.example.test/auth/callback",
        },
        revision: providerRevision,
      },
    });
  });
  await page.goto("/ui/settings/");
  await editSection(page, "Sign-in");
  await page.getByLabel("Client ID", { exact: true }).fill("unsaved-provider");
  await page.getByRole("button", { name: "Save sign-in choice" }).click();
  await expect(
    page.getByText("Sign-in choice saved", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Client ID", { exact: true })).toHaveValue(
    "unsaved-provider",
  );
  await page.getByLabel("Available sign-in methods").click();
  await page
    .getByRole("option", { name: "Built-in accounts only", exact: true })
    .click();
  await page.getByRole("button", { name: "Save sign-in", exact: true }).click();
  await expect(
    page.getByText("Identity provider saved", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Available sign-in methods")).toContainText(
    "Built-in accounts only",
  );
  await page
    .getByText("Provider details and access rules", { exact: true })
    .click();
  await expect(page.getByLabel("Sign-in email exceptions")).toBeVisible();
  await expect(
    page.getByText(/Once any grant exists, this list grants no role/),
  ).toBeVisible();
  await editSection(page, "Defaults");
  await page
    .getByRole("switch", { name: "Let people create their own apps" })
    .click();
  await page.getByRole("button", { name: "Save default plan" }).click();
  await expect(
    page.getByText("Default access plan saved", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("switch", { name: "Let people create their own apps" }),
  ).not.toBeChecked();
  await page.getByLabel("Default access plan").click();
  await page
    .getByRole("option", { name: "No default plan", exact: true })
    .click();
  await page.route("**/admin/api/operator/user-access-defaults", (route) =>
    route.fulfill({
      json: {
        user_access_defaults: {
          self_service_apps: false,
          auto_create_personal_app: true,
        },
      },
    }),
  );
  await page.getByRole("button", { name: "Save app defaults" }).click();
  await expect(
    page.getByText("Personal app defaults saved", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Default access plan")).toContainText(
    "No default plan",
  );
});

// Craft PR 2: a setting row is one line of label and one of detail on a
// phone (64 px), and its detail is one line everywhere. Long text never
// overflows: the name wraps above 640 px; a truncated line carries its full
// text in its title; Edit shows the full values.
for (const width of [390, 1440]) {
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
    await page.goto("/ui/settings/");
    const section = page.getByRole("region", { name: "Instance", exact: true });
    for (const text of [name, url]) {
      const field = section.getByText(text, { exact: text === name });
      await expect(field).toBeVisible();
      expect(
        await field.evaluate(
          (el) => el.getBoundingClientRect().right <= innerWidth,
        ),
      ).toBe(true);
      // Wrapped in full, or cut with an ellipsis that keeps the full text.
      const fits = await field.evaluate(
        (el) => el.scrollWidth <= el.clientWidth,
      );
      if (!fits) {
        await expect(field).toHaveCSS("text-overflow", "ellipsis");
        expect(await field.getAttribute("title")).toContain(text);
      }
      if (text === name && width === 1440) expect(fits).toBe(true);
    }
    await editSection(page, "Instance");
    await expect(section.getByLabel("Instance name")).toHaveValue(name);
    await expect(section.getByLabel("Public base URL")).toHaveValue(url);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("cached owner settings do not expose owner-only forms to administrators", async ({
  page,
}) => {
  await seed(page, "seeded");
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "admin",
          name: "Admin",
          role: "admin",
          email: "admin@example.test",
        },
      },
    }),
  );
  await page.goto("/ui/settings/");
  await editSection(page, "Sign-in");
  await expect(
    page.getByRole("button", { name: "Save sign-in choice", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save sign-in", exact: true }),
  ).toHaveCount(0);
  await editSection(page, "Instance");
  await expect(
    page.getByRole("button", { name: "Save instance", exact: true }),
  ).toHaveCount(0);
  await editSection(page, "Defaults");
  await expect(
    page.getByRole("button", { name: "Save default plan", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save app defaults", exact: true }),
  ).toBeEnabled();
});

const revisionForms = [
  {
    section: "Instance",
    field: "Instance name",
    save: "Save instance",
    get: "/admin/api/settings/instance",
    put: "/admin/api/settings/instance",
    original: "Original",
    draft: "Unsaved name",
    refreshed: "External name",
    saved: "Normalized name",
  },
  {
    section: "Sign-in",
    field: "Client ID",
    save: "Save sign-in",
    get: "/admin/api/oidc-config",
    put: "/admin/api/oidc-config",
    original: "original-client",
    draft: "draft-client",
    refreshed: "external-client",
    saved: "normalized-client",
  },
  {
    section: "Sign-in",
    field: "Available sign-in methods",
    save: "Save sign-in choice",
    get: "/admin/api/operator/sign-in",
    put: "/admin/api/operator/sign-in",
    original: "Built-in accounts and OIDC",
    draft: "Built-in accounts only",
    refreshed: "OIDC only",
    saved: "Built-in accounts and OIDC",
  },
  {
    section: "Media admission",
    field: "Maximum queued jobs",
    save: "Save media admission",
    get: "/admin/api/operator/overrides",
    put: "/admin/api/operator/media-job-admission",
    original: "5",
    draft: "12",
    refreshed: "9",
    saved: "10",
  },
] as const;
type RevisionForm = (typeof revisionForms)[number];
function revisionResponse(
  form: RevisionForm,
  value: string,
  revision: string,
  mutation = false,
) {
  if (form.field === "Instance name")
    return {
      instance: { name: value, base_url: "", theme_default: "system" },
      revision,
    };
  if (form.field === "Client ID")
    return {
      oidc_config: {
        enabled: true,
        issuer: "https://id.example.test",
        client_id: value,
        redirect_uri: "https://gateway.example.test/auth/callback",
      },
      revision,
    };
  if (form.field === "Available sign-in methods")
    return {
      mode:
        value === "Built-in accounts only"
          ? "built-in"
          : value === "OIDC only"
            ? "oidc"
            : "both",
      built_in: value !== "OIDC only",
      oidc: true,
      revision,
    };
  const media = { max_queue_depth: Number(value) };
  return mutation
    ? { media_job_admission: media, revision }
    : {
        overrides: {
          media_job_admission: media,
          revisions: { media_job_admission_overrides: revision },
        },
      };
}
async function changeRevisionField(
  page: Page,
  form: RevisionForm,
  value: string,
) {
  const field = page.getByLabel(form.field, { exact: true });
  if (form.field === "Available sign-in methods") {
    await field.click();
    await page.getByRole("option", { name: value, exact: true }).click();
  } else await field.fill(value);
}
async function expectRevisionField(
  page: Page,
  form: RevisionForm,
  value: string,
) {
  const field = page.getByLabel(form.field, { exact: true });
  if (form.field === "Available sign-in methods")
    await expect(field).toContainText(value);
  else await expect(field).toHaveValue(value);
}
async function refreshSettings(page: Page, path: string) {
  // Exercise React Query's real visibility refresh after its 15-second stale time.
  await page.clock.setFixedTime(
    (await page.evaluate(() => Date.now())) + 16_000,
  );
  const fetched = page.waitForResponse(
    (response) =>
      response.url().endsWith(path) && response.request().method() === "GET",
  );
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
  });
  await fetched;
}
for (const form of revisionForms) {
  test(`${form.save}: dirty refresh conflicts, clean refresh and successful response rebase revisions`, async ({
    page,
  }) => {
    await page.clock.setFixedTime(Date.now());
    await seed(page, "seeded");
    let value: string = form.original;
    let revision = "7";
    let conflict = true;
    const received: string[] = [];
    const routePaths = new Set([form.get, form.put]);
    for (const path of routePaths)
      await page.route(`**${path}`, (route) => {
        if (route.request().method() === "PUT") {
          received.push(route.request().headers()["if-match"]);
          if (conflict)
            return route.fulfill({
              status: 409,
              json: {
                error: { code: "settings_conflict", message: "Conflict" },
              },
            });
          value = form.saved;
          revision = "10";
          return route.fulfill({
            json: revisionResponse(form, value, revision, true),
          });
        }
        return route.fulfill({ json: revisionResponse(form, value, revision) });
      });
    await page.goto("/ui/settings/");
    await editSection(page, form.section);
    await changeRevisionField(page, form, form.draft);
    value = form.refreshed;
    revision = "8";
    await refreshSettings(page, form.get);
    await expectRevisionField(page, form, form.draft);
    await page.getByRole("button", { name: form.save, exact: true }).click();
    await expect(
      page
        .getByRole("region", { name: form.section, exact: true })
        .getByRole("alert"),
    ).toContainText("Settings changed elsewhere; reload to see the latest");
    await expectRevisionField(page, form, form.draft);
    expect(received).toEqual(["7"]);
    await page.reload();
    await editSection(page, form.section);
    await expectRevisionField(page, form, form.refreshed);
    value = form.original;
    revision = "9";
    await refreshSettings(page, form.get);
    await expectRevisionField(page, form, form.original);
    conflict = false;
    await page.getByRole("button", { name: form.save, exact: true }).click();
    await expectRevisionField(page, form, form.saved);
    await page.getByRole("button", { name: form.save, exact: true }).click();
    await expect.poll(() => received).toEqual(["7", "9", "10"]);
  });

  test(`${form.save}: save-time owner denial retains the draft`, async ({
    page,
  }) => {
    await seed(page, "seeded");
    await page.route(`**${form.put}`, (route) =>
      route.request().method() === "PUT"
        ? route.fulfill({
            status: 403,
            json: {
              error: { code: "owner_required", message: "Owner required" },
            },
          })
        : route.fulfill({ json: revisionResponse(form, form.original, "7") }),
    );
    await page.goto("/ui/settings/");
    await editSection(page, form.section);
    await changeRevisionField(page, form, form.draft);
    await page.getByRole("button", { name: form.save, exact: true }).click();
    await expect(
      page
        .getByRole("region", { name: form.section, exact: true })
        .getByRole("alert"),
    ).toContainText("Only the owner can change this.");
    await expectRevisionField(page, form, form.draft);
  });
}
