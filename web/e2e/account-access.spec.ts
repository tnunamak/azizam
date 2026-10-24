import { expect, test } from "./fixtures/isolation";

// A person's access plan is shown on their own settings page. Try no longer carries it, so the plan is found
// in one place (the account page) and the owner's plan tools stay on People.
for (const width of [1440, 390]) {
  test(`your settings name your access plan, and Try shows none of it, at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const session = await (await page.request.get("/me/api/session")).json();
    const plan = session.user?.access_plan as { name: string } | null | undefined;

    await page.goto("/account/");
    const access = page.getByRole("region", { name: "Access" });
    await expect(access).toBeVisible();
    if (plan) {
      await expect(access.getByText(plan.name, { exact: true })).toBeVisible();
    } else {
      await expect(access.getByText("No access plan is assigned to your account. Ask the owner.")).toBeVisible();
    }

    await page.goto("/try/");
    await expect(page.getByRole("heading", { name: "Try" })).toBeVisible();
    await expect(page.getByText(/Signed in as/)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Refresh access" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Check another plan" })).toHaveCount(0);
  });
}
