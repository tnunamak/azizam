import { expect, test } from "@playwright/test";

test("Home uses the saved public URL without assuming every key can call Chat", async ({
  page,
  request,
}) => {
  const path = "/admin/api/settings/instance";
  const originalResponse = await request.get(path);
  expect(originalResponse.ok()).toBe(true);
  const { instance } = await originalResponse.json();
  const session = await (await request.get("/admin/api/session")).json();
  const headers = {
    "x-azizam-admin-action": "1",
    "x-azizam-csrf": session.actor.csrf_token,
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
    await page.getByRole("button", { name: "Open WebUI", exact: true }).click();
    await expect(
      page.getByRole("textbox", { name: "Base URL", exact: true }),
    ).toHaveValue(`${publicUrl}/v1`);
    await page.getByRole("combobox", { name: "API key", exact: true }).click();
    await page
      .getByRole("option", {
        name: "Browser test app · Seeded desktop key",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Setup notes", exact: true })
      .click();
    await expect(
      page.getByRole("dialog", { name: "Setup notes" }),
    ).toContainText("gateway check verifies the route and key");
    const model = page.getByRole("textbox", { name: "Model", exact: true });
    if (await model.count()) {
      await expect(model).toHaveValue("default");
      await expect(page.locator("pre code:visible")).toContainText(
        `${publicUrl}/v1`,
      );
    } else {
      await expect(page.locator("pre code:visible")).toHaveCount(0);
    }
    expect(new URL(page.url()).origin).not.toBe(publicUrl);
  } finally {
    expect((await request.put(path, { headers, data: instance })).ok()).toBe(
      true,
    );
  }
});
