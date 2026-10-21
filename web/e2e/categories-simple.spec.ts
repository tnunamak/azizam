import { type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

const widths = [1440, 390] as const;

async function accessible(page: Page, width: number) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter(({ impact }) => impact === "serious" || impact === "critical")).toEqual([]);
  if (width === 390) {
    for (const target of await page.locator("main button:visible, main summary:visible").all()) {
      const box = await target.boundingBox();
      expect(box, await target.evaluate((node) => node.outerHTML)).not.toBeNull();
      expect(box!.height, await target.evaluate((node) => node.outerHTML)).toBeGreaterThanOrEqual(44);
    }
  }
}

for (const width of widths) {
  // Writes the shared category store, so it runs with the other global-state cases.
  test(`add a category, file an item, move it out at ${width}px`, { tag: GLOBAL_STATE_TAG }, async ({ page, request }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "light" });
    const name = `Labs ${width}`;
    const session = await (await request.get("/admin/api/session")).json();
    const headers = { "x-azizam-csrf": session.actor.csrf_token };
    const plans = (await (await request.get("/admin/api/access-plans")).json()).access_plans;
    const document = { ...plans[0].plan, name: `${name} access` };
    delete document.id;
    const created = await request.post("/admin/api/access-plans", { headers, data: document });
    expect(created.ok()).toBe(true);
    const unused = (await created.json()).access_plan;
    await page.goto("/categories/");
    await expect(page.getByRole("heading", { name: "Categories", level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "Add category" }).first().click();
    const add = page.getByRole("dialog", { name: "Add category" });
    await add.getByLabel("Name").fill(name);
    await add.getByLabel("What it is for").fill("Not ready yet.");
    await expect(add.getByRole("radio", { name: /Only me/ })).toBeChecked();
    await add.getByRole("button", { name: "Add category" }).click();
    const section = page.getByRole("region", { name, exact: true });
    await expect(section.getByText("Only you", { exact: true })).toBeVisible();
    await expect(section.getByText("Not ready yet.", { exact: true })).toBeVisible();
    await expect(section.getByText(`Nothing in ${name} yet`)).toBeVisible();

    await section.getByRole("button", { name: `Edit ${name}` }).click();
    const edit = page.getByRole("dialog", { name: `Edit ${name}` });
    await edit.getByRole("radio", { name: /Me and chosen access plans/ }).click();
    await edit.getByRole("checkbox", { name: unused.name, exact: true }).check();
    await edit.getByRole("button", { name: "Save", exact: true }).click();
    await expect(section.getByText(`You; allowed plans: ${unused.name} (0 people, 0 apps; no one assigned)`, { exact: true })).toBeVisible();

    await section.getByRole("button", { name: `Add items to ${name}` }).click();
    const items = page.getByRole("dialog", { name: `Add items to ${name}` });
    const first = items.getByRole("group", { name: "Models and workflows" }).getByRole("checkbox").first();
    await expect(first).toBeVisible();
    const itemName = (await first.getAttribute("aria-label")) ?? "";
    expect(itemName).not.toBe("");
    await first.click();
    await items.getByRole("button", { name: "Add item" }).click();
    await expect(section.getByText(itemName, { exact: true })).toBeVisible();
    await accessible(page, width);

    await section.getByRole("button", { name: `Move ${itemName}` }).click();
    const move = page.getByRole("dialog", { name: `Move ${itemName}` });
    await move.getByRole("radio", { name: "No category" }).click();
    await move.getByRole("button", { name: "Move" }).click();
    await expect(section.getByText(`Nothing in ${name} yet`)).toBeVisible();

    await page.reload();
    await section.getByRole("button", { name: `Edit ${name}` }).click();
    await page.getByRole("dialog", { name: `Edit ${name}` }).getByRole("button", { name: "Delete category" }).click();
    await page.getByRole("dialog", { name: `Delete ${name}?` }).getByRole("button", { name: "Delete category" }).click();
    await expect(page.getByRole("region", { name, exact: true })).toHaveCount(0);
    const deleted = await request.delete(`/admin/api/access-plans/${unused.id}`, { headers });
    expect(deleted.ok()).toBe(true);

    // The bundled starter categories come from the gateway, not from the page.
    await page.getByRole("button", { name: "Start from a preset" }).click();
    const presets = page.getByRole("dialog", { name: "Start from a preset" });
    await expect(presets.getByText("Content for adults only.")).toBeVisible();
    await expect(presets.getByText("Still being built, so it may not work yet.")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(presets).toHaveCount(0);
  });
}
