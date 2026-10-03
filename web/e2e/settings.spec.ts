import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function seed(page: Page, state: "seeded" | "empty" | "error") {
  await page.route("**/admin/api/settings/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.includes("instance")
      ? {
          instance: {
            name: "Our gateway",
            base_url: "https://gateway.example.test",
            theme_default: "system",
          },
        }
      : path.includes("defaults")
        ? {
            defaults: { profile_id: state === "empty" ? null : "local" },
            aliases: state === "empty" ? {} : { chat: "local-chat" },
          }
        : {
            config: { proxy: { port: 18000 }, secret: "<redacted>" },
            export_url: "/admin/api/settings/effective/export",
          };
    return route.fulfill({ status: state === "error" ? 503 : 200, json: body });
  });
  await page.route("**/admin/api/oidc-config", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        oidc_config:
          state === "empty"
            ? null
            : {
                enabled: false,
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
  await page.route("**/admin/api/operator/overrides", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json: {
        overrides: {
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
          "Media admission",
          "Extensions",
          "Advanced",
        ]) {
          await expect(
            page.getByRole("heading", { name, exact: true }),
          ).toBeVisible();
        }
        await page
          .getByText("Effective configuration and export", { exact: true })
          .click();
        if (state === "error") {
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toHaveCount(6);
        } else {
          await expect(page.getByLabel("Instance name")).toHaveValue(
            "Our gateway",
          );
          await expect(
            page.getByText("OIDC only", { exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("link", { name: "Export redacted snapshot" }),
          ).toBeVisible();
          if (state === "empty") {
            await expect(
              page.getByText("No extensions installed"),
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
  await page.goto("/ui/settings/");
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
  await expect(page.getByLabel("Instance name")).toHaveValue(
    "Browser settings journey",
  );
  await page.getByLabel("Maximum queued jobs").fill("7");
  const mediaSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/operator/media-job-admission") &&
      r.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "Save media admission" }).click();
  expect((await mediaSaved).status()).toBe(200);
  await page.reload();
  await expect(page.getByLabel("Maximum queued jobs")).toHaveValue("7");
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
  await expect(page.getByLabel("Default access plan")).toContainText(
    "Local chat",
  );
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
  await expect(page.getByLabel("Instance name")).toHaveValue("Recovered");
  failing = true;
  await page
    .getByRole("button", { name: "Save instance", exact: true })
    .click();
  await expect(section.getByRole("alert")).toContainText(
    "Instance could not be saved",
  );
});
