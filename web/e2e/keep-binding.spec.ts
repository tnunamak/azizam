import type { Page } from "@playwright/test";
import { fulfilJson } from "./fixtures/intercept";
import { expect, test } from "./fixtures/isolation";

// A person keeps a key that was made before its creator was recorded, from the Apps page. The gateway
// records a creator for every key it makes now, so the snapshot the browser reads first says this key has
// none (a slice of the real response, checked by fulfilJson). Keeping goes to the real gateway, and every
// snapshot after it is the real one, so the notice and the check line go away only because the gateway said so.
const viewports = [
  { label: "desktop", width: 1440, height: 900 },
  { label: "phone", width: 390, height: 844 },
] as const;

const KEY_NOTICE =
  "Made before Azizam recorded who made keys. Keep it, or it stops working at the next ownership update.";

async function noSideways(page: Page, where: string) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth, `${where} must not scroll sideways`).toBeLessThanOrEqual(clientWidth);
}

for (const viewport of viewports) {
  test(`keep a key made before its creator was recorded, from the Apps page (${viewport.label})`, async ({
    isolated,
  }) => {
    const member = await isolated.member({ name: "Keep member" });
    const page = member.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    let kept = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/me\/api\/credentials\/[^/]+\/adopt$/.test(new URL(request.url()).pathname)) {
        kept = true;
      }
    });
    await page.route("**/me/api/session", async (route) => {
      if (kept) return route.continue();
      const response = await route.fetch();
      const actual = await response.json();
      const before = {
        ...actual,
        apps: actual.apps.map((app: { credentials: { created_by: string | null }[] }) => ({
          ...app,
          credentials: app.credentials.map((key) => ({ ...key, created_by: null })),
        })),
      };
      await fulfilJson(route, before, { response });
    });

    await page.goto("/apps/");
    const checkLine = page.getByRole("link", { name: "1 key or address needs a quick check" });
    await expect(checkLine).toBeVisible();
    await noSideways(page, "the Apps page with the check line");
    await page.screenshot({
      path: `test-results/screens/keep-binding-apps-${viewport.width}.png`,
      animations: "disabled",
    });

    await checkLine.click();
    await page.getByRole("button", { name: /^Actions for / }).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    const keys = page.getByRole("dialog", { name: /^Keys for / });
    await expect(keys).toBeVisible();
    await expect(keys.getByText(KEY_NOTICE, { exact: true })).toBeVisible();
    await noSideways(page, "the keys dialog with the keep notice");
    await page.screenshot({
      path: `test-results/screens/keep-binding-keys-${viewport.width}.png`,
      animations: "disabled",
    });

    await keys.getByRole("button", { name: /^Keep key / }).click();
    await expect(keys.getByText(KEY_NOTICE, { exact: true })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Keep key / })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Rename key / })).toBeFocused();
    await keys.getByRole("button", { name: "Done", exact: true }).click();
    await expect(keys).not.toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);

    // The kept key stays kept after a reload: the snapshot now comes from the gateway itself.
    await page.reload();
    await expect(page.getByRole("button", { name: /^Actions for / })).toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
  });
}
