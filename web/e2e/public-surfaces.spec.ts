import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const sections = {
  sections: [
    { id: "chat", title: "Chat and vision", source: "aliases:chat" },
    { id: "speech", title: "Speech", source: "aliases:speech" },
    {
      id: "images",
      title: "Image generation",
      match: { intent: "image.generate" },
    },
    { id: "more", title: "More workflows", fallback: true },
  ],
};

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      for (const surface of ["setup", "sign-in", "showcase"] as const) {
        test(`${surface}: ${state}, ${theme}, ${width}px`, async ({ page }) => {
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
          if (surface === "setup") {
            await page.route("**/setup/api/state", (route) =>
              route.fulfill({
                status: state === "error" ? 503 : 200,
                contentType: "application/json",
                body: JSON.stringify({
                  owner_exists: state === "seeded",
                  oidc_enabled: false,
                  can_recover: true,
                }),
              }),
            );
          } else if (surface === "sign-in") {
            await page.route("**/auth/api/options*", (route) =>
              route.fulfill({
                status: state === "error" ? 503 : 200,
                json:
                  state === "error"
                    ? { error: { message: "Sign-in options are unavailable." } }
                    : {
                        built_in: state === "seeded",
                        oidc: false,
                        oidc_label: "",
                        oidc_url: "/admin/login?return_to=/ui/",
                        instance_name: "Browser test gateway",
                        owner_name: "Gateway owner",
                      },
              }),
            );
          } else {
            await page.route("**/showcase/static/sections.json", (route) =>
              route.fulfill({ json: sections }),
            );
            await page.route("**/showcase/examples/index.json", (route) =>
              route.fulfill({
                status: state === "error" ? 503 : 200,
                contentType: "application/json",
                body: JSON.stringify({
                  data:
                    state === "empty"
                      ? []
                      : [
                          {
                            id: "chat-recorded",
                            card: "chat",
                            title: "A recorded answer",
                            model: "local-chat",
                            seconds: 1.2,
                            inputs: [{ value: "Why is the sky blue?" }],
                            outputs: [
                              {
                                kind: "text",
                                text: "Air scatters blue light.",
                              },
                            ],
                          },
                        ],
                }),
              }),
            );
          }
          await page.goto(`/ui/${surface}/`);
          await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
          await expect(
            page.getByRole("navigation", { name: "Main navigation" }),
          ).toHaveCount(0);
          if (state === "error") {
            await expect(
              page.getByRole("button", { name: "Try again" }),
            ).toBeVisible();
          } else if (surface === "setup") {
            await expect(page.getByLabel("Email or username")).toBeVisible();
            await expect(
              page.getByRole("button", {
                name: state === "empty" ? "Create owner account" : "Sign in",
                exact: true,
              }),
            ).toBeVisible();
            await page
              .getByText("Recovery and advanced setup", { exact: true })
              .click();
            await expect(page.getByLabel("Recovery admin key")).toBeVisible();
            await expect(page.locator("form").first()).toHaveAttribute(
              "action",
              state === "empty" ? "/setup/local-owner" : "/setup/local-login",
            );
          } else if (surface === "sign-in") {
            if (state === "empty") {
              await expect(
                page.getByText("Sign-in is not configured", { exact: true }),
              ).toBeVisible();
              await expect(page.locator("form")).toHaveCount(0);
            } else {
              await expect(
                page.getByLabel("Email", { exact: true }),
              ).toBeVisible();
              await expect(
                page.getByLabel("Password", { exact: true }),
              ).toHaveAttribute("autocomplete", "current-password");
              await expect(
                page.getByRole("button", { name: "Sign in", exact: true }),
              ).toBeVisible();
            }
            await expect(
              page.getByText("Recovery and advanced setup", { exact: true }),
            ).toHaveCount(0);
          } else if (state === "empty") {
            await expect(
              page.getByText(/No recorded examples yet/),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", { name: "Speech", exact: true }),
            ).toHaveCount(0);
          } else {
            await expect(
              page.getByText("Air scatters blue light."),
            ).toBeVisible();
            await expect(
              page.getByRole("heading", { name: "Speech", exact: true }),
            ).toHaveCount(0);
            await expect(
              page.getByRole("heading", {
                name: "Image generation",
                exact: true,
              }),
            ).toHaveCount(0);
          }
          expect(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
          ).toBe(true);
          const result = await new AxeBuilder({ page }).analyze();
          expect(
            result.violations.filter((v) =>
              ["serious", "critical"].includes(v.impact ?? ""),
            ),
          ).toEqual([]);
          await page.screenshot({
            path: `test-results/screens/public-${surface}-${state}-${theme}-${width}.png`,
            fullPage: true,
          });
        });
      }
    }
  }
}

