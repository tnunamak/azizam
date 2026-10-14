import { type Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

// GET /admin/api/signin: a live provider ("seeded") or none yet ("empty").
function signInSnapshot(state: "seeded" | "empty" | "error") {
  const live = state !== "empty";
  return {
    revision: "rev-1",
    viewer: { role: "owner", can_edit: true },
    methods: { password: true, provider: live },
    provider: {
      name: live ? "Example identity" : "",
      address: live ? "https://login.example.test/realms/home" : "",
      client_id: live ? "gateway" : "",
      scopes: live ? "openid email profile" : "",
      groups_claim: live ? "groups" : "",
      authorization_endpoint: "",
      token_endpoint: "",
      jwks_uri: "",
      clock_skew_seconds: 60,
      client_secret_saved: live,
      host: live ? "login.example.test" : "",
      effective_name: live ? "Example identity" : "",
      redirect_uri: "https://gateway.example.test/admin/oidc/callback",
      tested_at: live ? 1_800_000_000 : null,
      groups_missing: false,
    },
    rules: {
      fallback: "closed",
      rules: live
        ? [
            {
              id: "rule-trusted",
              field: "group",
              values: ["/Trusted"],
              admit: true,
              admin: true,
              plan: null,
            },
          ]
        : [],
    },
    rule_matches: live ? { "rule-trusted": { count: 1, names: ["Casey"] } } : {},
    seen: {
      group: live ? [{ value: "/Trusted", count: 1 }] : [],
      role: [],
      email: [],
      domain: [],
    },
    plans: [{ id: "local", name: "Local chat" }],
    owner: { name: "Casey", email: "casey@example.test", has_password: true },
    recovery_command: "./venv/bin/python proxy.py generate-admin-key owner-cli",
    notice: null,
  };
}

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
                defaults: { invite_access_plan: state === "empty" ? null : "local" },
                aliases:
                  state === "empty"
                    ? {}
                    : { chat: { stable: { fast: "local-chat" } } },
              }
            : {
                config: {
                  proxy: { port: 18000 },
                  secret: "<redacted>",
                  example_endpoint: `https://gateway.example.test/${"recorded-path/".repeat(25)}`,
                },
                export_url: "/admin/api/settings/effective/export",
              };
    return fulfilJson(route, body, { status: state === "error" ? 503 : 200 });
  });
  await page.route("**/admin/api/signin", (route) =>
    fulfilJson(route, signInSnapshot(state), {
      status: state === "error" ? 503 : 200,
    }),
  );
  await page.route("**/admin/api/signin/preview", (route) =>
    fulfilJson(route, {
      people: [],
      unaffected: [],
      issuer_change: null,
      settings: [],
      blockers: [],
      test_verdict: null,
    }),
  );
  await page.route("**/admin/api/operator/overrides", (route) =>
    fulfilJson(
      route,
      {
        overrides: {
          revisions: { media_job_admission_overrides: "7" },
          media_job_admission:
            state === "empty"
              ? {}
              : { max_queue_depth: 5, retry_after_seconds: 30 },
        },
      },
      { status: state === "error" ? 503 : 200 },
    ),
  );
  await page.route("**/admin/api/extensions", (route) =>
    fulfilJson(
      route,
      {
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
      { status: state === "error" ? 503 : 200 },
    ),
  );
  await page.route("**/admin/api/access-plans", (route) =>
    fulfilJson(route, {
      access_plans: [
        {
          id: "local",
          name: "Local only",
          created_at: 1710000000,
          updated_at: 1710000000,
          plan: {
            id: "local",
            name: "Local only",
            can_use: { chat: { items: "all" } },
            direct_engine_access: { engines: [], operations: [] },
            paid_providers: [],
            provider_fallback: false,
            caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
            can_load_models: true,
            receipt_detail: "basic",
            media_metadata: "none",
          },
          summary: "Fixture local access",
          used_by: { people: [], apps: [], invites: 0 },
        },
      ],
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
          "People and access",
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
            page
              .getByRole("region", { name: "Receipts", exact: true })
              .getByText("Couldn’t load receipt settings"),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toHaveCount(8);
          for (const name of ["People and access"]) {
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
          const signIn = page.getByRole("region", { name: "Sign-in", exact: true });
          await expect(
            signIn.getByRole("heading", { name: "Ways to sign in" }),
          ).toBeVisible();
          if (state === "empty")
            await expect(signIn.getByText("Not set up", { exact: true })).toBeVisible();
          else
            await expect(
              signIn.getByRole("switch", { name: "Example identity" }),
            ).toBeChecked();
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
          }
        }
        await expect
          .poll(() =>
            page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
          )
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

// Owner-global state: instance, media admission, the access plan for new
// invitations and the sign-in settings. The fixture restores them after the test.
test(
  "settings saves each section through its own API",
  { tag: GLOBAL_STATE_TAG },
  async ({ page }) => {
    await settingsJourney(page);
  },
);

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
  const access = page.getByRole("region", {
    name: "People and access",
    exact: true,
  });
  await expect(access).toBeVisible();
  await page.getByLabel("Access plan for new invitations").click();
  await page.getByRole("option", { name: "Local chat", exact: true }).click();
  const planSaved = page.waitForResponse(
    (r) =>
      r.url().endsWith("/admin/api/settings/defaults") &&
      r.request().method() === "PUT",
  );
  await access.getByRole("button", { name: "Save", exact: true }).click();
  expect((await planSaved).status()).toBe(200);
  await expect(access.getByRole("status")).toHaveText(
    "Access plan for new invitations saved",
  );
  await page.reload();
  await expect(page.getByLabel("Access plan for new invitations")).toContainText(
    "Local chat",
  );
  // Sign-in is on the page, not behind an Edit button. This journey reads it;
  // the writes are covered by the component specs against the same contract.
  const signIn = page.getByRole("region", { name: "Sign-in", exact: true });
  await expect(
    signIn.getByRole("heading", { name: "Ways to sign in" }),
  ).toBeVisible();
  await expect(
    signIn.getByRole("switch", { name: "Email and password" }),
  ).toBeChecked();
  await expect(signIn.getByLabel("Client secret", { exact: true })).toHaveCount(0);
  await editSection(page, "Advanced");
  await page
    .getByText("Effective configuration and export", { exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Export redacted snapshot" }),
  ).toBeVisible();
  await expect(page.locator("main")).not.toContainText(
    "browser-fixture-secret",
  );
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export redacted snapshot" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("gateway-effective-config.json");
}

test("new visitors use the saved default theme and personal choices persist", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({ colorScheme: "light" });
  const page = await context.newPage();
  await page.route("**/.well-known/azizam", (route) =>
    fulfilJson(route, {
      service: { name: "Our gateway" },
      presentation: { theme_default: "dark" },
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
for (const width of [390, 1440]) {
  test(`long instance names and public URLs stay inside the page at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await seed(page, "seeded");
    const name = "n".repeat(300);
    const url = `https://example.test/${"path".repeat(100)}`;
    await page.route("**/admin/api/settings/instance", (route) =>
      fulfilJson(route, {
        instance: { name, base_url: url, theme_default: "system" },
      }),
    );
    await page.goto("/settings/");
    const section = page.getByRole("region", { name: "Instance", exact: true });
    for (const text of [name, url]) {
      const field = section.getByText(text, { exact: text === name });
      await expect(field).toBeVisible();
      await expect
        .poll(() =>
          field.evaluate(
            (el) => el.getBoundingClientRect().right <= innerWidth,
          ),
        )
        .toBe(true);
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
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
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
        name: "People and access",
        exact: true,
      });
      const plan = page.getByLabel("Access plan for new invitations");
      await expect(plan).toContainText("Ask each time");
      await expect(
        defaults.getByText(/Nobody has a plan until you choose one/),
      ).toBeVisible();
      await defaults.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `${process.env.UI_DEFAULTS_SHOTS_DIR ?? "test-results/screens"}/after-${width}-${theme}.png`,
        fullPage: true,
      });
      await plan.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("option", { name: "Ask each time", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("End");
      await expect(
        page.getByRole("option", { name: "Local only", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(plan).toContainText("Local only");
      await expect(plan).toBeFocused();
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
            .getByRole("option", { name: "Ask each time", exact: true })
            .click();
          await expect(plan).toContainText("Ask each time");
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
          .poll(
            () =>
              page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth,
              ),
            { message: JSON.stringify(overflow) },
          )
          .toBe(true);
        for (const section of [defaults]) {
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
        .include('[aria-labelledby="people-and-access"]')
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
