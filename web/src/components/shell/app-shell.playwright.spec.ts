import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const screenshotDir = "/home/tnunamak/.tmp/gw-product/ui-review/gw-fix-shell";
const ownerPages = ["Engines", "Providers", "People", "Activity", "Settings"];

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

async function seriousAxeViolations(page: Page) {
  const wcag = await new AxeBuilder({ page }).analyze();
  const labelContent = await new AxeBuilder({ page })
    .withRules(["label-content-name-mismatch"])
    .analyze();
  return [
    ...wcag.violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    ),
    ...labelContent.violations.filter(
      (violation) => violation.id === "label-content-name-mismatch",
    ),
  ];
}

async function installMemberRoutes(page: Page) {
  const member = {
    id: "member-fixture",
    name: "Test member",
    email: "member@example.test",
    role: "member",
  };
  const ownerCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
      ownerCalls.push(request.url());
    }
  });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "member-fixture",
        user: member,
        apps: [],
      },
    }),
  );
  await page.route("**/me/api/home", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        user: member,
        apps: [],
        status: { state: "available", reason: "Models are available." },
      },
    });
  });
  return ownerCalls;
}

async function installNamedOwnerSession(page: Page) {
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf_token: "owner-fixture",
        user: {
          id: "owner-fixture",
          name: "Alex Owner",
          email: "owner@example.test",
          role: "owner",
        },
        apps: [{ id: "browser-test-app", name: "Browser test app" }],
      },
    }),
  );
}

async function expectNoOwnerControls(page: Page) {
  for (const name of ownerPages) {
    await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
  }
  await page.keyboard.press("Control+k");
  for (const name of ownerPages) {
    await expect(page.getByRole("option", { name, exact: true })).toHaveCount(
      0,
    );
  }
  await expect(
    page.getByRole("option", { name: "Status", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
}

async function verifyMemberStatusPath(page: Page) {
  await expect(
    page.getByRole("button", { name: "Account menu: Test member" }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Status", exact: true }),
  ).toBeVisible();
  await expectNoOwnerControls(page);

  const viewport = page.viewportSize();
  if (viewport && viewport.width < 640) {
    await page
      .getByRole("navigation", { name: "Phone navigation" })
      .getByRole("link", { name: "Status", exact: true })
      .click();
    await expect(page).toHaveURL(/\/ui\/status\/?$/);
    await page.goto("/ui/");

    await page.getByRole("button", { name: "More", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Explore your gateway" });
    await sheet.getByRole("link", { name: "Status", exact: true }).click();
    await expect(sheet).not.toBeVisible();
    await expect(page).toHaveURL(/\/ui\/status\/?$/);
    await page.goto("/ui/");
  } else {
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Status", exact: true })
      .click();
    await expect(page).toHaveURL(/\/ui\/status\/?$/);
    await page.goto("/ui/");
  }

  await page.keyboard.press("Control+k");
  await page.getByRole("option", { name: "Status", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/status\/?$/);
  await page.goto("/ui/");

  await page
    .getByRole("button", { name: /Gateway status:/ })
    .first()
    .click();
  await expect(page.getByLabel("Gateway health")).toBeVisible();
  await page
    .getByLabel("Gateway health")
    .getByRole("link", { name: "View status" })
    .click();
  await expect(page).toHaveURL(/\/ui\/status\/?$/);
}

test("tablet rail restores focus, closes from the button, and navigates", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 768, height: 1000 });
  await page.goto("/ui/");

  const expand = page.getByRole("button", { name: "Expand navigation" });
  expect(await seriousAxeViolations(page)).toEqual([]);
  await expand.click();
  let sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).not.toBeVisible();
  await expect(expand).toBeFocused();

  await expand.click();
  sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await sheet.getByRole("button", { name: "Close" }).click();
  await expect(sheet).not.toBeVisible();
  await expect(expand).toBeFocused();

  await expand.click();
  sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await sheet.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(sheet).not.toBeVisible();
  await expect(page).toHaveURL(/\/ui\/settings\/?$/);
});

test("phone More returns keyboard focus with motion enabled", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/ui/");
  const more = page.getByRole("button", { name: "More", exact: true });
  await more.focus();
  await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "Explore your gateway" });
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).not.toBeVisible();
  await expect(more).toBeFocused();
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    test(`owner shell labels, focus, and axe are stable at ${viewport.name} ${colorScheme}`, async ({
      page,
    }) => {
      await installNamedOwnerSession(page);
      await mkdir(screenshotDir, { recursive: true });
      await page.emulateMedia({
        colorScheme,
        reducedMotion: viewport.name === "desktop" ? "no-preference" : "reduce",
      });
      await page.setViewportSize(viewport);
      await page.goto("/ui/");

      await expect(
        page.getByRole("link", { name: /Vivid Fish/ }).first(),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Account menu: Alex Owner" }).first(),
      ).toBeVisible();

      const searchButton = page
        .getByRole("button", { name: "Search", exact: true })
        .first();
      if (viewport.name === "desktop") {
        await expect(searchButton).toBeVisible();
        await expect(searchButton).not.toHaveAccessibleName(/⌘K/);
      } else {
        await page.getByRole("button", { name: "More", exact: true }).click();
        const sheet = page.getByRole("dialog", {
          name: "Explore your gateway",
        });
        await expect(sheet).toBeVisible();
        expect(await seriousAxeViolations(page)).toEqual([]);
        await page.screenshot({
          path: path.join(screenshotDir, `shell-more-${colorScheme}.png`),
        });
        await page.keyboard.press("Escape");
        await expect(sheet).not.toBeVisible();
        await expect(
          page.getByRole("button", { name: "More", exact: true }),
        ).toBeFocused();

        await page.getByRole("button", { name: "More", exact: true }).click();
        await sheet.getByRole("button", { name: "Close" }).click();
        await expect(sheet).not.toBeVisible();
        await expect(
          page.getByRole("button", { name: "More", exact: true }),
        ).toBeFocused();

        await page.getByRole("button", { name: "More", exact: true }).click();
        await sheet
          .getByRole("link", { name: "Activity", exact: true })
          .click();
        await expect(sheet).not.toBeVisible();
        await expect(page).toHaveURL(/\/ui\/activity\/?$/);

        await page.goto("/ui/");
        await page.getByRole("button", { name: "More", exact: true }).click();
        await sheet
          .getByRole("button", { name: "Search", exact: true })
          .click();
        await expect(sheet).not.toBeVisible();
        await expect(
          page.getByRole("dialog", { name: "Search your gateway" }),
        ).toBeVisible();
        await expect(
          page.getByRole("combobox", { name: "Search your gateway" }),
        ).toBeFocused();
        await page.keyboard.press("Escape");
      }

      expect(await seriousAxeViolations(page)).toEqual([]);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(viewport.width);
      await page.screenshot({
        path: path.join(
          screenshotDir,
          `shell-${viewport.name}-${colorScheme}.png`,
        ),
        fullPage: true,
      });
    });
  }
}

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    test(`member Status path avoids owner APIs at ${viewport.name} ${colorScheme}`, async ({
      page,
    }) => {
      const ownerCalls = await installMemberRoutes(page);
      await page.emulateMedia({ colorScheme });
      await page.setViewportSize(viewport);
      await page.goto("/ui/");
      await verifyMemberStatusPath(page);
      expect(ownerCalls).toEqual([]);
      expect(await seriousAxeViolations(page)).toEqual([]);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(viewport.width);
      await page.screenshot({
        path: path.join(
          screenshotDir,
          `shell-member-${viewport.name}-${colorScheme}.png`,
        ),
        fullPage: true,
      });
    });
  }
}
