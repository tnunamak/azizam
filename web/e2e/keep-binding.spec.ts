import type { Page } from "@playwright/test";
import { fulfilJson } from "./fixtures/intercept";
import { expect, ownerApi, test } from "./fixtures/isolation";

// A binding the App-binding cut-over would remove shows the one remedy the gateway names, on the Apps page. Each flow
// changes the browser's copy of the snapshot to say what the gateway would say for that binding, then sends the
// action to the real gateway. Every snapshot after the action is the real one, so the notice and the check line go
// away only because the gateway said so.
//
// Who may keep decides the flow. Only the owner keeps, and only a key nobody else could have made: the owner's own
// key is kept (the gateway already records the owner). A member cannot keep their key, so the member replaces it.
const viewports = [
  { label: "desktop", width: 1440, height: 900 },
  { label: "phone", width: 390, height: 844 },
] as const;

const KEEP_NOTICE =
  "Made before Azizam recorded who made keys. Keep it, or it stops working at the next ownership update.";
const REPLACE_NOTICE =
  "Made before Azizam recorded who made keys. It stops working at the next ownership update. Replace it with a new key.";
const ADD_AGAIN_NOTICE =
  "Made by Alex. It stops working at the next ownership update. Add it again yourself to keep it.";
const ADDRESS_KEEP_NOTICE =
  "Made before Azizam recorded who made addresses. Keep it, or it stops working at the next ownership update.";
// Administrators on record could also have made the binding, so keeping it asks the owner first.
const ADMINISTRATORS = { people: ["Simon Agent"], unnamed_people: 1, admin_keys: 23 };
const CONFIRM_BODY =
  "Azizam did not record who made it. Administrators at the time could also have made it: Simon Agent, 1 person with no name, and 23 administrator keys. Keep it only if you made it or trust them.";
const ADDRESS = "10.42.7.9";

async function noSideways(page: Page, where: string) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth, `${where} must not scroll sideways`).toBeLessThanOrEqual(clientWidth);
}

