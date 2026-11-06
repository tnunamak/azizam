// One way to end a session in the browser (account switcher W1; SECURITY.md section 9, AC3, AC4 and the
// S3e network case of AC7). Each test signs out a member from isolated.member(), never the lane owner.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

const COVER_HEADING = "Couldn’t finish signing out";
const OTHER_TAB_HEADING = "This tab is signed out";

/** The account menu's Sign out, from any app page. */
async function signOut(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
}

test("tabs: a second tab shows the cover within 1 s, mirrors the pending screen while /logout is delayed, and shows 'This tab is signed out' only after it succeeds", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Tabs member" });
  await member.page.goto("/");
  const second = await member.context.newPage();
  await second.goto("/");
  let release!: () => void;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await member.page.route("**/logout", async (route) => {
    await released;
    await route.continue();
  });

  await signOut(member.page);

  await expect(second.getByText("Signing out…")).toBeVisible({ timeout: 1_000 });
  await expect(second.getByRole("heading", { name: OTHER_TAB_HEADING })).toHaveCount(0);
  await expect(second.getByRole("button", { name: /^Account menu:/ })).toHaveCount(0);

  release();
  await expect(second.getByRole("heading", { name: OTHER_TAB_HEADING })).toBeVisible();
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
});

test("tabs: storage-event fallback works when BroadcastChannel is unavailable", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Fallback member" });
  await member.context.addInitScript(() => {
    Object.defineProperty(window, "BroadcastChannel", { value: undefined, configurable: true });
  });
  await member.page.goto("/");
  const second = await member.context.newPage();
  await second.goto("/");

  await signOut(member.page);

  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
  await expect(second.getByRole("heading", { name: OTHER_TAB_HEADING })).toBeVisible();
});

test("bfcache: Back after sign-out shows the other-tab screen or the sign-in page, not the previous page", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Back member" });
  await member.page.goto("/library/");
  await member.page.goto("/settings/");
  await signOut(member.page);
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);

  await member.page.goBack();

  await expect(member.page.getByText("Back member")).toHaveCount(0);
  await expect(member.page.getByRole("button", { name: /^Account menu:/ })).toHaveCount(0);
});

test("pending: a tab opened while sign-out is pending shows 'Couldn’t finish signing out'", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Pending member" });
  await member.context.route("**/logout", (route) => route.abort());
  await member.page.goto("/");
  await signOut(member.page);
  await expect(member.page.getByRole("heading", { name: COVER_HEADING })).toBeVisible();

  const later = await member.context.newPage();
  await later.goto("/");
  await expect(later.getByRole("heading", { name: COVER_HEADING })).toBeVisible();

  // The gateway answers again: Try again in the later tab finishes it, and the first tab follows.
  await member.context.unroute("**/logout");
  await later.getByRole("button", { name: "Try again" }).click();
  await expect(later).toHaveURL(/\/sign-in\/?$/);
  await expect(member.page.getByRole("heading", { name: OTHER_TAB_HEADING })).toBeVisible();
});

test("network: logout fails → S3e", async ({ isolated }) => {
  const member = await isolated.member({ name: "Offline member" });
  await member.page.goto("/");
  await member.page.route("**/logout", (route) => route.abort());

  await signOut(member.page);

  await expect(member.page.getByRole("heading", { name: COVER_HEADING })).toBeVisible();
  await expect(
    member.page.getByText("This browser couldn’t reach the gateway, so you may still be signed in."),
  ).toBeVisible();
  // Nothing of the app shows behind the cover.
  await expect(member.page.getByRole("button", { name: /^Account menu:/ })).toHaveCount(0);

  await member.page.unroute("**/logout");
  await member.page.getByRole("button", { name: "Try again" }).click();
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
});

test("wipe: after sign-out, storage holds only the theme and the handoff record", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Wipe member" });
  await member.page.goto("/");
  await member.page.evaluate(() => {
    localStorage.setItem("theme", "dark");
    localStorage.setItem("azizam:unfinished-key-rotations", JSON.stringify({ "app-x": ["key-x"] }));
    localStorage.setItem("azizam.try.key", "seeded");
    sessionStorage.setItem("azizam.try.forget", "1");
  });

  await signOut(member.page);
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);

  const after = await member.page.evaluate(async () => {
    const databases = typeof indexedDB.databases === "function" ? await indexedDB.databases() : [];
    const cacheNames = "caches" in window ? await caches.keys() : [];
    const registrations = navigator.serviceWorker ? await navigator.serviceWorker.getRegistrations() : [];
    return {
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      theme: localStorage.getItem("theme"),
      databases: databases.map((database) => database.name),
      cacheNames,
      registrations: registrations.length,
    };
  });
  expect(after.local.every((key) => key === "theme" || key === "azizam.handoff")).toBe(true);
  expect(after.local).toContain("theme");
  expect(after.theme).toBe("dark");
  expect(after.local).not.toContain("azizam:unfinished-key-rotations");
  expect(after.local).not.toContain("azizam.try.key");
  expect(after.session).not.toContain("azizam.try.forget");
  expect(after.databases).toEqual([]);
  expect(after.cacheNames).toEqual([]);
  expect(after.registrations).toBe(0);
});

test("history: no URL or document.title during sign-out contains the person's email", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "History member" });
  await member.page.goto("/");
  await member.page.route("**/logout", (route) => route.abort());
  await signOut(member.page);
  await expect(member.page.getByRole("heading", { name: COVER_HEADING })).toBeVisible();

  const local = member.email.split("@")[0];
  expect(member.page.url()).not.toContain(local);
  expect(await member.page.title()).not.toContain(local);

  await member.page.unroute("**/logout");
  await member.page.getByRole("button", { name: "Try again" }).click();
  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
  expect(member.page.url()).not.toContain(local);
  expect(await member.page.title()).not.toContain(local);
});
