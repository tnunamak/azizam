import { expect, test } from "@playwright/test";

test("Home and client recipes use the saved public URL on a different request host", async ({
  page,
  request,
}) => {
  const path = "/admin/api/settings/instance";
  const originalResponse = await request.get(path);
  expect(originalResponse.ok()).toBe(true);
  const { instance } = await originalResponse.json();
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-ai-gateway-admin-action": "1",
    "x-ai-gateway-csrf": session.actor.csrf_token,
  };
  const publicUrl = "https://family-gateway.example.test";
  try {
    expect(
      (
        await request.put(path, {
          headers,
          data: { ...instance, base_url: publicUrl },
        })
      ).ok(),
    ).toBe(true);
    const home = await (await request.get("/me/api/home")).json();
    expect(home.public_base_url).toBe(publicUrl);
    expect(home.api_base_url).toBe(`${publicUrl}/v1`);
    await page.goto("/ui/home/");
    await expect(
      page.getByRole("textbox", { name: "Base URL", exact: true }),
    ).toHaveValue(`${publicUrl}/v1`);
    await page.getByRole("combobox", { name: "App key", exact: true }).click();
    await page.getByRole("option", { name: /Browser test app/ }).click();
    // Desktop instructions start open; keep this regression independent of that default.
    await page
      .locator("details")
      .filter({ has: page.getByText("Client instructions", { exact: true }) })
      .evaluate((element) => {
        (element as HTMLDetailsElement).open = true;
      });
    await expect(page.getByRole("tabpanel").locator("pre")).toContainText(
      `${publicUrl}/v1`,
    );
    await page.getByRole("tab", { name: "Claude Code", exact: true }).click();
    await expect(page.getByRole("tabpanel").locator("pre")).toContainText(
      `ANTHROPIC_BASE_URL='${publicUrl}'`,
    );
    expect(new URL(page.url()).origin).not.toBe(publicUrl);
  } finally {
    expect((await request.put(path, { headers, data: instance })).ok()).toBe(
      true,
    );
  }
});
