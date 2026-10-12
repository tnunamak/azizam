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

    await page.getByRole("button", { name: "Copy JSON code" }).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const config = JSON.parse(copied);
    expect(config.mcpServers.azizam.url).toBe(`${baseURL}/mcp`);
    expect(config.mcpServers.azizam.headers.Authorization).toMatch(/^Bearer /);
    expect(copied).not.toMatch(/opk_/);

    await expect(page.getByRole("link", { name: "Create a key" })).toHaveAttribute(
      "href",
      /^\/apps\/?\?action=create-key$/,
    );
  } finally {
    await context.close();
  }
});
