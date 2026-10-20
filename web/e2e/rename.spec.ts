import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

// A person renames an app and one of its keys on the Apps page. Each case signs in as its
// own member (e2e/fixtures/isolation.ts), so the names it writes belong to that member.
const viewports = [
  { label: "desktop", width: 1280, height: 800 },
  { label: "phone", width: 390, height: 844 },
] as const;

async function noSideways(page: Page, where: string) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth, `${where} must not scroll sideways`).toBeLessThanOrEqual(clientWidth);
}

for (const viewport of viewports) {
  test(`rename an app and a key, and the new names survive a reload (${viewport.label})`, async ({
    isolated,
  }) => {
    const member = await isolated.member({ name: "Rename member" });
    const page = member.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    const firstName = isolated.name("Rename app");
    const appName = isolated.name("Kitchen tablet");
    const keyName = isolated.name("Counter key");

    // An app with one key, made the way a person makes it.
    await page.goto("/apps/");
    await page.getByRole("button", { name: "New app", exact: true }).first().click();
    await page.getByLabel("App name", { exact: true }).fill(firstName);
    await page.getByRole("button", { name: "Create app and key", exact: true }).click();
    const reveal = page.getByRole("dialog", { name: `Connect ${firstName}` });
    await expect(reveal).toBeVisible();
    await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
    await expect(reveal).not.toBeVisible();

    const rowFor = (name: string) =>
      page
        .getByRole("listitem")
        .filter({ has: page.getByRole("heading", { name, exact: true }) });

    // Rename the app. The list shows the saved name, also after a reload.
    await rowFor(firstName).getByRole("button", { name: `Actions for ${firstName}` }).click();
    await page.getByRole("menuitem", { name: "Rename app", exact: true }).click();
    const renameApp = page.getByRole("dialog", { name: `Rename ${firstName}` });
    await expect(renameApp).toBeVisible();
    const nameField = renameApp.getByLabel("App name", { exact: true });
    await expect(nameField).toHaveValue(firstName);
    await nameField.fill(`  ${appName}  `);
    if (viewport.width < 640) await noSideways(page, "the rename app dialog");
    await renameApp.getByRole("button", { name: "Save", exact: true }).click();
    await expect(renameApp).not.toBeVisible();
    await expect(page.getByRole("heading", { name: appName, exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: firstName, exact: true })).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: `Actions for ${appName}`, exact: true }),
    ).toBeFocused();
    await page.reload();
    await expect(page.getByRole("heading", { name: appName, exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: firstName, exact: true })).toHaveCount(0);

    // Rename the app's key from the list of its keys.
    await rowFor(appName).getByRole("button", { name: `Actions for ${appName}` }).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    const keys = page.getByRole("dialog", { name: `Keys for ${appName}` });
    await expect(keys).toBeVisible();
    await expect(keys.getByRole("listitem")).toHaveCount(1);
    // The key's own words stay masked: only its start shows.
    await expect(keys.getByText(/^opk_.{4}…$/)).toBeVisible();
    await keys.getByRole("button", { name: `Rename key ${firstName}` }).click();
    const keyField = keys.getByLabel("Key name", { exact: true });
    await expect(keyField).toBeFocused();
    await keyField.fill(keyName);
    if (viewport.width < 640) await noSideways(page, "the keys dialog while a key is renamed");
    await keyField.press("Enter");
    await expect(keys.getByText(keyName, { exact: true })).toBeVisible();
    await expect(keys.getByRole("textbox")).toHaveCount(0);
    await expect(keys.getByRole("button", { name: `Rename key ${keyName}` })).toBeFocused();
    await keys.getByRole("button", { name: "Done", exact: true }).click();
    await expect(keys).not.toBeVisible();

    // Both names are still there after a reload.
    await page.reload();
    await rowFor(appName).getByRole("button", { name: `Actions for ${appName}` }).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    await expect(
      page
        .getByRole("dialog", { name: `Keys for ${appName}` })
        .getByText(keyName, { exact: true }),
    ).toBeVisible();
    if (viewport.width < 640) await noSideways(page, "the keys dialog");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    if (viewport.width < 640) await noSideways(page, "the Apps page");
  });
}

test("a refused rename shows the gateway's words and keeps the name editable", async ({ isolated }) => {
  const member = await isolated.member({ name: "Rename refusal member" });
  const page = member.page;
  const first = isolated.name("Rename first");
  const second = isolated.name("Rename second");
  await page.goto("/apps/");
  for (const name of [first, second]) {
    await page.getByRole("button", { name: "New app", exact: true }).first().click();
    await page.getByLabel("App name", { exact: true }).fill(name);
    await page.getByRole("button", { name: "Create app and key", exact: true }).click();
    const reveal = page.getByRole("dialog", { name: `Connect ${name}` });
    await expect(reveal).toBeVisible();
    await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
    await expect(reveal).not.toBeVisible();
  }
  await page.getByRole("button", { name: `Actions for ${second}` }).click();
  await page.getByRole("menuitem", { name: "Rename app", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Rename ${second}` });
  await dialog.getByLabel("App name", { exact: true }).fill(first);
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog.getByRole("alert")).not.toBeEmpty();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("App name", { exact: true })).toBeFocused();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("heading", { name: second, exact: true })).toBeVisible();
});
