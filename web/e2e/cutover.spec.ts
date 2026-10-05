import { expect, test } from "@playwright/test";

test("anonymous front door signs in and returns to Home", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const page = await context.newPage();
  try {
    await page.goto("/?connect=phone");
    await expect(page).toHaveURL(/\/ui\/home\/\?connect=phone$/);
    await page.getByRole("link", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/sign-in\//);
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/home\/\?connect=phone$/);
    await expect(
      page.getByRole("heading", { name: "Connect an app", level: 2 }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Hello, there.", exact: true }),
    ).toHaveCount(0);
    await expect(
      page
        .getByRole("navigation", { name: "Main navigation" })
        .getByText("Gateway", { exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("owner admin bookmark opens Home with Gateway navigation", async ({
  page,
}) => {
  await page.goto("/admin?from=bookmark");
  await expect(page).toHaveURL(/\/ui\/home\/\?from=bookmark$/);
  await expect(
    page.getByRole("heading", { name: "Connect an app", level: 2 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Hello, there.", exact: true }),
  ).toHaveCount(0);
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav.getByText("Gateway", { exact: true })).toBeVisible();
  await expect(
    nav.getByRole("link", { name: "Engines", exact: true }),
  ).toBeVisible();
});

test("local sign-in returns to the new page and keeps its filters", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const page = await context.newPage();
  try {
    await page.goto(
      "/ui/sign-in/?return_to=%2Fui%2Fusage%2F%3Freceipt%3Dtest-receipt",
    );
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/usage\/\?receipt=test-receipt$/);
    await expect(
      page.getByRole("heading", { name: "Usage", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("Status links to the owner technical dashboard", async ({ page }) => {
  await page.goto("/ui/status/");
  await page.getByRole("button", { name: "Status actions" }).click();
  await page
    .getByRole("menuitem", { name: "Technical details", exact: true })
    .click();
  await expect(page).toHaveURL(/\/status\/raw$/);
  await expect(page.locator("body")).toContainText("Status");
});

test("OIDC sign-in carries one return destination", async ({ page }) => {
  const destination = "/ui/usage/?receipt=oidc-return";
  await page.route("**/auth/api/options?*", async (route) => {
    const url = new URL(route.request().url());
    const returnTo = url.searchParams.get("return_to");
    await route.fulfill({
      json: {
        built_in: false,
        oidc: true,
        oidc_label: "Test identity provider",
        oidc_url: `/login?return_to=${encodeURIComponent(returnTo || "/ui/home/")}`,
      },
    });
  });
  await page.goto(`/ui/sign-in/?return_to=${encodeURIComponent(destination)}`);
  const link = page.getByRole("link", {
    name: "Continue with Test identity provider",
  });
  await expect(link).toHaveAttribute(
    "href",
    `/login?return_to=${encodeURIComponent(destination)}`,
  );
  const href = new URL((await link.getAttribute("href"))!, "http://localhost");
  expect(href.searchParams.getAll("return_to")).toEqual([destination]);
});

test("anonymous legacy engine bookmark signs in and keeps its selection", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const page = await context.newPage();
  try {
    await page.goto("/admin/services?service=capture-engine");
    await expect(page).toHaveURL(/\/ui\/sign-in\//);
    expect(new URL(page.url()).searchParams.get("return_to")).toBe(
      "/ui/engines/?service=capture-engine",
    );
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/engines\/\?service=capture-engine$/);
    await expect(
      page.getByRole("heading", { name: "Engines", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("legacy Profiles bookmark selects Access plans", async ({ page }) => {
  await page.goto("/admin/profiles?profile=local-chat");
  await expect(page).toHaveURL(/\/ui\/people\/\?/);
  const destination = new URL(page.url());
  expect(destination.searchParams.get("profile")).toBe("local-chat");
  expect(destination.searchParams.get("tab")).toBe("plans");
  await expect(
    page.getByRole("tab", { name: "Access plans", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("button", { name: "New access plan", exact: true }),
  ).toBeVisible();
});
