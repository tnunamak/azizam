import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

// Sign in, open Connect from the navigation, copy an address from the recipe.
test("owner signs in, opens Connect and copies an address from the recipe", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  try {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const page = await context.newPage();
    await page.goto("/sign-in/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
      .getByLabel("Password", { exact: true })
      .fill("Portable UI test password 2026");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Connect", exact: true })
      .click();
    await expect(page).toHaveURL(/\/connect\/?$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "Connect" }),
    ).toBeVisible();

    // Every client is one toggle, and the first one is open: its values, then a link to its full steps.
    const picker = page.getByRole("group", { name: "Setting up in" });
    await expect(picker.getByRole("button", { name: "Open WebUI", pressed: true })).toBeVisible();
    expect(await picker.getByRole("button").count()).toBeGreaterThan(10);
    const recipe = page.getByRole("region", { name: "Values for Open WebUI" });
    await expect(recipe.getByLabel("URL", { exact: true })).toHaveValue(`${baseURL}/openai/v1`);
    await expect(recipe.getByRole("link", { name: "Full steps for Open WebUI →" })).toHaveAttribute(
      "href",
      "https://azizam.dev/docs/connect/open-webui/",
    );

    // The address copies to the clipboard as the whole value.
    await recipe.getByRole("button", { name: "Copy URL" }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${baseURL}/openai/v1`);

    // Reference material is not on the page: no tool list, no JSON block, no setup steps.
    await expect(page.getByText("MCP tools this gateway offers")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Copy JSON code" })).toHaveCount(0);
    await expect(page.getByRole("list", { name: "How to connect" })).toHaveCount(0);

    // Connect creates no key: the key hint links to Apps & keys, where keys are made.
    await expect(page.getByRole("link", { name: "Create a key" })).toHaveCount(0);
    await expect(
      recipe.getByRole("link", { name: "Apps & keys", exact: true }),
    ).toHaveAttribute("href", /^\/apps\/?$/);
  } finally {
    await context.close();
  }
});

// An App's menu opens Connect with that App set up, and Back returns to Apps & keys. It only reads, so it runs
// against the shared seed.
test("Set up this app opens Connect for that app, and Back returns to Apps & keys", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/apps/");
  await page.getByRole("button", { name: /^Actions for / }).first().click();
  await page.getByRole("menuitem", { name: "Set up this app", exact: true }).click();
  await expect(page).toHaveURL(/\/connect\/?\?app=/);
  await expect(page.getByRole("heading", { level: 1, name: /^Connect / })).toBeVisible();
  await expect(page.getByRole("group", { name: "Setting up in" })).toBeVisible();
  await page.getByRole("link", { name: "Back to Apps & keys", exact: true }).click();
  await expect(page).toHaveURL(/\/apps\/?$/);
});

// Back and Forward between two Connect pages, each set up for a different app. Apps & keys sits between them in
// the history, so Back crosses a page that is not Connect and the Connect page must read its app id again.
// The member is its own principal, so the test changes no shared state.
test("Back and Forward return to the Connect page of each app, and the Apps and Connect pages pass axe", async ({
  isolated,
}) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/apps/");
  await page.getByRole("button", { name: "New app", exact: true }).first().click();
  await page.getByLabel("App name", { exact: true }).fill(isolated.name("Second app"));
  await page.getByRole("button", { name: "Create app and key", exact: true }).click();
  await page.getByRole("dialog", { name: /^New key for / }).getByRole("button", { name: "I’ve saved the key" }).click();

  // Each app is one list item named by the app, and its keys are a list nested inside it.
  const appNames = page
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { level: 2 }) })
    .getByRole("heading", { level: 2 });
  await expect(appNames).toHaveCount(2);
  const [nameA, nameB] = (await appNames.allTextContents()).map((name) => name.trim());
  const group = page.getByRole("listitem", { name: nameA, exact: true });
  await expect(group.getByRole("list", { name: `Keys for ${nameA}` })).toBeVisible();
  await assertNoSeriousAxe(page);

  const setUp = async (name: string) => {
    await page.getByRole("button", { name: `Actions for ${name}` }).click();
    await page.getByRole("menuitem", { name: "Set up this app", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: `Connect ${name}` })).toBeVisible();
  };
  await setUp(nameA);
  const urlA = page.url();
  await page.getByRole("link", { name: "Back to Apps & keys", exact: true }).click();
  await expect(page).toHaveURL(/\/apps\/?$/);
  await setUp(nameB);
  const urlB = page.url();
  expect(urlB).not.toBe(urlA);
  await assertNoSeriousAxe(page);

  await page.goBack();
  await expect(page).toHaveURL(/\/apps\/?$/);
  await page.goBack();
  await expect(page).toHaveURL(urlA);
  await expect(page.getByRole("heading", { level: 1, name: `Connect ${nameA}` })).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(/\/apps\/?$/);
  await page.goForward();
  await expect(page).toHaveURL(urlB);
  await expect(page.getByRole("heading", { level: 1, name: `Connect ${nameB}` })).toBeVisible();
});

/** Serious and critical axe violations in the page fail the test; minor ones are reported by the other suites. */
async function assertNoSeriousAxe(page: Page) {
  // Scoped to the page, not the shell: a toast (outside #main-content) is checked elsewhere.
  const a11y = await new AxeBuilder({ page }).include("#main-content").analyze();
  // The failure lists each violation's elements, so a contrast miss names the text that failed.
  expect(
    a11y.violations
      .filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""))
      .map((violation) => ({
        id: violation.id,
        targets: violation.nodes.slice(0, 5).map((node) => `${node.target.join(" ")}: ${node.failureSummary ?? ""}`),
      })),
  ).toEqual([]);
}
