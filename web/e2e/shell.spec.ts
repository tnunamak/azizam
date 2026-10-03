import { test, expect } from "@playwright/test";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

const ownerPages = ["Engines", "Providers", "People", "Activity", "Settings"];

test("You navigation keeps Workflows between Models and Try", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/ui/");
  const navigation = page.getByRole("navigation", { name: "Main navigation" });
  await expect(navigation.getByRole("link")).toHaveText([
    "Home",
    "Apps & keys",
    "Models",
    "Workflows",
    "Try",
    "Library",
    "Usage",
    "Status",
    ...ownerPages,
  ]);
  await navigation
    .getByRole("link", { name: "Workflows", exact: true })
    .click();
  await expect(page).toHaveURL(/\/ui\/workflows\/?$/);
  await expect(
    page.getByRole("heading", { name: "Workflows", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Control+k");
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill("Workflows");
  await expect(
    page.getByRole("option", { name: "Workflows", exact: true }),
  ).toBeVisible();
});

test("command palette navigates and runs three real actions", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/ui/");
  await page.keyboard.press("Control+k");
  await expect(
    page.getByRole("dialog", { name: "Search your gateway" }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill("Models");
  await page.getByRole("option", { name: "Models", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/models\/?$/);
  // Both desktop shortcuts are supported, independent of the host platform.
  await page.keyboard.press("Meta+k");
  const discovery = page.waitForResponse((response) =>
    response.url().endsWith("/.well-known/ai-gateway"),
  );
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill("Copy base URL");
  await page
    .getByRole("option", { name: "Copy base URL", exact: true })
    .click();
  const snapshot = await (await discovery).json();
  await expect(
    page.getByRole("dialog", { name: "Search your gateway" }),
  ).not.toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe(snapshot.recipes.openai.canonical_base_url);
  await page.keyboard.press("Control+k");
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill("Check gateway connection");
  const health = page.waitForResponse((response) =>
    response.url().endsWith("/health"),
  );
  await page.getByRole("option", { name: "Check gateway connection" }).click();
  expect((await health).ok()).toBe(true);
  await expect(
    page.getByText(
      "The gateway is reachable. Engine requests are tested on Try.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.keyboard.press("Control+k");
  await page
    .getByRole("combobox", { name: "Search your gateway" })
    .fill("Create key");
  await page.getByRole("option", { name: "Create key", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/apps\/\?action=create-key/);
  await expect(
    page.getByRole("dialog", { name: "New app", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("App name", { exact: true })
    .fill("Command palette app");
  const key = page.waitForResponse(
    (response) =>
      response.url().includes("/me/api/apps/") &&
      response.url().endsWith("/keys") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create app", exact: true }).click();
  expect((await key).status()).toBe(201);
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(
    page.getByRole("heading", { name: "Command palette app", exact: true }),
  ).toBeVisible();
});

test("theme toggle persists after navigation and reload", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/ui/");
  await expect(page.locator("html")).toHaveClass(/light/);
  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.getByRole("link", { name: "Models", exact: true }).first().click();
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
});

test("phone More and tablet expansion expose every destination", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/");
  await page.getByRole("button", { name: "More", exact: true }).click();
  let sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  for (const name of [
    "Home",
    "Models",
    "Workflows",
    "Try",
    "Library",
    "Usage",
    "Apps & keys",
    ...ownerPages,
  ]) {
    await expect(sheet.getByRole("link", { name, exact: true })).toBeVisible();
  }
  await sheet.getByRole("link", { name: "Activity", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/activity\/?$/);
  await expect(sheet).not.toBeVisible();
  await page.setViewportSize({ width: 768, height: 1000 });
  await page.getByRole("button", { name: "Expand navigation" }).click();
  sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(
    sheet.getByRole("link", { name: "Settings", exact: true }),
  ).toBeVisible();
  await sheet.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/settings\/?$/);
});

test("member shell hides owner controls and makes no owner API calls", async ({
  page,
}) => {
  // Built-in member invitation APIs are not present in this foundation lane.
  // Keep the member identity consistent across session and Home snapshots.
  // Owner APIs remain real so any unexpected request fails this assertion.
  const member = {
    id: "member-fixture",
    name: "Test member",
    email: "member@example.test",
    role: "member",
  };
  const ownerCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/"))
      ownerCalls.push(request.url());
  });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "member-fixture",
        user: member,
        apps: [],
      },
    }),
  );
  await page.route("**/me/api/home", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        user: member,
        apps: [],
        receipts: [],
        capabilities: [],
        aliases: [],
        credential_aliases: {},
        new_app_aliases: [],
      },
    });
  });
  await page.goto("/ui/");
  await expect(
    page.getByRole("button", { name: "Account menu" }),
  ).toContainText("Test member");
  for (const name of ownerPages)
    await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  await page.keyboard.press("Control+k");
  await expect(
    page.getByRole("dialog", { name: "Search your gateway" }),
  ).toBeVisible();
  for (const name of ownerPages)
    await expect(page.getByRole("option", { name, exact: true })).toHaveCount(
      0,
    );
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "More", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(
    sheet.getByRole("link", { name: "Workflows", exact: true }),
  ).toBeVisible();
  await expect(
    sheet.getByRole("link", { name: "Try", exact: true }),
  ).toBeVisible();
  for (const name of ownerPages)
    await expect(sheet.getByRole("link", { name, exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.goto("/ui/status/");
  await expect(
    page.getByRole("heading", { name: "Status", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No models are available to your account yet", {
      exact: true,
    }),
  ).toBeVisible();
  expect(ownerCalls).toEqual([]);
});

for (const width of [1024, 768]) {
  test(`sidebar footer remains reachable at ${width}x768`, async ({ page }) => {
    await page.setViewportSize({ width, height: 768 });
    await page.goto("/ui/");
    await expect(
      page.getByRole("link", { name: "Settings", exact: true }),
    ).toBeVisible();
    const toggle = page.getByRole("button", { name: "Toggle theme" });
    await toggle.click();
    await page.getByRole("button", { name: "Account menu" }).click();
    await expect(
      page.getByRole("menuitem", { name: "Sign out" }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    const health = page.getByRole("button", { name: /Gateway status:/ });
    await health.click();
    await expect(page.getByLabel("Gateway health")).toBeVisible();
    await page
      .getByLabel("Gateway health")
      .getByRole("link", { name: "View status" })
      .click();
    await expect(page).toHaveURL(/\/ui\/status\/?$/);
  });
}

test("session failure explains the problem and retry restores the account", async ({
  page,
}) => {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Unavailable for this test" } },
    }),
  );
  await page.goto("/ui/");
  await expect(
    page.getByText(
      "Your account could not be loaded. Check your connection and try again.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Settings", exact: true }),
  ).toHaveCount(0);
  await page.unroute("**/me/api/session");
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Settings", exact: true }),
  ).toBeVisible();
});

test("phone header navigation has 44px touch targets", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/");
  await expect(
    page.getByRole("heading", { name: "Hello, there.", exact: true }),
  ).toBeVisible();
  const header = page.locator("header").first();
  const navigation = page.getByRole("navigation", { name: "Phone navigation" });
  await expect(navigation.getByRole("link")).toHaveText([
    "Home",
    "Apps & keys",
    "Status",
  ]);
  for (const target of [
    header.getByRole("link", { name: "Vivid Fish", exact: true }),
    header.getByRole("button", { name: /Gateway status:/ }),
    header.getByRole("button", { name: "Account menu" }),
  ]) {
    const box = await target.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
});
