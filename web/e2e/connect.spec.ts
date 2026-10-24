import { expect, test } from "./fixtures/isolation";

// Sign in, open Connect from the navigation, copy the MCP config.
test("owner signs in, opens Connect and copies the MCP config", async ({
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

    // The tools come from the running server, whatever it exposes.
    const live = await (await page.request.get("/.well-known/azizam")).json();
    const names: string[] = live.mcp.tools.map((t: { name: string }) => t.name);
    expect(names.length).toBeGreaterThan(0);
    const list = page.getByRole("list", { name: "MCP tools" });
    for (const name of names) await expect(list).toContainText(name);

    // App setup comes from data: a generic guide, and MCP notes per app.
    await expect(page.getByText("Set up an app", { exact: true })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Your app" })).toContainText("Open WebUI");
    await expect(page.getByText("MCP notes for each app", { exact: true })).toBeVisible();
    await expect(page.getByLabel("URL", { exact: true })).toHaveValue(`${baseURL}/openai/v1`);

    await page.getByRole("button", { name: "Copy JSON code" }).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const config = JSON.parse(copied);
    expect(config.mcpServers.azizam.url).toBe(`${baseURL}/mcp`);
    expect(config.mcpServers.azizam.headers.Authorization).toMatch(/^Bearer /);
    expect(copied).not.toMatch(/opk_/);

    // Connect creates no key: the key section links to Apps & keys, where keys are made.
    await expect(page.getByRole("link", { name: "Create a key" })).toHaveCount(0);
    await expect(
      page.getByLabel("Use a key instead").getByRole("link", { name: "Apps & keys", exact: true }),
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
  await expect(page.getByText(/^Set up .+ with its key\.$/)).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Your app" })).toBeVisible();
  await page.getByRole("link", { name: "Back to Apps & keys", exact: true }).click();
  await expect(page).toHaveURL(/\/apps\/?$/);
});