test("setup uses configured single sign-on without a password form", async ({
  page,
}) => {
  await page.route("**/setup/api/state", (route) =>
    route.fulfill({
      json: { owner_exists: false, oidc_enabled: true, can_recover: false },
    }),
  );
  await page.goto("/ui/setup/");
  await expect(
    page.getByRole("link", { name: "Continue with single sign-on" }),
  ).toHaveAttribute("href", "/login?return_to=/setup");
  await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0);
});

test("built-in sign-in posts JSON and retains its fields after denied credentials", async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await page.route("**/auth/api/options*", (route) =>
    route.fulfill({
      json: {
        built_in: true,
        oidc: false,
        oidc_label: "",
        oidc_url: "/admin/login?return_to=/ui/",
        instance_name: "Browser test gateway",
        owner_name: "Gateway owner",
      },
    }),
  );
  await page.goto("/ui/sign-in/");
  await page.getByLabel("Email", { exact: true }).fill("unknown@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill("Invalid browser test password");
  const posted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/auth/api/login") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await posted;
  expect(response.status()).toBe(401);
  expect(response.request().postDataJSON()).toEqual({
    email: "unknown@example.test",
    password: "Invalid browser test password",
    return_to: "/ui/home/",
  });
  await expect(page).toHaveURL(/\/ui\/sign-in\/$/);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Email or password is incorrect." }),
  ).toBeVisible();
  await expect(page.getByLabel("Email", { exact: true })).toHaveValue(
    "unknown@example.test",
  );
  await expect(page.getByLabel("Password", { exact: true })).toHaveValue(
    "Invalid browser test password",
  );
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeEnabled();
});

test("expired invite links ask for a new invitation without retry", async ({
  page,
}) => {
  await page.route("**/auth/api/options*", (route) =>
    route.fulfill({
      json: {
        built_in: true,
        oidc: false,
        oidc_label: "",
        oidc_url: "/admin/login?return_to=/ui/",
        instance_name: "Browser test gateway",
        owner_name: "Gateway owner",
      },
    }),
  );
  await page.route("**/auth/api/invite?**", (route) =>
    route.fulfill({
      status: 404,
      json: {
        error: {
          code: "link_unavailable",
          message: "This link has expired or is no longer available.",
        },
      },
    }),
  );
  await page.goto("/ui/invite/#token=expired-browser-token");
  const alert = page
    .getByRole("alert")
    .filter({ hasText: "Couldn’t use this invitation" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText(
    "This link can no longer be used. Ask the gateway owner for a new link.",
  );
  await expect(page.getByRole("button", { name: /try again/i })).toHaveCount(
    0,
  );
});

for (const theme of ["light", "dark"] as const) {
  test(`native recovery failure fits 390px in ${theme}`, async ({
    page,
    context,
  }) => {
    await context.clearCookies();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await page.addInitScript(
      (value) => localStorage.setItem("theme", value),
      theme,
    );
    await page.route("**/setup/api/state", (route) =>
      route.fulfill({
        json: { owner_exists: true, oidc_enabled: false, can_recover: true },
      }),
    );
    await page.goto("/ui/setup/");
    await page
      .getByText("Recovery and advanced setup", { exact: true })
      .click();
    await page
      .getByLabel("Recovery admin key")
      .fill("invalid-browser-recovery-key");
    await page
      .getByRole("button", { name: "Continue with recovery key" })
      .click();
    await expect(page).toHaveURL(/\/setup\/claim$/);
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.locator("details.recovery")).toHaveAttribute("open", "");
    await expect(page.locator("html")).toHaveClass(
      theme === "dark" ? /setup-public.*dark/ : /^setup-public$/,
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/screens/public-native-recovery-${theme}-390.png`,
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

test("Gallery retry recovers from a failed recorded-example request", async ({
  page,
}) => {
  let unavailable = true;
  await page.route("**/showcase/static/sections.json", (route) =>
    route.fulfill({ json: sections }),
  );
  await page.route("**/showcase/examples/index.json", (route) =>
    route.fulfill({ status: unavailable ? 503 : 200, json: { data: [] } }),
  );
  await page.goto("/ui/showcase/");
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  unavailable = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText(/No recorded examples yet/)).toBeVisible();
});
