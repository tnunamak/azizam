import type { Page } from "@playwright/test";
import { fulfilJson } from "./fixtures/intercept";
import { expect, test } from "./fixtures/isolation";

// A new key on the Apps page stays on screen until its person confirms saving it. The Connect guide opens in a
// new tab. Back asks first, and Stay here keeps the key. Leave anyway discards it, as the question says. Once the
// key is saved, Back goes straight through. Creating and replacing a key both take this path.
// The phone bar labels the Apps tab "Apps"; the desktop sidebar says "Apps & keys".
const viewports = [
  { label: "desktop", width: 1440, height: 900, nav: "Main navigation", appsLink: "Apps & keys" },
  { label: "phone", width: 390, height: 844, nav: "Phone navigation", appsLink: "Apps" },
] as const;

const UNSAVED_TEXT = "You haven’t saved this key yet. It won’t be shown again.";

/** Opens Apps from Library with a click on the shell's own link, so the Apps page is a later entry in this page's history. */
async function openAppsFromLibrary(page: Page, viewport: (typeof viewports)[number]) {
  await page.goto("/library/");
  await page
    .getByRole("navigation", { name: viewport.nav })
    .getByRole("link", { name: viewport.appsLink, exact: true })
    .click();
  await expect(page).toHaveURL(/\/apps\/?$/);
}

/** Whether the page would ask before closing: a closing prompt shows when the event's default is prevented. */
function closingWouldAsk(page: Page) {
  return page.evaluate(() => {
    const closing = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(closing);
    return closing.defaultPrevented;
  });
}

for (const viewport of viewports) {
  test(`a new key stays until it is saved, and Back asks first (${viewport.label})`, async ({ isolated }) => {
    const member = await isolated.member({ name: "Create member" });
    const page = member.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openAppsFromLibrary(page, viewport);

    await page.getByRole("button", { name: "New app" }).first().click();
    await page.getByLabel("App name").fill(isolated.name("Laptop"));
    await page.getByRole("button", { name: "Create app and key" }).click();

    const reveal = page.getByRole("dialog", { name: /^Connect / });
    const key = reveal.getByRole("textbox", { name: "Your key" });
    await expect(key).toHaveValue(/^opk_/);
    await expect.poll(() => closingWouldAsk(page)).toBe(true);

    // The guide opens in its own tab, and the key stays on this page.
    const [guide] = await Promise.all([
      page.waitForEvent("popup"),
      reveal.getByRole("link", { name: "Connect" }).click(),
    ]);
    await guide.waitForURL(/\/connect\/?$/);
    await guide.close();
    await expect(key).toHaveValue(/^opk_/);

    // Back is undone on this page: the question appears, and the key is still on screen.
    await page.goBack();
    const question = page.getByRole("dialog", { name: "Leave anyway?" });
    await expect(question).toContainText(UNSAVED_TEXT);
    await expect(page).toHaveURL(/\/apps\/?$/);
    await question.getByRole("button", { name: "Stay here" }).click();
    await expect(question).toHaveCount(0);
    await expect(key).toHaveValue(/^opk_/);

    await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
    await expect(reveal).toHaveCount(0);
    await expect.poll(() => closingWouldAsk(page)).toBe(false);

    // Once the key is saved, Back goes straight to Library with no question.
    await page.goBack();
    await expect(page).toHaveURL(/\/library\/?$/);
    await expect(page.getByRole("dialog", { name: "Leave anyway?" })).toHaveCount(0);
  });

  test(`a key replaced from its row stays until it is saved, and Leave anyway discards it (${viewport.label})`, async ({
    isolated,
  }) => {
    const member = await isolated.member({ name: "Replace member" });
    const page = member.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    let replaced = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/me\/api\/credentials\/[^/]+\/replace$/.test(new URL(request.url()).pathname)) {
        replaced = true;
      }
    });
    // The member's key is one the cut-over would remove, so its row offers Replace key (as keep-binding.spec.ts sets it up).
    await page.route("**/me/api/session", async (route) => {
      if (replaced) return route.continue();
      const response = await route.fetch();
      const actual = await response.json();
      const before = {
        ...actual,
        apps: actual.apps.map((app: { credentials: Record<string, unknown>[] }) => ({
          ...app,
          credentials: app.credentials.map((key) => ({ ...key, created_by: null, cutover: "replace" })),
        })),
      };
      await fulfilJson(route, before, { response });
    });
    await openAppsFromLibrary(page, viewport);

    await page.getByRole("button", { name: "Replace key" }).click();
    const reveal = page.getByRole("dialog", { name: /^Connect / });
    const key = reveal.getByRole("textbox", { name: "Your key" });
    await expect(key).toHaveValue(/^opk_/);
    // The old key is revoked now. The member's principal lives on under the new key, which the teardown checks.
    member.key = await key.inputValue();
    await expect.poll(() => closingWouldAsk(page)).toBe(true);

    await page.goBack();
    const question = page.getByRole("dialog", { name: "Leave anyway?" });
    await expect(question).toContainText(UNSAVED_TEXT);
    await expect(page).toHaveURL(/\/apps\/?$/);
    await question.getByRole("button", { name: "Leave anyway" }).click();
    await expect(page).toHaveURL(/\/library\/?$/);
    await expect(page.getByRole("dialog", { name: /^Connect / })).toHaveCount(0);
  });
}
