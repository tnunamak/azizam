import { type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

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
    // Starts with no categories, so the new category is in no plan until this case adds it below.
    const document = { ...plans[0].plan, name: `${name} access`, categories: [] };
    delete document.id;
    const created = await request.post("/admin/api/access-plans", { headers, data: document });
    expect(created.ok()).toBe(true);
    const unused = (await created.json()).access_plan;
    // Who a save reaches depends on the Apps other global-state cases left behind. Each real candidate
    // keeps its real body; only its audience is set here, so the review shows exactly when this case says.
    const nobody = { gains: [], losses: [], cannot_tell: [] };
    const entry = { item_id: "notebook-model", category_id: "", category_label: name, app_id: "app_notebook", app_name: "Notebook",
      person_id: "person_casey", person_label: "Casey", person_plan_id: unused.id, app_plan_id: unused.id };
    let reach: { gains: typeof entry[]; losses: typeof entry[]; cannot_tell: never[] } = nobody;
    await page.route("**/admin/api/categories/candidates", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      await fulfilJson(route, { ...body, preview: { ...body.preview, audience: reach, audience_reason: null } }, { response });
    });
    await page.goto("/categories/");
    await expect(page.getByRole("heading", { name: "Categories", level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "Add category" }).first().click();
    const add = page.getByRole("dialog", { name: "Add category" });
    await add.getByLabel("Name").fill(name);
    await add.getByLabel("What it is for").fill("Not ready yet.");
    // A category has no audience of its own, so the Add dialog offers no choice of who can use it.
    await expect(add.getByRole("radio")).toHaveCount(0);
    await add.getByRole("button", { name: "Add category" }).click();
    const section = page.getByRole("region", { name, exact: true });
    await expect(section.getByText("Not ready yet.", { exact: true })).toBeVisible();
    await expect(section.getByText("No access plan includes it yet", { exact: true })).toBeVisible();
    await expect(section.getByText(`Nothing in ${name} yet`)).toBeVisible();

    await section.getByRole("button", { name: `Edit ${name}` }).click();
    const edit = page.getByRole("dialog", { name: `Edit ${name}` });
    await edit.getByLabel("What it is for").fill("Not ready yet, still.");
    await edit.getByRole("button", { name: "Save", exact: true }).click();
    await expect(section.getByText("Not ready yet, still.", { exact: true })).toBeVisible();

    // The owner sets which plans include a category on People. This case sets the plan through the API, then
    // checks that the Categories page names it.
    const rules = (await (await request.get("/admin/api/categories/active")).json()).snapshot.categories;
    const categoryId = Object.values<{ id: string; label: string }>(rules).find((entry) => entry.label === name)!.id;
    const included = await request.put(`/admin/api/access-plans/${unused.id}`, { headers, data: { ...document, categories: [categoryId] } });
    expect(included.ok()).toBe(true);
    await page.reload();
    await expect(section.getByText(unused.name, { exact: true })).toBeVisible();

    await section.getByRole("button", { name: `Add items to ${name}` }).click();
    const items = page.getByRole("dialog", { name: `Add items to ${name}` });
    const first = items.getByRole("group", { name: "Models and workflows" }).getByRole("checkbox").first();
    await expect(first).toBeVisible();
    const itemName = (await first.getAttribute("aria-label")) ?? "";
    expect(itemName).not.toBe("");
    await first.click();
    // A change that takes a category from someone waits for Turn on. Cancel turns nothing on and keeps the form.
    reach = { ...nobody, losses: [entry] };
    const revision = async () => (await (await request.get("/admin/api/categories/active")).json()).revision;
    const before = await revision();
    await items.getByRole("button", { name: "Add item" }).click();
    const review = page.getByRole("dialog", { name: "Turn on these category rules?" });
    await expect(review.getByText("Can no longer use", { exact: true })).toBeVisible();
    await expect(review.getByText(`Notebook for Casey: notebook-model (${name})`, { exact: true })).toBeVisible();
    await accessible(page, width);
    await review.getByRole("button", { name: "Cancel" }).click();
    await expect(review).toHaveCount(0);
    await expect(items.getByRole("button", { name: "Add item" })).toBeVisible();
    expect(await revision()).toBe(before);
    await items.getByRole("button", { name: "Add item" }).click();
    await expect(review.getByText(`Notebook for Casey: notebook-model (${name})`, { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("turn-on-review.png") });
    await review.getByRole("button", { name: "Turn on" }).click();
    await expect(review).toHaveCount(0);
    await expect(section.getByText(itemName, { exact: true })).toBeVisible();
    reach = nobody;
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
