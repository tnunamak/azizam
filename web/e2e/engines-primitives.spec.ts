import { test, expect } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`engine controls and dialog layout at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/ui/engines/");
    const trigger = page
      .getByRole("button", { name: "Add engine", exact: true })
      .first();
    await expect(trigger).toBeVisible();
    const phase = process.env.BATCH5_SCREENSHOT_PHASE ?? "after";
    await page.screenshot({
      path: `test-results/screens/batch5-${phase}-engines-${width}.png`,
      animations: "disabled",
      fullPage: true,
    });
    await trigger.click();
    const dialog = page.getByRole("dialog", {
      name: "Add engine",
      exact: true,
    });
    await expect(dialog.getByLabel(/^Name/)).toBeVisible();
    await page.screenshot({
      path: `test-results/screens/batch5-${phase}-add-${width}.png`,
      animations: "disabled",
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
  });
}

for (const scale of [1, 2]) {
  test(`phone controls, scrolling and focus at ${scale * 100}% text`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/ui/engines/?service=capture-engine");
    await page.evaluate((factor) => {
      document.documentElement.style.fontSize = `${16 * factor}px`;
    }, scale);
    const trigger = page
      .getByRole("button", { name: "Add engine", exact: true })
      .first();
    await trigger.click();
    const dialog = page.getByRole("dialog", {
      name: "Add engine",
      exact: true,
    });
    const name = dialog.getByLabel(/^Name/);
    await expect(name).toBeVisible();
    for (const control of await dialog
      .locator('button, input:not([aria-hidden="true"])')
      .all()) {
      if (!(await control.isVisible())) continue;
      const box = await control.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
    }
    const body = dialog.locator('[data-slot="dialog-body"]');
    const footer = dialog.locator('[data-slot="dialog-footer"]');
    const before = await footer.boundingBox();
    await body.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    expect(await footer.boundingBox()).toEqual(before);
    expect(before!.y + before!.height).toBeLessThanOrEqual(844);
    await expect(
      dialog.getByRole("button", { name: "Add engine", exact: true }),
    ).toBeInViewport();
    await dialog.getByRole("button", { name: "Close", exact: true }).focus();
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
    await page.keyboard.press("Shift+Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(trigger).toBeFocused();
    const row = page.locator('[data-engine-id="capture-engine"]');
    const actions = row.getByRole("button", { name: /Actions/ });
    await actions.click();
    await page
      .getByRole("menuitem", { name: "Remove engine", exact: true })
      .click();
    const remove = page.getByRole("dialog", {
      name: "Remove engine?",
      exact: true,
    });
    await expect(remove.getByRole("textbox")).toBeFocused();
    await expect(
      remove.getByRole("button", { name: "Remove engine", exact: true }),
    ).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(actions).toBeFocused();
    await actions.click();
    await page
      .getByRole("menuitem", { name: "Detect models", exact: true })
      .click();
    const sheet = page.getByRole("dialog", {
      name: "Edit engine",
      exact: true,
    });
    await sheet.getByRole("button", { name: "Detect", exact: true }).click();
    const consent = sheet.getByRole("checkbox", {
      name: "Use existing access rules for these models",
    });
    await expect(consent).toBeVisible();
    for (const checkbox of await sheet.getByRole("checkbox").all()) {
      const box = await checkbox.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
    }
    await consent.focus();
    await page.keyboard.press("Space");
    await expect(consent).toBeChecked();
    await page.keyboard.press("Escape");
    await expect(actions).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("a stale apply ETag is rejected by the fixture registry and retry preserves mappings", async ({
  page,
}) => {
  await page.goto("/ui/engines/?service=capture-engine");
  await page
    .locator('[data-engine-id="capture-engine"]')
    .getByRole("button", { name: /Actions/ })
    .click();
  await page
    .getByRole("menuitem", { name: "Detect models", exact: true })
    .click();
  const sheet = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await sheet.getByRole("button", { name: "Detect", exact: true }).click();
  await sheet
    .getByLabel("Public name for demo-chat")
    .fill("conflict-retry-chat");
  await sheet.getByLabel("Use existing access rules for these models").check();
  const session = await (await page.request.get("/admin/api/session")).json();
  let conflictInjected = false;
  await page.route(
    "**/admin/api/operator/services/capture-engine",
    async (route) => {
      if (route.request().method() === "PATCH" && !conflictInjected) {
        conflictInjected = true;
        const etag = route.request().headers()["if-match"];
        expect(etag).toBeTruthy();
        const concurrent = await page.request.patch(
          "/admin/api/operator/services/capture-engine",
          {
            headers: {
              "x-azizam-csrf": session.actor.csrf_token,
              "if-match": etag,
            },
            data: { note: "Concurrent fixture edit" },
          },
        );
        expect(concurrent.status()).toBe(200);
      }
      await route.continue();
    },
  );
  const response = page.waitForResponse(
    (res) =>
      res.request().method() === "PATCH" &&
      res.url().endsWith("/services/capture-engine") &&
      res.status() === 412,
  );
  await sheet.getByRole("button", { name: "Apply models" }).click();
  expect((await response).status()).toBe(412);
  await expect(sheet.getByRole("alert")).toBeVisible();
  await expect(sheet.getByLabel("Public name for demo-chat")).toHaveValue(
    "conflict-retry-chat",
  );
  await sheet.getByRole("button", { name: "Apply models" }).click();
  await expect(sheet.getByText(/models applied/)).toBeVisible();
  const registry = (
    await (
      await page.request.get("/admin/api/operator/service-registry")
    ).json()
  ).service_registry;
  expect(registry.services["capture-engine"].config.note).toBe(
    "Concurrent fixture edit",
  );
  expect(registry.services["capture-engine"].config.models).toContainEqual(
    expect.objectContaining({
      model_group: "conflict-retry-chat",
      upstream_model: "demo-chat",
    }),
  );
});
