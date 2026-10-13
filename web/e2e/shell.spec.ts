import { type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

const ownerPages = [
  "Engines",
  "Providers",
  "People",
  "Categories",
  "Activity",
  "Settings",
];

// DESIGN.md "Shell": You is ordered by how often a household member needs
// each place (Home, Library, Try first); Workflows stays right after Models.
test("You navigation keeps Workflows right after Models", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  const navigation = page.getByRole("navigation", { name: "Main navigation" });
  await expect(navigation.getByRole("link")).toHaveText([
    "Home",
    "Library",
    "Try",
    "Apps & keys",
    "Connect",
    "Models",
    "Workflows",
    "Usage",
    "Status",
    ...ownerPages,
  ]);
  await navigation
    .getByRole("link", { name: "Workflows", exact: true })
    .click();
  await expect(page).toHaveURL(/\/workflows\/?$/);
  await expect(
    page.getByRole("heading", { level: 1, name: /^Workflows( \d+)?$/ }),
  ).toBeVisible();
  await page.keyboard.press("Control+k");
  await page.getByRole("combobox", { name: "Go to" }).fill("Workflows");
  await expect(
    page.getByRole("option", { name: "Workflows", exact: true }),
  ).toBeVisible();
});

test(
  "command palette navigates and runs three real actions",
  { tag: GLOBAL_STATE_TAG },
  async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/");
    await page.keyboard.press("Control+k");
    await expect(page.getByRole("dialog", { name: "Go to" })).toBeVisible();
    await page.getByRole("combobox", { name: "Go to" }).fill("Models");
    await page.getByRole("option", { name: "Models", exact: true }).click();
    await expect(page).toHaveURL(/\/models\/?$/);
    // Both desktop shortcuts are supported, independent of the host platform.
    await page.keyboard.press("Meta+k");
    const discovery = page.waitForResponse((response) =>
      response.url().endsWith("/.well-known/azizam"),
    );
    await page
      .getByRole("combobox", { name: "Go to" })
      .fill("Copy gateway address");
    await page
      .getByRole("option", { name: "Copy gateway address", exact: true })
      .click();
    const snapshot = await (await discovery).json();
    await expect(page.getByRole("dialog", { name: "Go to" })).not.toBeVisible();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toBe(snapshot.recipes.openai.canonical_base_url);
    await page.keyboard.press("Control+k");
    await page
      .getByRole("combobox", { name: "Go to" })
      .fill("Check gateway reachability");
    const health = page.waitForResponse((response) =>
      response.url().endsWith("/health"),
    );
    await page
      .getByRole("option", { name: "Check gateway reachability" })
      .click();
    expect((await health).ok()).toBe(true);
    await expect(
      page.getByText(
        "The gateway is reachable. Engine requests are tested on Try.",
        { exact: true },
      ),
    ).toBeVisible();
    await page.keyboard.press("Control+k");
    await page.getByRole("combobox", { name: "Go to" }).fill("Create key");
    await page.getByRole("option", { name: "Create key", exact: true }).click();
    await expect(page).toHaveURL(/\/apps\/\?action=create-key/);
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
    await page
      .getByRole("button", { name: "Create app and key", exact: true })
      .click();
    expect((await key).status()).toBe(201);
    await page.getByRole("button", { name: "I’ve saved it" }).click();
    await expect(
      page.getByRole("heading", { name: "Command palette app", exact: true }),
    ).toBeVisible();
  },
);

test("command clipboard denial preserves and focuses the exact gateway address", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => {
          throw new DOMException("Denied", "NotAllowedError");
        },
      },
    });
  });
  const address = "https://gateway.example/openai/v1?exact=1";
  await page.route("**/.well-known/azizam", (route) =>
    fulfilJson(route, { recipes: { openai: { canonical_base_url: address } } }),
  );
  await page.goto("/models/");
  const trigger = page.getByRole("button", {
    name: "Go to",
    exact: true,
  });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Go to" });
  await dialog.getByRole("combobox").fill("Copy gateway address");
  await dialog
    .getByRole("option", { name: "Copy gateway address", exact: true })
    .click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toContainText(
    "Copy failed. Select the gateway address and copy it manually.",
  );
  const field = dialog.getByRole("textbox", {
    name: "Gateway address",
    exact: true,
  });
  await expect(field).toHaveValue(address);
  await expect(field).toBeFocused();
  await expect
    .poll(() =>
      field.evaluate((element: HTMLInputElement) =>
        element.value.slice(element.selectionStart!, element.selectionEnd!),
      ),
    )
    .toBe(address);
  // Pending command groups ("Loading apps…") are status regions of their own.
  await expect(
    dialog.getByTestId("gateway-address-recovery").getByRole("status"),
  ).toBeEmpty();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
});

test("command discovery failure offers retry before attempting clipboard writes", async ({
  page,
}) => {
  await page.goto("/models/");
  let clipboardWrites = 0;
  await page.exposeFunction("recordClipboardWrite", () => {
    clipboardWrites += 1;
  });
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => {
          await (
            window as unknown as { recordClipboardWrite: () => Promise<void> }
          ).recordClipboardWrite();
        },
      },
    });
  });
  await page.route("**/.well-known/azizam", (route) =>
    fulfilJson(
      route,
      { error: { message: "fixture unavailable" } },
      { status: 503 },
    ),
  );
  await page.keyboard.press("Control+k");
  const dialog = page.getByRole("dialog", { name: "Go to" });
  await dialog.getByRole("combobox").fill("Copy gateway address");
  await dialog
    .getByRole("option", { name: "Copy gateway address", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Couldn’t load the gateway address",
  );
  await expect(
    dialog.getByRole("textbox", { name: "Gateway address", exact: true }),
  ).toHaveCount(0);
  expect(clipboardWrites).toBe(0);
  await page.unroute("**/.well-known/azizam");
  await dialog.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(clipboardWrites).toBe(1);
});

