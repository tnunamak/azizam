import { expect, test } from "@playwright/test";

test("Azizam Enabled changes reach the registry only on Save", async ({
  page,
  baseURL,
}) => {
  await page.goto("/ui/engines/");
  await page
    .getByRole("button", { name: "Add engine", exact: true })
    .first()
    .click();
  const add = page.getByRole("dialog", { name: "Add engine", exact: true });
  await add.getByRole("combobox", { name: "Preset" }).click();
  await page.getByRole("option", { name: /OpenAI.?compatible/i }).click();
  await add.getByLabel(/^Name/).fill("Azizam staged engine");
  await add
    .getByLabel(/^Base URL/)
    .fill(`http://127.0.0.1:${Number(new URL(baseURL!).port) + 1}/v1`);
  await add.getByRole("button", { name: "Add engine", exact: true }).click();
  await expect(add).not.toBeVisible();
  const registry = async () => {
    const response = await page.request.get(
      "/admin/api/operator/service-registry",
    );
    expect(response.ok()).toBe(true);
    return (await response.json()).service_registry.services as Record<
      string,
      { id: string; name: string; enabled: boolean }
    >;
  };
  const service = Object.values(await registry()).find(
    (item) => item.name === "Azizam staged engine",
  )!;
  expect(service.enabled).toBe(true);
  const card = page.locator(`tr[data-engine-id="${service.id}"]`);
  await page.goto(`/ui/engines/?service=${encodeURIComponent(service.id)}`);
  for (const dismiss of ["Cancel", "Escape"]) {
    await card.getByRole("button").first().click();
    const edit = page.getByRole("dialog", { name: "Edit engine", exact: true });
    await edit.getByRole("switch", { name: /^Toggle Azizam staged/ }).click();
    expect((await registry())[service.id].enabled).toBe(true);
    if (dismiss === "Cancel")
      await edit.getByRole("button", { name: "Cancel" }).click();
    else await page.keyboard.press("Escape");
    await expect(edit).not.toBeVisible();
    expect((await registry())[service.id].enabled).toBe(true);
  }
  await card.getByRole("button").first().click();
  const edit = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await expect(
    edit.getByRole("switch", { name: /^Toggle Azizam staged/ }),
  ).toBeChecked();
  await edit.getByRole("switch", { name: /^Toggle Azizam staged/ }).click();
  await edit.getByRole("button", { name: "Save engine", exact: true }).click();
  await expect(edit).not.toBeVisible();
  expect((await registry())[service.id].enabled).toBe(false);
  await expect(card.getByText("Disabled", { exact: true })).toBeVisible();
});