for (const viewport of viewports) {
  test(`keep a key the cut-over would remove, from the Apps page (${viewport.label})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    let kept = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/me\/api\/credentials\/[^/]+\/adopt$/.test(new URL(request.url()).pathname)) {
        kept = true;
      }
    });
    // The owner's first key, on the first App that has one: the browser's copy says the cut-over would remove it and
    // that the owner keeps it. The gateway records the owner as its maker, so the keep leaves it as it is.
    let target = { appId: "", keyId: "" };
    await page.route("**/me/api/session", async (route) => {
      if (kept) return route.continue();
      const response = await route.fetch();
      const actual = await response.json();
      const app = actual.apps.find((item: { credentials: unknown[] }) => item.credentials.length > 0);
      target = { appId: app.id, keyId: app.credentials[0].id };
      const before = {
        ...actual,
        apps: actual.apps.map((item: { id: string; credentials: { id: string }[] }) => ({
          ...item,
          credentials: item.credentials.map((key) =>
            key.id === target.keyId ? { ...key, created_by: null, cutover: "keep" } : key,
          ),
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
    await page.locator(`#app-actions-${target.appId}`).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    const keys = page.getByRole("dialog", { name: /^Keys for / });
    await expect(keys).toBeVisible();
    await expect(keys.getByText(KEEP_NOTICE, { exact: true })).toBeVisible();
    await noSideways(page, "the keys dialog with the keep notice");
    await page.screenshot({
      path: `test-results/screens/keep-binding-keys-${viewport.width}.png`,
      animations: "disabled",
    });

    await keys.getByRole("button", { name: /^Keep key / }).click();
    await expect(keys.getByText(KEEP_NOTICE, { exact: true })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Keep key / })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Rename key / })).toBeFocused();
    await keys.getByRole("button", { name: "Done", exact: true }).click();
    await expect(keys).not.toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);

    // The kept key stays kept after a reload: the snapshot now comes from the gateway itself.
    await page.reload();
    await expect(page.getByRole("button", { name: /^Actions for / }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
  });

  test(`replace a key the cut-over would remove, from the Apps page (${viewport.label})`, async ({ isolated }) => {
    const member = await isolated.member({ name: "Replace member" });
    const page = member.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    let replaced = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/me\/api\/credentials\/[^/]+\/replace$/.test(new URL(request.url()).pathname)) {
        replaced = true;
      }
    });
    // The member's key: the owner exists and is the only one who may keep, so the gateway says replace.
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

    await page.goto("/apps/");
    const checkLine = page.getByRole("link", { name: "1 key or address needs a quick check" });
    await expect(checkLine).toBeVisible();
    await noSideways(page, "the Apps page with the check line");
    await checkLine.click();
    await page.getByRole("button", { name: /^Actions for / }).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    const keys = page.getByRole("dialog", { name: /^Keys for / });
    await expect(keys.getByText(REPLACE_NOTICE, { exact: true })).toBeVisible();
    await expect(keys.getByRole("button", { name: /^Keep key / })).toHaveCount(0);
    await noSideways(page, "the keys dialog with the replace notice");
    await page.screenshot({
      path: `test-results/screens/keep-binding-replace-keys-${viewport.width}.png`,
      animations: "disabled",
    });

    await keys.getByRole("button", { name: /^Replace key / }).click();
    const reveal = page.getByRole("dialog", { name: /^Connect / });
    await expect(reveal.getByRole("textbox", { name: "Your key" })).toHaveValue(/^opk_/);
    await expect(reveal.getByText("Copy the key now. It is shown only once.")).toBeVisible();
    // The old key is revoked now. The member's principal lives on under the new key, which the teardown checks.
    member.key = await reveal.getByRole("textbox", { name: "Your key" }).inputValue();
    await noSideways(page, "the new key reveal");
    await page.screenshot({
      path: `test-results/screens/keep-binding-replace-reveal-${viewport.width}.png`,
      animations: "disabled",
    });

    await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
    await expect(reveal).not.toBeVisible();
    await expect(keys.getByText(REPLACE_NOTICE, { exact: true })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Replace key / })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Rename key / })).toBeFocused();
    await keys.getByRole("button", { name: "Done", exact: true }).click();
    await expect(keys).not.toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);

    // The new key stays after a reload, and the old one is gone from the gateway's answer.
    await page.reload();
    await expect(page.getByRole("button", { name: /^Actions for / })).toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
  });

  test(`add an address again, from the Apps page (${viewport.label})`, async ({ page, request }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // The owner's own App takes a real address made by the owner. The browser's copy says another person made it,
    // so the gateway would remove it and the owner adds it again. The address is removed when the test ends.
    const owner = await ownerApi(request);
    const { apps } = await (await page.request.get("/me/api/session")).json();
    const app = apps.find((item: { disabled_at: unknown }) => !item.disabled_at) ?? apps[0];
    const added = (await owner.send("POST", `/admin/api/apps/${app.id}/trusted-addresses`, {
      address: ADDRESS,
      note: "Hall speaker",
    })) as { trusted_address: { id: string } };
    const entryId = added.trusted_address.id;
    try {
      let readded = false;
      page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === `/me/api/apps/${app.id}/trusted-addresses/${entryId}/readd`) {
          readded = true;
        }
      });
      await page.route("**/me/api/session", async (route) => {
        if (readded) return route.continue();
        const response = await route.fetch();
        const actual = await response.json();
        const before = {
          ...actual,
          apps: actual.apps.map((item: { id: string }) =>
            item.id === app.id ? { ...item, addresses_to_check: 1 } : item,
          ),
        };
        await fulfilJson(route, before, { response });
      });
      await page.route(`**/admin/api/apps/${app.id}/trusted-addresses`, async (route) => {
        if (readded || route.request().method() !== "GET") return route.continue();
        const response = await route.fetch();
        const actual = await response.json();
        const before = {
          trusted_addresses: actual.trusted_addresses.map((entry: { id: string }) =>
            entry.id === entryId
              ? { ...entry, created_by: "someone-else", made_by_name: "Alex", cutover: "readd" }
              : entry,
          ),
        };
        await fulfilJson(route, before, { response });
      });

      await page.goto("/apps/");
      const checkLine = page.getByRole("link", { name: "1 key or address needs a quick check" });
      await expect(checkLine).toBeVisible();
      await checkLine.click();
      await page.locator(`#app-actions-${app.id}`).click();
      await page.getByRole("menuitem", { name: "Trusted addresses", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: /^Trusted addresses for / });
      await expect(dialog.getByText(ADD_AGAIN_NOTICE, { exact: true })).toBeVisible();
      await noSideways(page, "the trusted addresses dialog with the add-again notice");
      await page.screenshot({
        path: `test-results/screens/keep-binding-readd-${viewport.width}.png`,
        animations: "disabled",
      });

      await dialog.getByRole("button", { name: `Add address ${ADDRESS} again` }).click();
      await expect(dialog.getByText(ADD_AGAIN_NOTICE, { exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: /^Add address / })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: `Remove ${ADDRESS}` })).toBeFocused();
      await dialog.getByRole("button", { name: "Done", exact: true }).click();
      await expect(dialog).not.toBeVisible();
      await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
    } finally {
      const listed = (await owner.get(`/admin/api/apps/${app.id}/trusted-addresses`)) as {
        trusted_addresses: { id: string; address: string }[];
      };
      for (const entry of listed.trusted_addresses.filter((item) => item.address === ADDRESS)) {
        await owner.send("DELETE", `/admin/api/apps/${app.id}/trusted-addresses/${entry.id}`);
      }
    }
  });

  test(`keep a key the owner confirms first, from the Apps page (${viewport.label})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    // Each keep the page sends, with its body. A cancelled confirmation sends none.
    const keeps: (string | null)[] = [];
    let kept = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/me\/api\/credentials\/[^/]+\/adopt$/.test(new URL(request.url()).pathname)) {
        keeps.push(request.postData());
        kept = true;
      }
    });
    // The owner's first key: the browser's copy says the cut-over would remove it and that administrators on record
    // could also have made it, so the keep waits for the owner to confirm them.
    let target = { appId: "" };
    await page.route("**/me/api/session", async (route) => {
      if (kept) return route.continue();
      const response = await route.fetch();
      const actual = await response.json();
      const app = actual.apps.find((item: { credentials: unknown[] }) => item.credentials.length > 0);
      target = { appId: app.id };
      const before = {
        ...actual,
        apps: actual.apps.map((item: { id: string; credentials: Record<string, unknown>[] }) => ({
          ...item,
          credentials: item.credentials.map((key, index) =>
            item.id === app.id && index === 0
              ? { ...key, created_by: null, cutover: "keep", administrators: ADMINISTRATORS }
              : key,
          ),
        })),
      };
      await fulfilJson(route, before, { response });
    });

    await page.goto("/apps/");
    const checkLine = page.getByRole("link", { name: "1 key or address needs a quick check" });
    await expect(checkLine).toBeVisible();
    await checkLine.click();
    await page.locator(`#app-actions-${target.appId}`).click();
    await page.getByRole("menuitem", { name: "Rename keys", exact: true }).click();
    const keys = page.getByRole("dialog", { name: /^Keys for / });
    await expect(keys.getByText(KEEP_NOTICE, { exact: true })).toBeVisible();

    await keys.getByRole("button", { name: /^Keep key / }).click();
    const confirm = page.getByRole("dialog", { name: "Keep this key?" });
    await expect(confirm.getByText(CONFIRM_BODY, { exact: true })).toBeVisible();
    await noSideways(page, "the keep confirmation for a key");
    await page.screenshot({
      path: `test-results/screens/keep-binding-confirm-key-${viewport.width}.png`,
      animations: "disabled",
    });

    await confirm.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(confirm).not.toBeVisible();
    expect(keeps).toEqual([]);
    await expect(keys.getByRole("button", { name: /^Keep key / })).toBeVisible();

    await keys.getByRole("button", { name: /^Keep key / }).click();
    await confirm.getByRole("button", { name: "Keep key", exact: true }).click();
    await expect(confirm).not.toBeVisible();
    expect(keeps.map((body) => JSON.parse(body ?? "null"))).toEqual([{ confirm_admins_could_have_made_it: true }]);
    await expect(keys.getByText(KEEP_NOTICE, { exact: true })).toHaveCount(0);
    await expect(keys.getByRole("button", { name: /^Rename key / })).toBeFocused();
    await keys.getByRole("button", { name: "Done", exact: true }).click();
    await expect(keys).not.toBeVisible();
    await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
  });

  test(`keep an address the owner confirms first, from the Apps page (${viewport.label})`, async ({ page, request }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // The owner's own App takes an address the owner made. The browser's copy says no one recorded its maker, so the
    // keep waits for the owner to confirm the administrators. The address is removed when the test ends.
    const owner = await ownerApi(request);
    const { apps } = await (await page.request.get("/me/api/session")).json();
    const app = apps.find((item: { disabled_at: unknown }) => !item.disabled_at) ?? apps[0];
    const added = (await owner.send("POST", `/admin/api/apps/${app.id}/trusted-addresses`, {
      address: ADDRESS,
      note: "Hall speaker",
    })) as { trusted_address: { id: string } };
    const entryId = added.trusted_address.id;
    try {
      const keeps: (string | null)[] = [];
      let kept = false;
      page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === `/me/api/apps/${app.id}/trusted-addresses/${entryId}/adopt`) {
          keeps.push(request.postData());
          kept = true;
        }
      });
      await page.route("**/me/api/session", async (route) => {
        if (kept) return route.continue();
        const response = await route.fetch();
        const actual = await response.json();
        const before = {
          ...actual,
          apps: actual.apps.map((item: { id: string }) =>
            item.id === app.id ? { ...item, addresses_to_check: 1 } : item,
          ),
        };
        await fulfilJson(route, before, { response });
      });
      await page.route(`**/admin/api/apps/${app.id}/trusted-addresses`, async (route) => {
        if (kept || route.request().method() !== "GET") return route.continue();
        const response = await route.fetch();
        const actual = await response.json();
        const before = {
          trusted_addresses: actual.trusted_addresses.map((entry: { id: string }) =>
            entry.id === entryId
              ? { ...entry, created_by: null, made_by_name: undefined, cutover: "keep", administrators: ADMINISTRATORS }
              : entry,
          ),
        };
        await fulfilJson(route, before, { response });
      });

      await page.goto("/apps/");
      const checkLine = page.getByRole("link", { name: "1 key or address needs a quick check" });
      await expect(checkLine).toBeVisible();
      await checkLine.click();
      await page.locator(`#app-actions-${app.id}`).click();
      await page.getByRole("menuitem", { name: "Trusted addresses", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: /^Trusted addresses for / });
      await expect(dialog.getByText(ADDRESS_KEEP_NOTICE, { exact: true })).toBeVisible();

      await dialog.getByRole("button", { name: `Keep address ${ADDRESS}` }).click();
      const confirm = page.getByRole("dialog", { name: "Keep this address?" });
      await expect(confirm.getByText(CONFIRM_BODY, { exact: true })).toBeVisible();
      await noSideways(page, "the keep confirmation for an address");
      await page.screenshot({
        path: `test-results/screens/keep-binding-confirm-address-${viewport.width}.png`,
        animations: "disabled",
      });

      await confirm.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(confirm).not.toBeVisible();
      expect(keeps).toEqual([]);

      await dialog.getByRole("button", { name: `Keep address ${ADDRESS}` }).click();
      await confirm.getByRole("button", { name: "Keep address", exact: true }).click();
      await expect(confirm).not.toBeVisible();
      expect(keeps.map((body) => JSON.parse(body ?? "null"))).toEqual([{ confirm_admins_could_have_made_it: true }]);
      await expect(dialog.getByText(ADDRESS_KEEP_NOTICE, { exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: `Remove ${ADDRESS}` })).toBeFocused();
      await dialog.getByRole("button", { name: "Done", exact: true }).click();
      await expect(dialog).not.toBeVisible();
      await expect(page.getByRole("link", { name: /need a quick check/ })).toHaveCount(0);
    } finally {
      const listed = (await owner.get(`/admin/api/apps/${app.id}/trusted-addresses`)) as {
        trusted_addresses: { id: string; address: string }[];
      };
      for (const entry of listed.trusted_addresses.filter((item) => item.address === ADDRESS)) {
        await owner.send("DELETE", `/admin/api/apps/${app.id}/trusted-addresses/${entry.id}`);
      }
    }
  });
}