test("theme toggle persists after navigation and reload", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
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
  await page.goto("/");
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
  await expect(page).toHaveURL(/\/activity\/?$/);
  await expect(sheet).not.toBeVisible();
  await page.setViewportSize({ width: 768, height: 1000 });
  await page.getByRole("button", { name: "Expand navigation" }).click();
  sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(
    sheet.getByRole("link", { name: "Settings", exact: true }),
  ).toBeVisible();
  await sheet.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/?$/);
});

test("member shell hides owner controls and makes no owner API calls", async ({
  page,
}) => {
  // Keep member snapshots consistent across Home and Status navigation.
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
    fulfilJson(route, {
      authenticated: true,
      csrf_token: "member-fixture",
      user: member,
      apps: [],
    }),
  );
  await page.route("**/me/api/home", async (route) => {
    const response = await route.fetch();
    await fulfilJson(
      route,
      {
        ...(await response.json()),
        user: member,
        apps: [],
        receipts: [],
        capabilities: [],
        aliases: [],
        credential_aliases: {},
        new_app_aliases: [],
      },
      { response },
    );
  });
  await page.route("**/me/api/status", (route) =>
    fulfilJson(route, {
      scope: "member",
      status: {
        state: "unavailable",
        reason: "No service has been measured as ready yet.",
      },
      capabilities: [],
      receipts: [],
      help: "Ask the owner if an allowed capability is unavailable.",
    }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Account menu" }),
  ).toContainText("Test member");
  for (const name of ownerPages)
    await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  await page.keyboard.press("Control+k");
  await expect(page.getByRole("dialog", { name: "Go to" })).toBeVisible();
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
  await page.goto("/status/");
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
    await page.goto("/");
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
      .getByRole("link", { name: /^(View status|See why)$/ })
      .click();
    await expect(page).toHaveURL(/\/status\/?$/);
  });
}

test("session failure explains the problem and retry restores the account", async ({
  page,
}) => {
  await page.route("**/me/api/session", (route) =>
    fulfilJson(
      route,
      { error: { message: "Unavailable for this test" } },
      { status: 503 },
    ),
  );
  await page.goto("/");
  await expect(
    page.getByText(
      "We couldn't tell why your account didn't load. Try again.",
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
  // Library: Home and Status carry their own verdict instead of the pill.
  await page.goto("/library/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const header = page.locator("header").first();
  const navigation = page.getByRole("navigation", { name: "Phone navigation" });
  // DESIGN.md "Shell": four labelled tabs, the fourth (More) a button that
  // opens every destination.
  await expect(navigation.getByRole("link")).toHaveText([
    "Home",
    "Library",
    "Apps",
  ]);
  const more = navigation.getByRole("button", { name: "More" });
  await expect(more).toBeVisible();
  expect((await more.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  for (const target of [
    header.getByRole("link", { name: /Azizam.*Home/ }),
    header.getByRole("button", { name: /Gateway status:/ }),
    header.getByRole("button", { name: "Account menu" }),
  ]) {
    const box = await target.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
});

// The ordinary axe pass skips experimental rules. A control whose accessible
// name leaves out its visible words breaks voice control (WCAG 2.5.3).
async function seriousAxeViolations(page: Page) {
  const wcag = await new AxeBuilder({ page }).analyze();
  const labelInName = await new AxeBuilder({ page })
    .withRules(["label-content-name-mismatch"])
    .analyze();
  return [
    ...wcag.violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    ),
    ...labelInName.violations,
  ];
}

test("tablet rail returns focus to its opener when it closes", async ({
  page,
}) => {
  // Motion on: the sheet leaves with an exit animation, which is when focus can get lost.
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 768, height: 1000 });
  await page.goto("/");
  const expand = page.getByRole("button", { name: "Expand navigation" });
  await expect.poll(() => seriousAxeViolations(page)).toEqual([]);

  await expand.click();
  const sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).not.toBeVisible();
  await expect(expand).toBeFocused();

  await expand.click();
  await sheet.getByRole("button", { name: "Close" }).click();
  await expect(sheet).not.toBeVisible();
  await expect(expand).toBeFocused();
});

test("phone More returns keyboard focus, and Go to hands focus to its own dialog", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const more = page.getByRole("button", { name: "More", exact: true });
  const sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await more.focus();
  await page.keyboard.press("Enter");
  await expect(sheet).toBeVisible();
  await expect.poll(() => seriousAxeViolations(page)).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(sheet).not.toBeVisible();
  await expect(more).toBeFocused();

  await more.click();
  await sheet.getByRole("button", { name: "Close" }).click();
  await expect(sheet).not.toBeVisible();
  await expect(more).toBeFocused();

  await more.click();
  await sheet.getByRole("button", { name: /^Go to/ }).click();
  await expect(sheet).not.toBeVisible();
  await expect(page.getByRole("dialog", { name: "Go to" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Go to" })).toBeFocused();
});

test("owner shell names every control by its visible words at desktop width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Go to", exact: true }).first(),
  ).toBeVisible();
  await expect.poll(() => seriousAxeViolations(page)).toEqual([]);
});
