import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// Measure settled controls, not the dialog's 95%-to-100% entrance zoom.
test.use({ contextOptions: { reducedMotion: "reduce" } });

for (const width of [1440, 390]) {
  test(`engine controls and dialog layout at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/engines/");
    const trigger = page
      .getByRole("button", { name: "Add engine", exact: true })
      .first();
    await expect(trigger).toBeVisible();
    await trigger.click();
    const dialog = page.getByRole("dialog", {
      name: "Add engine",
      exact: true,
    });
    await expect(dialog.getByLabel("Search server kinds")).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: /Any OpenAI-compatible server/ }),
    ).toBeVisible();
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
    await page.goto("/engines/?service=capture-engine");
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
    await expect(
      dialog.getByRole("button", { name: /Any OpenAI-compatible server/ }),
    ).toBeVisible();
    for (const control of await dialog
      .locator('button:visible, input:not([aria-hidden="true"]):visible')
      .all()) {
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
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeInViewport();
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
    // The engine page replaces the Edit engine sheet.
    await actions.click();
    await page
      .getByRole("menuitem", { name: "Open engine", exact: true })
      .click();
    await expect(page).toHaveURL(/engine=capture-engine/);
    await expect(
      page.getByRole("heading", { level: 1, name: "Local test engine" }),
    ).toBeVisible();
    for (const control of [
      page.getByRole("switch", { name: "Enabled" }),
      page.getByRole("button", { name: "Check again" }),
      page.getByRole("button", { name: "Detect models again" }),
    ]) {
      const box = await control.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
    }
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
  });
}

test("a stale save is rejected by the fixture registry and a retry keeps the typed model name", { tag: GLOBAL_STATE_TAG }, async ({
  page,
}) => {
  await page.goto("/engines/?engine=capture-engine");
  await expect(
    page.getByRole("heading", { level: 1, name: "Local test engine" }),
  ).toBeVisible();
  const publicName = page.getByLabel("Public name").first();
  const original = await publicName.inputValue();
  await publicName.fill("conflict-retry-chat");
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
  const rejected = page.waitForResponse(
    (res) =>
      res.request().method() === "PATCH" &&
      res.url().endsWith("/services/capture-engine") &&
      res.status() === 412,
  );
  await page.getByRole("button", { name: "Save changes" }).click();
  expect((await rejected).status()).toBe(412);
  await expect(page.getByRole("alert").filter({ hasText: "changed elsewhere" })).toBeVisible();
  await expect(publicName).toHaveValue("conflict-retry-chat");
  const saved = page.waitForResponse(
    (res) =>
      res.request().method() === "PATCH" &&
      res.url().endsWith("/services/capture-engine") &&
      res.status() === 200,
  );
  await page.getByRole("button", { name: "Save changes" }).click();
  await saved;
  const registry = (
    await (
      await page.request.get("/admin/api/operator/service-registry")
    ).json()
  ).service_registry;
  const config = registry.services["capture-engine"].config;
  expect(config.note).toBe("Concurrent fixture edit");
  expect(config.models).toContainEqual(
    expect.objectContaining({
      model_group: "conflict-retry-chat",
      upstream_model: "demo-chat",
    }),
  );
  // Put the seed back for the tests that read it.
  const headers = {
    "x-azizam-csrf": session.actor.csrf_token,
    "if-match": (await page.request.get("/admin/api/operator/service-registry")).headers().etag,
  };
  const restored = await page.request.patch(
    "/admin/api/operator/services/capture-engine",
    {
      headers,
      data: {
        definition: "openai-compatible",
        models: [{ id: "demo-chat", public_name: original }],
      },
    },
  );
  expect(restored.status()).toBe(200);
});
