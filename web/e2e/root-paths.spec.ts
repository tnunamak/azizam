import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

// The app lives at the origin root (gateway/ui_pages.json). Old /ui URLs are
// gone: no redirect, no alias.

test("anonymous front door shows the landing, signs in and returns to Home", async ({
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
    await expect(page).toHaveURL(/\/\?connect=phone$/);
    await expect(
      page.getByText("Your own AI, in the apps you use", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toHaveCount(0);
    await page.getByRole("link", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/sign-in\//);
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/\?connect=phone$/);
    await expect(
      page.getByRole("heading", { name: "Connect an app", level: 2 }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("navigation", { name: "Main navigation" })
        .getByText("Gateway", { exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("an old /ui URL in a browser gets the app's 404 page", async ({
  page,
}) => {
  const response = await page.goto("/ui/home/");
  expect(response?.status()).toBe(404);
  expect(response?.headers()["content-type"]).toContain("text/html");
  await expect(
    page.getByRole("heading", { name: "Page not found" }),
  ).toBeVisible();
  await page.getByRole("main").getByRole("link", { name: "Home", exact: true }).click();
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
});

test("an old /ui URL without an HTML Accept stays a JSON 404", async ({
  request,
}) => {
  const response = await request.get("/ui/home/", {
    headers: { accept: "application/json" },
  });
  expect(response.status()).toBeGreaterThanOrEqual(400);
  expect(response.headers()["content-type"]).not.toContain("text/html");
});

test("a page without its trailing slash keeps its query", async ({ page }) => {
  await page.goto("/engines?service=capture-engine");
  await expect(page).toHaveURL(/\/engines\/\?service=capture-engine$/);
  await expect(
    page.getByRole("heading", { name: "Engines", exact: true }),
  ).toBeVisible();
});

test("local sign-in returns to a receipt deep link", async ({
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
      "/sign-in/?return_to=%2Fusage%2F%3Freceipt%3Dtest-receipt",
    );
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/\/usage\/\?receipt=test-receipt$/);
    await expect(
      page.getByRole("heading", { name: "Usage", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "This receipt isn’t available. It may belong to someone else or be deleted.",
      ),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("Status links to the owner technical dashboard", async ({ page }) => {
  await page.goto("/status/");
  await page.getByRole("button", { name: "Status actions" }).click();
  await page
    .getByRole("menuitem", { name: "Technical details", exact: true })
    .click();
  await expect(page).toHaveURL(/\/status\/raw$/);
  await expect(page.locator("body")).toContainText("Status");
});

test("OIDC sign-in carries one return destination", async ({ page }) => {
  const destination = "/usage/?receipt=oidc-return";
  await page.route("**/auth/api/options?*", async (route) => {
    const url = new URL(route.request().url());
    const returnTo = url.searchParams.get("return_to");
    await fulfilJson(route, {
        built_in: false,
        oidc: true,
        oidc_label: "Test identity provider",
        oidc_url: `/login?return_to=${encodeURIComponent(returnTo || "/")}`,
      });
  });
  await page.goto(`/sign-in/?return_to=${encodeURIComponent(destination)}`);
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
