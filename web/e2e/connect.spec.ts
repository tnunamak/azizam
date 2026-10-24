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

    // No client is picked until the person picks one: the generic recipe shows, and the first six clients are toggles.
    // The group is named for the picked client once one is picked, so the locator names both.
    const picker = page.getByRole("group", { name: /^(Clients|Setting up: )/ });
    await expect(picker.getByRole("button", { pressed: true })).toHaveCount(0);
    // Only the client toggles carry aria-pressed; "More clients" does not.
    expect(await picker.locator("button[aria-pressed]").count()).toBe(6);
    await expect(picker.getByRole("button", { name: "More clients" })).toBeVisible();
    const recipe = page.getByRole("region", { name: "Values for Any OpenAI-compatible app" });
    await expect(recipe.getByLabel("Base URL", { exact: true })).toHaveValue(`${baseURL}/openai/v1`);

    // The address copies to the clipboard as the whole value.
    await recipe.getByRole("button", { name: "Copy Base URL" }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${baseURL}/openai/v1`);

    // Picking a client opens its own values, with a link to its full steps.
    await picker.getByRole("button", { name: "Open WebUI" }).click();
    await expect(picker.getByRole("button", { name: "Open WebUI", pressed: true })).toBeVisible();
    const openWebUI = page.getByRole("region", { name: "Values for Open WebUI" });
    await expect(openWebUI.getByLabel("URL", { exact: true })).toHaveValue(`${baseURL}/openai/v1`);
    await expect(openWebUI.getByRole("link", { name: "Full steps for Open WebUI →" })).toHaveAttribute(
      "href",
      "https://azizam.dev/docs/connect/open-webui/",
    );

    // The reference addresses come after the picker and the recipe.
    const reference = page.getByRole("heading", { level: 2, name: "Reference", exact: true });
    await expect(reference).toBeVisible();
    expect((await reference.boundingBox())!.y).toBeGreaterThan((await openWebUI.boundingBox())!.y);

    // Reference material is not on the page: no tool list, no JSON block, no setup steps.
    await expect(page.getByText("MCP tools this gateway offers")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Copy JSON code" })).toHaveCount(0);
    await expect(page.getByRole("list", { name: "How to connect" })).toHaveCount(0);

    // Connect creates no key: the key hint links to Apps & keys, where keys are made.
    await expect(page.getByRole("link", { name: "Create a key" })).toHaveCount(0);
    await expect(
      openWebUI.getByRole("link", { name: "Apps & keys", exact: true }),
    ).toHaveAttribute("href", /^\/apps\/?$/);

    // On a phone the whole address shows in its field: it wraps, and nothing is clipped at the end.
    await page.setViewportSize({ width: 390, height: 900 });
    expect(
      await openWebUI.getByLabel("URL", { exact: true }).evaluate(
        (field) => field.scrollWidth <= field.clientWidth && field.scrollHeight <= field.clientHeight,
      ),
    ).toBe(true);
  } finally {
    await context.close();
  }
});

// An App's Set up button opens Connect with that App set up, and Back returns to Apps & keys. It only reads, so it
// runs against the shared seed.
test("Set up opens Connect for that app, and Back returns to Apps & keys", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/apps/");
  await page.getByRole("link", { name: /^Set up / }).first().click();
  await expect(page).toHaveURL(/\/connect\/?\?app=/);
  await expect(page.getByRole("heading", { level: 1, name: /^Connect / })).toBeVisible();
  await expect(page.getByRole("group", { name: /^(Clients|Setting up: )/ })).toBeVisible();
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
    await page.getByRole("link", { name: `Set up ${name}`, exact: true }).click();
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

// An App's Connect page shows the App's access and newest key, then its own client's values with a link to the full
// steps. Revoking the App's only key leaves it with none: Connect then offers Create key, which opens that App's key
// flow on Apps & keys. The member is its own principal, so the test changes no shared state.
test("Connect shows an App's access and key, and Create key on an App with none opens its key flow", async ({
  isolated,
}) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/apps/");
  await page.getByRole("button", { name: "New app", exact: true }).first().click();
  const name = isolated.name("OpenWhispr dictation");
  await page.getByLabel("App name", { exact: true }).fill(name);
  await page.getByRole("button", { name: "Create app and key", exact: true }).click();
  await page.getByRole("dialog", { name: /^New key for / }).getByRole("button", { name: "I’ve saved the key" }).click();

  await page.getByRole("link", { name: `Set up ${name}`, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: `Connect ${name}` })).toBeVisible();
  const strip = page.getByRole("region", { name: "This app: access and key" });
  await expect(strip.getByRole("heading", { level: 2, name: "This app", exact: true })).toBeVisible();
  await expect(strip.getByText(/^Access plan:/)).toBeVisible();
  await expect(strip.getByText(/^(Last used|Never used)/)).toBeVisible();
  await expect(strip.getByRole("link", { name: "Rotate key", exact: true })).toBeVisible();
  await expect(strip.getByRole("link", { name: "Add key", exact: true })).toBeVisible();
  // The App's own client is the one open in the picker, and its full steps link to the public docs.
  const picker = page.getByRole("group", { name: "Setting up: OpenWhispr" });
  await expect(picker.getByRole("button", { name: "OpenWhispr", pressed: true })).toBeVisible();
  const recipe = page.getByRole("region", { name: "Values for OpenWhispr" });
  await expect(recipe.getByRole("link", { name: "Full steps for OpenWhispr →" })).toHaveAttribute(
    "href",
    "https://azizam.dev/docs/connect/openwhispr/",
  );

  // Back on Apps & keys, Revoke is red at rest: on the key row, in the App's menu, and on its confirm button.
  await page.getByRole("link", { name: "Back to Apps & keys", exact: true }).click();
  await expect(page).toHaveURL(/\/apps\/?$/);
  const rowRevoke = page.getByRole("button", { name: `Revoke key ${name}`, exact: true });
  const red = await rowRevoke.evaluate((el) => getComputedStyle(el).color);
  await page.getByRole("button", { name: `Actions for ${name}` }).click();
  const menuRevoke = page.getByRole("menuitem", { name: "Revoke keys", exact: true });
  expect(await menuRevoke.evaluate((el) => getComputedStyle(el).color)).toBe(red);
  await menuRevoke.click();
  const confirm = page.getByRole("dialog").getByRole("button", { name: "Revoke keys", exact: true });
  expect(await confirm.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(red);
  // Revoke the only key: the App now has none.
  await confirm.click();
  await expect(page.getByRole("button", { name: `Add key to ${name}` })).toBeVisible();
  // On a phone a zero-key app keeps Set up on the same row as its menu, so its card stays short.
  await page.setViewportSize({ width: 390, height: 900 });
  const setUpLink = page.getByRole("link", { name: `Set up ${name}`, exact: true });
  const menuButton = page.getByRole("button", { name: `Actions for ${name}` });
  expect(Math.abs((await setUpLink.boundingBox())!.y - (await menuButton.boundingBox())!.y)).toBeLessThan(2);
  await page.setViewportSize({ width: 1440, height: 1000 });

  await page.getByRole("link", { name: `Set up ${name}`, exact: true }).click();
  const empty = page.getByRole("region", { name: "This app: access and key" });
  await expect(empty.getByText("No keys yet", { exact: true })).toBeVisible();
  await empty.getByRole("link", { name: "Create key", exact: true }).click();

  // The link opens this App's key flow: the Create key dialog, with the App already named.
  const create = page.getByRole("dialog", { name: "Create key", exact: true });
  await expect(create).toBeVisible();
  await expect(create.getByLabel("App name", { exact: true })).toHaveValue(name);
  await create.getByRole("button", { name: "Create key", exact: true }).click();
  await page.getByRole("dialog", { name: /^New key for / }).getByRole("button", { name: "I’ve saved the key" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
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
